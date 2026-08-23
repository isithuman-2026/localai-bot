import asyncio
import hashlib
import json
import os
import re
import time
import discord
from discord.ext import commands
import llm
import lokiquery
import vault
import memory
import sysstats
import checks

ALERTS_CHANNEL_ID = int(os.environ.get("ALERTS_CHANNEL_ID", "1488857934061633697"))
DISCLAUDE_BOT_ID = int(os.environ.get("DISCLAUDE_BOT_ID", "1495259014014046229"))

HOMELAB_CONTEXT = """
IMPORTANT CONSTRAINTS — always follow regardless of other context:
- Oumuamua (UDR) is a hardware router — NEVER suggest "docker logs" for UDR events. UDR processes (mcad, uled-ctrl, ubios-udapi-server, mca-ctrl) run on the router itself, not on node1. To investigate UDR logs, SSH directly to the UDR (ssh admin@<UDR-IP>), not to node1.
- mcad calling uled-ctrl with "fw idle" is normal UniFi OS LED power management. Do NOT treat this as suspicious or suggest it is an attack.
- vault44 and Alpha60 are Synology NAS hardware — NEVER suggest "docker logs" for them.
- Loki is only reachable inside the Docker network — NEVER suggest curl to Loki and NEVER use grafana-cli to query logs. Use Grafana Explore in the browser instead.
- The user is on their workstation, not on node1. Always prefix node1 shell commands with: ssh boss@node1
- MAC addresses with the locally-administered bit set (second bit of first octet: prefixes like 6e:, da:, 2e:, 4e:, ce:, etc.) are iOS/Android privacy/randomized MACs — they are NOT unknown attacker devices. WiFi deauth events for these are normal. Deauth reason code 8 = station leaving BSS normally.

"""

ASK_PROMPT = (
    "You are JARVIS, a homelab ops assistant. Answer the user's question directly and conversationally.\n\n"
    + HOMELAB_CONTEXT
    + "\nBe concise. No preamble. No filler. Commands must be copy-pasteable."
)

TOOL_AVAILABILITY_NOTE = (
    "\nYou have read-only diagnostic tools available (docker_inspect, docker_logs, ping, curl_health, "
    "query_prometheus, query_loki, disk_usage). Use one if it would sharpen your diagnosis — check the "
    "actual state rather than guessing. Once you're confident, stop calling tools and answer with the JSON "
    "schema below. Don't use tools for alerts that are already clear from the context provided.\n"
)

TRIAGE_PROMPT_JSON = (
    "You are a homelab security and ops assistant. You have full knowledge of this homelab's topology and services.\n\n"
    + HOMELAB_CONTEXT
    + "\nAnalyse the alert."
    + TOOL_AVAILABILITY_NOTE
    + " Respond ONLY with a JSON object matching this exact schema:\n"
    '{"severity":"critical|high|medium|low","cause":"one sentence root cause","confidence":0.0,'
    '"commands":["ssh boss@node1 <exact command>"],"next_step":"one concrete action","suppress":false}\n\n'
    "Rules:\n"
    "- severity: use 'low' only if truly non-impacting noise\n"
    "- confidence: 0.0-1.0, your certainty about the cause\n"
    "- commands: max 3, copy-pasteable, prefix node1 commands with 'ssh boss@node1'\n"
    "- suppress: true only if this is known persistent noise with no action needed\n"
    "- No preamble. No explanation. JSON only."
)

TRIAGE_PROMPT_HYPOTHESIS = (
    "You are a homelab security and ops assistant. You have full knowledge of this homelab's topology and services.\n\n"
    + HOMELAB_CONTEXT
    + "\nAnalyse the alert. Generate up to 3 hypotheses ordered by confidence descending."
    + TOOL_AVAILABILITY_NOTE
    + " Respond ONLY with a JSON object matching this exact schema:\n"
    '{"severity":"critical|high|medium|low","hypotheses":[{"cause":"string","confidence":0.0,"commands":["ssh boss@node1 <cmd>"]}],'
    '"next_step":"string","suppress":false}\n\n'
    "Rules:\n"
    "- Max 3 hypotheses, highest confidence first\n"
    "- commands per hypothesis: diagnostic commands to verify that specific hypothesis only\n"
    "- suppress: true only for known persistent noise\n"
    "- No preamble. JSON only."
)

TRIAGE_COOLDOWN_SECS = 3600

