import os
import discord
from discord.ext import commands
import llm

ALERTS_CHANNEL_ID = int(os.environ.get("ALERTS_CHANNEL_ID", "1488857934061633697"))

TRIAGE_PROMPT = (
    "You are a homelab monitoring assistant. Analyse this alert and respond with:\n"
    "1. Severity: critical/high/medium/low\n"
    "2. Likely cause (one sentence)\n"
    "3. Recommended next step (one sentence)\n"
    "No preamble. No filler."
)

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
        messages = [
            {"role": "system", "content": TRIAGE_PROMPT},
            {"role": "user", "content": user_text},
        ]
        answer = await llm.chat(messages)
        await message.reply(answer[:1990])

    async def _triage(self, message: discord.Message) -> None:
        messages = [
            {"role": "system", "content": TRIAGE_PROMPT},
            {"role": "user", "content": f"Alert: {message.content}"},
        ]
        answer = await llm.chat(messages)
        await message.reply(answer[:1990])

