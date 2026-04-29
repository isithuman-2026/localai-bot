import os
import re
import discord
from discord.ext import commands
import llm
import lokiquery
import vulnlookup
import vault

ALERTS_CHANNEL_ID = int(os.environ.get("ALERTS_CHANNEL_ID", "1488857934061633697"))

HOMELAB_CONTEXT = """
IMPORTANT CONSTRAINTS — always follow regardless of other context:
- Oumuamua (UDR) is a hardware router — NEVER suggest "docker logs" for UDR events.
- vault44 and Alpha60 are Synology NAS hardware — NEVER suggest "docker logs" for them.
- Loki is only reachable inside the Docker network — NEVER suggest curl to Loki. Use Grafana Explore instead.
- The user is on their workstation, not on node1. Always prefix node1 shell commands with: ssh boss@node1

"""

TRIAGE_PROMPT = (
    "You are a homelab security and ops assistant. You have full knowledge of this homelab's topology and services.\n\n"
    + HOMELAB_CONTEXT
    + "\nAnalyse the alert and respond with:\n"
    "1. Severity: critical/high/medium/low\n"
    "2. Likely cause (one sentence)\n"
    "3. Investigation: 2-3 exact commands to run on node1 to diagnose further\n"
    "4. Next step: one concrete action to take\n"
    "No preamble. No filler. Commands must be copy-pasteable."
)

TRIAGE_PROMPT_VULN = (
    "You are a homelab security assistant. You have full knowledge of this homelab's topology and services.\n\n"
    + HOMELAB_CONTEXT
    + "\nSecurity packages were just auto-installed on node1. Vuln context is provided below the alert.\n"
    "Respond with:\n"
    "1. Severity: critical/high/medium/low\n"
    "2. Most critical CVE (if any) in one sentence\n"
    "3. Investigation: exact command to confirm patch is applied\n"
    "4. Next step: one concrete action\n"
    "No preamble. No filler."
)

_UPDATE_RE = re.compile(r"security updates installed", re.IGNORECASE)
_CODE_BLOCK_RE = re.compile(r"```\s*(.*?)\s*```", re.DOTALL)
_GRAFANA_RESOLVED_RE = re.compile(r"^\s*Resolved\s*$", re.MULTILINE)
_GRAFANA_FIRING_RE = re.compile(r"^\s*Firing\s*$", re.MULTILINE)
_ALERTNAME_RE = re.compile(r"alertname\s*=\s*(.+)")

class AlertsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._seen: set[int] = set()
        self._active_alerts: dict[str, int] = {}  # alertname -> firing message_id

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != ALERTS_CHANNEL_ID:
            return

        # @mention in alerts channel — direct question, no history
        if not message.author.bot and self.bot.user in message.mentions:
            content = message.content
            for fmt in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
                content = content.replace(fmt, "")
            content = content.strip()
            if content:
                await self._ask(message, content)
            return

        # Auto-triage: bot/webhook message in alerts channel (skip own messages and resolved notifications)
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
        vault_ctx = vault.search(user_text)
        loki_ctx = await lokiquery.fetch_context(user_text, user_text)
        system = TRIAGE_PROMPT + (f"\n\n{vault_ctx}" if vault_ctx else "")
        user_content = user_text + (f"\n\n{loki_ctx}" if loki_ctx else "")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        answer = await llm.chat(messages)
        await message.reply(answer[:1990])

    async def _triage(self, message: discord.Message) -> None:
        content = message.content
        system_prompt = TRIAGE_PROMPT
        vuln_context = ""

        if _UPDATE_RE.search(content):
            match = _CODE_BLOCK_RE.search(content)
            if match:
                packages = match.group(1).split()
                if packages:
                    vuln_context = await vulnlookup.enrich_packages(packages)
                    if vuln_context:
                        system_prompt = TRIAGE_PROMPT_VULN

        # Extract alert label from message format "**Security Alert [label]**"
        label_match = re.search(r"\[([^\]]+)\]", content)
        alert_label = label_match.group(1) if label_match else ""

        loki_ctx = await lokiquery.fetch_context(alert_label, content)
        vault_ctx = vault.search(content)
        if vault_ctx:
            system_prompt += f"\n\n{vault_ctx}"

        user_content = f"Alert: {content}"
        if loki_ctx:
            user_content += f"\n\n{loki_ctx}"
        if vuln_context:
            user_content += f"\n\n{vuln_context}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        answer = await llm.chat(messages)
        await message.reply(answer[:1990])

