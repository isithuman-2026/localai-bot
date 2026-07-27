import os
import discord
from discord.ext import commands
import llm
import memory
import sysstats

ALERTS_CHANNEL_ID = int(os.environ.get("ALERTS_CHANNEL_ID", "1488857934061633697"))

SYSTEM_PROMPT = (
    "You are JARVIS, a homelab AI assistant. Be concise: one to three sentences max. "
    "Lead with the answer. No preamble, no filler, no sign-offs. "
    "Use `backticks` for commands and paths. Use **bold** for hostnames and key values."
)

_history: dict[int, list[dict]] = {}
_pending_remember: dict[int, dict] = {}  # bot_message_id -> {user_text, answer}

_CORRECTION_PHRASES = (
    "actually", "that's wrong", "thats wrong", "that is wrong",
    "incorrect", "not right", "you're wrong", "youre wrong",
    "it's not", "its not", "only ", "no,", "no it", "wrong,",
)

SAVE_EMOJI = "💾"

EXTRACT_FACT_PROMPT = (
    "Extract a single homelab fact from this exchange. "
    "Reply with JSON only: {\"topic\": \"short label\", \"content\": \"full fact sentence\"}. "
    "The fact should reflect the user's correction, not the assistant's wrong answer."
)


def _looks_like_correction(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in _CORRECTION_PHRASES)


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
        is_dm = message.guild is None
        mentioned = self.bot.user in message.mentions
        if not mentioned and not is_dm:
            return
        if message.channel.id == ALERTS_CHANNEL_ID and not mentioned:
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
        facts = memory.search_facts(user_text, limit=5)
        system_parts = [SYSTEM_PROMPT]
        if facts:
            facts_block = "\n".join(f"- [{f['topic']}] {f['content']}" for f in facts)
            system_parts.append(f"Relevant homelab facts (treat as ground truth):\n{facts_block}")
        if sysstats.looks_like_sitrep_request(user_text):
            snapshot = await sysstats.gather_sitrep()
            if snapshot:
                system_parts.append(snapshot + sysstats.SITREP_ADDENDUM)
        system_content = "\n\n".join(system_parts)
        messages = [{"role": "system", "content": system_content}] + history + [
            {"role": "user", "content": user_text}
        ]
        answer = await llm.chat(messages)
        history = history + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": answer},
        ]
        _history[channel_id] = _trim(history)
        bot_msg = await message.reply(answer[:1990])
        if _looks_like_correction(user_text):
            await bot_msg.add_reaction(SAVE_EMOJI)
            _pending_remember[bot_msg.id] = {"user_text": user_text, "answer": answer}

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if str(payload.emoji) != SAVE_EMOJI:
            return
        if payload.user_id == self.bot.user.id:
            return
        pending = _pending_remember.pop(payload.message_id, None)
        if pending is None:
            return
        exchange = (
            f"User: {pending['user_text']}\n"
            f"Assistant: {pending['answer']}"
        )
        result = await llm.chat_json([
            {"role": "system", "content": EXTRACT_FACT_PROMPT},
            {"role": "user", "content": exchange},
        ])
        topic = (result.get("topic") or "").strip()
        content = (result.get("content") or "").strip()
        if not topic or not content:
            channel = self.bot.get_channel(payload.channel_id)
            if channel:
                await channel.send("Couldn't extract a fact from that exchange. Use `!remember <topic>: <content>` to save manually.")
            return
        memory.write_fact(topic, content, source="auto-correction")
        channel = self.bot.get_channel(payload.channel_id)
        if channel:
            await channel.send(f"Saved: **{topic}** — {content}")

    @commands.command(name="remember")
    async def remember(self, ctx: commands.Context, *, text: str) -> None:
        """Store a fact: !remember <topic>: <content>"""
        if ":" not in text:
            await ctx.reply("Format: `!remember <topic>: <content>`")
            return
        topic, _, content = text.partition(":")
        topic = topic.strip()
        content = content.strip()
        if not topic or not content:
            await ctx.reply("Format: `!remember <topic>: <content>`")
            return
        memory.write_fact(topic, content, source="user")
        await ctx.reply(f"Got it. Stored under **{topic}**.")
