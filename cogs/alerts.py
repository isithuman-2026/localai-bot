import hashlib
import os
import re
import time
import discord
from discord.ext import commands
import llm
import lokiquery
import vulnlookup
import vault
import memory

ALERTS_CHANNEL_ID = int(os.environ.get("ALERTS_CHANNEL_ID", "1488857934061633697"))

HOMELAB_CONTEXT = """
IMPORTANT CONSTRAINTS — always follow regardless of other context:
- Oumuamua (UDR) is a hardware router — NEVER suggest "docker logs" for UDR events.
- vault44 and Alpha60 are Synology NAS hardware — NEVER suggest "docker logs" for them.
- Loki is only reachable inside the Docker network — NEVER suggest curl to Loki. Use Grafana Explore instead.
- The user is on their workstation, not on node1. Always prefix node1 shell commands with: ssh boss@node1

"""

ASK_PROMPT = (
    "You are JARVIS, a homelab ops assistant. Answer the user's question directly and conversationally.\n\n"
    + HOMELAB_CONTEXT
    + "\nBe concise. No preamble. No filler. Commands must be copy-pasteable."
)

TRIAGE_PROMPT_JSON = (
    "You are a homelab security and ops assistant. You have full knowledge of this homelab's topology and services.\n\n"
    + HOMELAB_CONTEXT
    + "\nAnalyse the alert. Respond ONLY with a JSON object matching this exact schema:\n"
    '{"severity":"critical|high|medium|low","cause":"one sentence root cause","confidence":0.0,'
    '"commands":["ssh boss@node1 <exact command>"],"next_step":"one concrete action","suppress":false}\n\n'
    "Rules:\n"
    "- severity: use 'low' only if truly non-impacting noise\n"
    "- confidence: 0.0-1.0, your certainty about the cause\n"
    "- commands: max 3, copy-pasteable, prefix node1 commands with 'ssh boss@node1'\n"
    "- suppress: true only if this is known persistent noise with no action needed\n"
    "- No preamble. No explanation. JSON only."
)

TRIAGE_PROMPT_VULN_JSON = (
    "You are a homelab security assistant. You have full knowledge of this homelab's topology and services.\n\n"
    + HOMELAB_CONTEXT
    + "\nSecurity packages were just auto-installed on node1. Vuln context is provided below the alert.\n"
    "Respond ONLY with a JSON object matching this exact schema:\n"
    '{"severity":"critical|high|medium|low","cause":"most critical CVE or issue in one sentence","confidence":0.0,'
    '"commands":["ssh boss@node1 <exact command>"],"next_step":"one concrete action","suppress":false}\n\n'
    "Rules:\n"
    "- commands: exact command to confirm patch is applied\n"
    "- No preamble. JSON only."
)

TRIAGE_COOLDOWN_SECS = 3600

_UPDATE_RE = re.compile(r"security updates installed", re.IGNORECASE)
_CODE_BLOCK_RE = re.compile(r"```\s*(.*?)\s*```", re.DOTALL)
_GRAFANA_RESOLVED_RE = re.compile(r"^\s*\*{0,2}Resolved\*{0,2}\s*$", re.MULTILINE | re.IGNORECASE)
_GRAFANA_FIRING_RE = re.compile(r"^\s*Firing\s*$", re.MULTILINE)
_ALERTNAME_RE = re.compile(r"alertname\s*=\s*(.+)")
_NOTHING_NOTABLE_RE = re.compile(r"🔴\s*NOTHING_NOTABLE.*?(?=🟡|🔴|$)", re.DOTALL | re.IGNORECASE)


