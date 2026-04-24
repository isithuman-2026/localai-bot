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

_history: dict[int, list[dict]] = {}


def _trim(h: list[dict]) -> list[dict]:
    if len(h) <= 10:
        return h
    oldest = h[:4]
    summary = "Earlier: " + " | ".join(
        f"{m['role']}: {m['content'][:80]}" for m in oldest
    )
    return [{"role": "system", "content": summary}] + h[4:]


class AlertsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._seen: set[int] = set()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != ALERTS_CHANNEL_ID:
            return

        # @mention in alerts channel — chat response with channel history
        if not message.author.bot and self.bot.user in message.mentions:
            content = message.content
            for fmt in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
                content = content.replace(fmt, "")
            content = content.strip()
            if content:
                await self._chat_respond(message, content)
            return

        # Auto-triage: bot/webhook message in alerts channel
        if message.author.bot:
            if message.id in self._seen:
                return
            self._seen.add(message.id)
            if len(self._seen) > 100:
                self._seen = set(list(self._seen)[-100:])
            await self._triage(message)

    async def _triage(self, message: discord.Message) -> None:
        recent = []
        async for m in message.channel.history(limit=5):
            recent.append({"role": "assistant" if m.author.bot else "user", "content": m.content})
        recent.reverse()

        messages = [
            {"role": "system", "content": TRIAGE_PROMPT},
        ] + recent + [
            {"role": "user", "content": f"Alert: {message.content}"},
        ]
        answer = await llm.chat(messages)
        await message.reply(answer[:1990])

    async def _chat_respond(self, message: discord.Message, user_text: str) -> None:
        channel_id = message.channel.id
        history = _history.get(channel_id, [])
        messages = [{"role": "system", "content": TRIAGE_PROMPT}] + history + [
            {"role": "user", "content": user_text}
        ]
        answer = await llm.chat(messages)
        history = history + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": answer},
        ]
        _history[channel_id] = _trim(history)
        await message.reply(answer[:1990])
