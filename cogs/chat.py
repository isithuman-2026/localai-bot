import discord
from discord.ext import commands
import llm

SYSTEM_PROMPT = (
    "You are JARVIS, a homelab AI assistant. Be concise: one to three sentences max. "
    "Lead with the answer. No preamble, no filler, no sign-offs. "
    "Use `backticks` for commands and paths. Use **bold** for hostnames and key values."
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


class ChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if self.bot.user not in message.mentions:
            return
        content = message.content
        for fmt in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
            content = content.replace(fmt, "")
        content = content.strip()
        if not content:
            await message.reply("Yes?")
            return
        async with message.channel.typing():
            await self._respond(message, content)

    async def _respond(self, message: discord.Message, user_text: str) -> None:
        channel_id = message.channel.id
        history = _history.get(channel_id, [])
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
            {"role": "user", "content": user_text}
        ]
        answer = await llm.chat(messages)
        history = history + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": answer},
        ]
        _history[channel_id] = _trim(history)
        await message.reply(answer[:1990])