def _is_empty_alert(content: str) -> bool:
    text = re.sub(r"\[[\w\s|]+\|\s*review\]\s*\d{1,2}:\d{2}\s*(?:UTC)?", "", content)
    text = re.sub(r"🟡\s*severity:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"🔴\s*NOTHING_NOTABLE.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return len(text) < 20


def _fingerprint(content: str) -> str:
    parts = []
    ips = re.findall(r"\b10\.\d+\.\d+\.\d+\b", content)
    parts.extend(ips[:2])
    for kw in ("timeout", "scraping", "tmdb", "oom", "container down", "fail2ban", "eth4", "520", "429"):
        if kw in content.lower():
            parts.append(kw)
    if re.search(r"\[udr\s*\|", content, re.IGNORECASE):
        parts.append("udr")
    elif re.search(r"\[node1\s*\|", content, re.IGNORECASE):
        parts.append("node1")
    key = ":".join(sorted(set(p.lower() for p in parts))) or "generic"
    return hashlib.md5(key.encode()).hexdigest()[:10]


def _format_triage_reply(result: dict, history: dict | None = None) -> str:
    severity = result.get("severity", "unknown").upper()
    cause = result.get("cause") or result.get("root_cause", "unknown")
    confidence = result.get("confidence", 0.0)
    commands = result.get("commands") or result.get("recommended_actions", [])
    next_step = result.get("next_step", "")
    evidence = result.get("evidence", [])

    lines = [f"**[{severity}]** {cause}"]
    lines.append(f"Confidence: {confidence:.0%}")

    if history and history.get("occurrence_count", 1) > 1:
        count = history["occurrence_count"]
        last = time.strftime("%H:%M UTC", time.gmtime(history["last_seen"]))
        lines.append(f"Seen: {count}x (last: {last})")

    if evidence:
        lines.append("\n**Evidence:**")
        for e in evidence[:3]:
            lines.append(f"- {e}")

    if commands:
        lines.append("\n**Suggested commands:**")
        lines.append("```")
        for cmd in commands[:3]:
            lines.append(cmd)
        lines.append("```")

    if next_step:
        lines.append(f"\n**Next:** {next_step}")

    return "\n".join(lines)


class AlertsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._seen: set[int] = set()
        self._active_alerts: dict[str, int] = {}
        self._triage_cooldowns: dict[str, tuple[int, float]] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != ALERTS_CHANNEL_ID:
            return

        if not message.author.bot and self.bot.user in message.mentions:
            content = message.content
            for fmt in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
                content = content.replace(fmt, "")
            content = content.strip()
            if content:
                await self._ask(message, content)
            return

        if message.author.bot and message.author.id != self.bot.user.id:
            if message.id in self._seen:
                return
            self._seen.add(message.id)
            if len(self._seen) > 100:
                self._seen = set(list(self._seen)[-100:])

            content = message.content
            alertname_m = _ALERTNAME_RE.search(content)
            alertname = alertname_m.group(1).strip() if alertname_m else ""

            if _GRAFANA_RESOLVED_RE.search(content) or "[RESOLVED]" in content:
                prior_id = self._active_alerts.pop(alertname, None)
                note = f"**Resolved:** {alertname or 'alert'} auto-cleared — no action needed."
                if prior_id:
                    note += f"\nCorrelates to earlier firing alert (message {prior_id})."
                await message.reply(note)
                return

            if _GRAFANA_FIRING_RE.search(content) and alertname:
                self._active_alerts[alertname] = message.id
                if len(self._active_alerts) > 20:
                    self._active_alerts = dict(list(self._active_alerts.items())[-20:])

            await self._triage(message)

    async def _ask(self, message: discord.Message, user_text: str) -> None:
        vault_write_match = re.match(
            r"update vault:\s*(TheLab/\S+\.md)\s*\n(.*)",
            user_text,
            re.DOTALL | re.IGNORECASE,
        )
        if vault_write_match:
            path, content = vault_write_match.group(1), vault_write_match.group(2).strip()
            ok = vault.update_note(path, content)
            await message.reply(f"Vault updated: `{path}`" if ok else f"Failed to write `{path}` (TheLab/ only, check path).")
            return

        vault_ctx = vault.search(user_text)
        loki_ctx = await lokiquery.fetch_context(user_text, user_text)
        mem_facts = memory.search_facts(user_text)
        system = ASK_PROMPT + (f"\n\n{vault_ctx}" if vault_ctx else "")
        if mem_facts:
            facts_text = "\n".join(f"- [{r['topic']}] {r['content']}" for r in mem_facts)
            system += f"\n\nKnown facts from memory:\n{facts_text}"
        user_content = user_text + (f"\n\n{loki_ctx}" if loki_ctx else "")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        answer = await llm.chat(messages)
        await message.reply(answer[:1990])

    async def _triage(self, message: discord.Message) -> None:
        content = message.content

        if _is_empty_alert(content):
            return

        content = _NOTHING_NOTABLE_RE.sub("", content).strip()
        if _is_empty_alert(content):
            return

        suppressed, suppress_reason = memory.is_suppressed(content)
        if suppressed:
            await message.reply(f"Suppressed (known noise): {suppress_reason}")
            return

        fp = _fingerprint(content)
        auto_sup, auto_reason = memory.check_auto_suppress(fp)
        if auto_sup:
            await message.reply(f"Auto-suppressed: {auto_reason}")
            return

        now = time.time()
        if fp in self._triage_cooldowns:
            count, first_ts = self._triage_cooldowns[fp]
            if now - first_ts < TRIAGE_COOLDOWN_SECS:
                self._triage_cooldowns[fp] = (count + 1, first_ts)
                first_time = time.strftime("%H:%M UTC", time.gmtime(first_ts))
                await message.reply(
                    f"Ongoing — {count + 1} occurrences since {first_time}. "
                    f"Pattern unchanged; original triage above still applies."
                )
                return
        self._triage_cooldowns[fp] = (1, now)
        if len(self._triage_cooldowns) > 50:
            cutoff = now - TRIAGE_COOLDOWN_SECS
            self._triage_cooldowns = {k: v for k, v in self._triage_cooldowns.items() if v[1] > cutoff}

        system_prompt = TRIAGE_PROMPT_JSON
        vuln_context = ""

        if _UPDATE_RE.search(content):
            match = _CODE_BLOCK_RE.search(content)
            if match:
                packages = match.group(1).split()
                if packages:
                    vuln_context = await vulnlookup.enrich_packages(packages)
                    if vuln_context:
                        system_prompt = TRIAGE_PROMPT_VULN_JSON

        label_match = re.search(r"\[([^\]]+)\]", content)
        alert_label = label_match.group(1) if label_match else ""

        loki_ctx = await lokiquery.fetch_context(alert_label, content)
        vault_ctx = vault.search(content)
        mem_facts = memory.search_facts(content)

        if vault_ctx:
            system_prompt += f"\n\n{vault_ctx}"
        if mem_facts:
            facts_text = "\n".join(f"- [{r['topic']}] {r['content']}" for r in mem_facts)
            system_prompt += f"\n\nKnown facts from memory:\n{facts_text}"

        user_content = f"Alert: {content}"
        if loki_ctx:
            user_content += f"\n\n{loki_ctx}"
        if vuln_context:
            user_content += f"\n\n{vuln_context}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            result = await llm.chat_json(messages)
        except Exception:
            answer = await llm.chat(messages)
            await message.reply(answer[:1990])
            memory.log_observation(event=content[:200], summary=answer[:500], host=alert_label)
            return

        if result.get("suppress"):
            memory.add_suppression(fp, reason=result.get("cause", "LLM-flagged noise"), expires=0)
            await message.reply(f"Suppressed (LLM-flagged): {result.get('cause', 'persistent noise')}")
            return

        history = memory.upsert_alert_history(
            fingerprint=fp,
            root_cause=result.get("cause", ""),
            confidence=result.get("confidence", 0.0),
            severity=result.get("severity", ""),
        )

        reply = _format_triage_reply(result, history)
        await message.reply(reply[:1990])

        memory.log_observation(
            event=content[:200],
            summary=result.get("cause", ""),
            host=alert_label,
            fingerprint=fp,
            root_cause=result.get("cause", ""),
            confidence=result.get("confidence", 0.0),
            severity=result.get("severity", ""),
        )
