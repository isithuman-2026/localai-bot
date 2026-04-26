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
Homelab: node1 (Ubuntu Server, 10.0.3.9), vault44 (Synology NAS, 10.0.0.44), Alpha60 (Synology NAS, 10.0.0.12), Oumuamua (UniFi UDR, 10.0.3.1).

Services on node1 (all Docker, /opt/ or ~/projects/):
- /opt/monitoring/  : Prometheus, Grafana (:3000), Loki, snmp_exporter, node-exporter, cAdvisor
- /opt/localai/     : LocalAI (Qwen2.5-14b), LiteLLM proxy
- /opt/infra/       : Traefik, socket-proxy
- /opt/homelab/     : Authelia, Portainer, Heimdall, Dozzle
- /opt/arr/         : Gluetun VPN, Sonarr, Radarr, Prowlarr, qBittorrent
- /opt/localai-bot/ : this bot
- ~/projects/homelab-monitor/ : homelab-vector (syslog+Docker logs→Loki), homelab-scripts (cron jobs)

Log locations:
- Docker containers  : docker logs <container> --since 30m
- Loki (all logs)    : Grafana Explore → LogQL, or curl http://monitoring-loki:3100
- journald (node1)   : journalctl -u <service> --since "30 min ago"
- fail2ban           : journalctl -u fail2ban --since "1h ago" | grep -E "Ban|Found|WARNING"
- fail2ban jail list : fail2ban-client status
- fail2ban banned IPs: fail2ban-client status <jailname>
- Authelia           : docker logs homelab-authelia --since 30m
- Synology logs      : DSM Log Center, or query Loki {source="syslog", host="vault44"}
- UDR logs           : UniFi admin UI → Insights → Threats, or Loki {source="syslog", host="Oumuamua"}

Useful investigation commands:
IMPORTANT: Oumuamua (UDR) is a hardware router at 10.0.3.1 — NOT a Docker container. Never suggest "docker logs" for UDR events.
IMPORTANT: vault44 and Alpha60 are Synology NAS hardware — not Docker containers.
Loki is only reachable inside the ai-agent-net Docker network; never suggest curl to Loki as a user command. Instead suggest Grafana Explore.

The user is investigating from their workstation, not already on node1. Always prefix node1 commands with: ssh boss@node1 (or provide as a block to paste after SSHing in).
For browser-based tools, give the full URL the user can open from their workstation.

Investigation commands — SSH to node1 first: ssh boss@node1
- fail2ban bans       : sudo fail2ban-client status sshd
- fail2ban recent     : sudo journalctl -u fail2ban --since "1h ago" | grep -E "Ban|Found|WARNING"
- Unban IP            : sudo fail2ban-client set sshd unbanip <ip>
- OOM kill            : dmesg | grep -i "killed process" | tail -20
- Container resources : docker stats --no-stream
- Active connections  : ss -tnp | grep ESTABLISHED
- Container logs      : docker logs <container> --since 30m
- Authelia logs       : docker logs homelab-authelia --since 30m 2>&1 | grep -iE "fail|ban|error"
- Node1 auth log      : sudo journalctl _COMM=sshd --since "1h ago" | tail -50

Browser tools (open from workstation):
- Grafana log search  : http://node1.local:3000/explore → select Loki datasource, filter by {source="syslog", host="vault44"} etc.
- Grafana dashboards  : http://node1.local:3000 (Synology Health, Docker, UniFi, Tailscale, GPU, Claude)
- Portainer           : http://node1.local:9000
- Dozzle (live logs)  : http://node1.local:8888
- UniFi threats       : UniFi admin UI → Insights → Threats
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

class AlertsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._seen: set[int] = set()

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

        # Auto-triage: bot/webhook message in alerts channel (skip own messages)
        if message.author.bot and message.author.id != self.bot.user.id:
            if message.id in self._seen:
                return
            self._seen.add(message.id)
            if len(self._seen) > 100:
                self._seen = set(list(self._seen)[-100:])
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