_UPDATE_RE = re.compile(r"security updates installed", re.IGNORECASE)
_CODE_BLOCK_RE = re.compile(r"```\s*(.*?)\s*```", re.DOTALL)
_GRAFANA_RESOLVED_RE = re.compile(r"^\s*\*{0,2}Resolved\*{0,2}\s*$", re.MULTILINE | re.IGNORECASE)
_GRAFANA_FIRING_RE = re.compile(r"^\s*Firing\s*$", re.MULTILINE)
_ALERTNAME_RE = re.compile(r"alertname\s*=\s*(.+)")
_NOTHING_NOTABLE_RE = re.compile(r"🔴\s*NOTHING_NOTABLE.*?(?=🟡|🔴|$)", re.DOTALL | re.IGNORECASE)
_ABUSEIPDB_RE = re.compile(r"AbuseIPDB Reporter", re.IGNORECASE)


def _is_empty_alert(content: str) -> bool:
    text = re.sub(r"\*{0,2}\[[\w\s|]+\|\s*(?:review|infra|udr)\]\*{0,2}\s*\d{1,2}:\d{2}\s*(?:UTC)?", "", content, flags=re.IGNORECASE)
    text = re.sub(r"[🟡🔴]\s*severity:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"🔴\s*NOTHING_NOTABLE.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return len(text) < 20


_INFRA_CONTAINERS = {"traefik", "socket-proxy", "socket_proxy", "authelia", "infra-traefik"}

_SECURITY_KEYWORDS = {
    "honeypot", "brute force", "bruteforce", "fail2ban", "banned", "blocked",
    "ssh auth", "failed to log in", "failed to sign in", "authorization failure",
    "account protection", "intrusion", "ids", "ips", "threat", "port scan",
    "honeypot triggered", "ssh brute", "dsm login",
}

def _is_infra_alert(content: str) -> bool:
    lower = content.lower()
    return any(c in lower for c in _INFRA_CONTAINERS)


def _is_security_alert(content: str) -> bool:
    lower = content.lower()
    return any(kw in lower for kw in _SECURITY_KEYWORDS)


def _fingerprint(content: str) -> str:
    parts = []
    ips = re.findall(r"\b10\.\d+\.\d+\.\d+\b", content)
    parts.extend(ips[:2])
    for kw in (
        "timeout", "scraping", "tmdb", "oom", "container down", "fail2ban", "eth4", "520", "429",
        "honeypot", "ssh auth", "failed to log in", "failed to sign in", "account protection",
        "brute force", "banned", "blocked", "authorization failure",
    ):
        if kw in content.lower():
            parts.append(kw)
    if re.search(r"\[udr\s*\|", content, re.IGNORECASE):
        parts.append("udr")
    elif re.search(r"\[node1\s*\|", content, re.IGNORECASE):
        parts.append("node1")
    elif re.search(r"\[alpha60\s*\|", content, re.IGNORECASE):
        parts.append("alpha60")
    key = ":".join(sorted(set(p.lower() for p in parts))) or "generic"
    return hashlib.sha256(key.encode()).hexdigest()[:10]


def _parse_triage_json(content: str) -> dict:
    """Parse a JSON verdict object out of a chat_with_tools final answer.

    Unlike chat_json(), chat_with_tools() doesn't force response_format=json_object,
    so a local model's answer may be wrapped in a ```json fence or preceded by prose.
    Strip a fence if present, then fall back to slicing the first {...} span.
    Raises ValueError if the result parses but isn't a JSON object.
    """
    text = content.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("parsed JSON is not an object")
    return parsed


def _format_triage_reply(result: dict, history: dict | None = None) -> str:
    severity = result.get("severity", "unknown").upper()
    next_step = result.get("next_step", "")
    evidence = result.get("evidence", [])

    lines = [f"**[{severity}]**"]

    if history and history.get("occurrence_count", 1) > 1:
        count = history["occurrence_count"]
        last = time.strftime("%H:%M UTC", time.gmtime(history["last_seen"]))
        lines.append(f"Seen: {count}x (last: {last})")

    hypotheses = result.get("hypotheses")
    if hypotheses:
        for i, h in enumerate(hypotheses[:3]):
            cause = h.get("cause", "")
            conf = h.get("confidence", 0.0)
            cmds = h.get("commands", [])
            marker = ">" if i == 0 else " "
            lines.append(f"\n{marker} **H{i+1} ({conf:.0%}):** {cause}")
            if cmds:
                lines.append("  ```")
                for cmd in cmds[:2]:
                    lines.append(f"  {cmd}")
                lines.append("  ```")
    else:
        cause = result.get("cause") or result.get("root_cause", "unknown")
        confidence = result.get("confidence", 0.0)
        commands = result.get("commands") or result.get("recommended_actions", [])

        lines.append(f"{cause}")
        lines.append(f"Confidence: {confidence:.0%}")

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

        if message.author.bot and message.author.id != self.bot.user.id and message.author.id != DISCLAUDE_BOT_ID:
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

        async with message.channel.typing():
            vault_ctx = vault.search(user_text)
            loki_ctx = await lokiquery.fetch_context(user_text, user_text)
            mem_facts = memory.search_facts(user_text)
            system = ASK_PROMPT + (f"\n\n{vault_ctx}" if vault_ctx else "")
            if mem_facts:
                facts_text = "\n".join(f"- [{r['topic']}] {r['content']}" for r in mem_facts)
                system += f"\n\nKnown facts from memory:\n{facts_text}"
            if sysstats.looks_like_sitrep_request(user_text):
                snapshot = await sysstats.gather_sitrep()
                if snapshot:
                    system += snapshot + sysstats.SITREP_ADDENDUM
            user_content = user_text + (f"\n\n{loki_ctx}" if loki_ctx else "")
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]
            answer = await llm.chat(messages)
        await message.reply(answer[:1990])

    async def _triage(self, message: discord.Message) -> None:
        content = message.content

        if _ABUSEIPDB_RE.search(content):
            return

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
        memory.record_occurrence(fp)
        auto_sup, auto_reason = memory.check_auto_suppress(fp)
        re_escalated_note = ""
        if not auto_sup and auto_reason.startswith("re-escalated"):
            re_escalated_note = f"⚠️ Previously auto-suppressed, but {auto_reason}\n\n"
        if auto_sup and not _is_infra_alert(content) and not _is_security_alert(content):
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

        history_check = memory.get_alert_history(fp)
        if history_check is None:
            system_prompt = TRIAGE_PROMPT_HYPOTHESIS
        else:
            system_prompt = TRIAGE_PROMPT_JSON
        if _UPDATE_RE.search(content):
            await message.reply("Routine security update — no action needed.")
            return

        label_match = re.search(r"\[([^\]]+)\]", content)
        alert_label = label_match.group(1) if label_match else ""

        async with message.channel.typing():
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

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

            try:
                result = await self._run_tool_loop(system_prompt, user_content)
            except Exception:
                answer = await llm.chat(messages)
                await message.reply(answer[:1990])
                memory.log_observation(event=content[:200], summary=answer[:500], host=alert_label)
                return

        if result.get("suppress"):
            memory.add_suppression(fp, reason=result.get("cause", "LLM-flagged noise"), expires=0)
            await message.reply(f"Suppressed (LLM-flagged): {result.get('cause', 'persistent noise')}")
            return

        hypotheses = result.get("hypotheses")
        primary_cause = result.get("cause") or result.get("root_cause", "")
        primary_confidence = result.get("confidence", 0.0)
        if hypotheses:
            top = hypotheses[0] if hypotheses else {}
            primary_cause = top.get("cause", "")
            primary_confidence = top.get("confidence", 0.0)

        history = memory.upsert_alert_history(
            fingerprint=fp,
            root_cause=primary_cause,
            confidence=primary_confidence,
            severity=result.get("severity", ""),
        )

        reply = re_escalated_note + _format_triage_reply(result, history)
        await message.reply(reply[:1990])

        memory.log_observation(
            event=content[:200],
            summary=primary_cause,
            host=alert_label,
            fingerprint=fp,
            root_cause=primary_cause,
            confidence=primary_confidence,
            severity=result.get("severity", ""),
        )

    async def _run_tool_loop(self, system_prompt: str, user_content: str) -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        for _ in range(4):
            response = await llm.chat_with_tools(messages, checks.TOOL_SCHEMA)
            tool_calls = response.get("tool_calls")
            if not tool_calls:
                content = response.get("content") or ""
                try:
                    return _parse_triage_json(content)
                except (json.JSONDecodeError, TypeError, ValueError):
                    return {"severity": "medium", "cause": content[:200], "confidence": 0.3,
                            "commands": [], "next_step": "manual review", "suppress": False}
            messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
            for call in tool_calls:
                call_id = call.get("id", "")
                try:
                    fn_name = call["function"]["name"]
                    fn_args = call["function"]["arguments"]
                    fn_args = fn_args if isinstance(fn_args, dict) else json.loads(fn_args)
                    print(f"[triage] tool call: {fn_name}({fn_args})", flush=True)
                    tool_result = await asyncio.to_thread(checks.dispatch, fn_name, fn_args)
                except (json.JSONDecodeError, TypeError, KeyError) as e:
                    tool_result = {"error": f"invalid arguments: {e}"}
                messages.append({
                    "role": "tool", "tool_call_id": call_id,
                    "content": json.dumps(tool_result)[:4000],
                })
        # Round 4 exhausted with no verdict — force a final answer, no tool access
        print("[triage] round cap reached, forcing final answer", flush=True)
        forced = await llm.chat_json(messages)
        if not isinstance(forced, dict):
            return {"severity": "medium", "cause": str(forced)[:200], "confidence": 0.3,
                    "commands": [], "next_step": "manual review", "suppress": False}
        return forced
