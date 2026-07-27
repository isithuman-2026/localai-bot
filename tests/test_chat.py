import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from discord.ext import commands
import discord

from cogs.chat import ChatCog, SYSTEM_PROMPT, _trim, ALERTS_CHANNEL_ID


def make_bot():
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    return bot


def test_trim_no_op_under_10():
    h = [{"role": "user", "content": f"msg{i}"} for i in range(8)]
    result = _trim(h)
    assert len(result) == 8


def test_trim_compresses_at_10():
    h = []
    for i in range(12):
        h.append({"role": "user", "content": f"user msg {i}"})
        h.append({"role": "assistant", "content": f"bot reply {i}"})
    result = _trim(h)
    assert len(result) < len(h)
    assert result[0]["role"] == "system"
    assert "Earlier:" in result[0]["content"]


@pytest.mark.asyncio
async def test_on_message_ignores_bots():
    bot = make_bot()
    cog = ChatCog(bot)
    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = True
    with patch.object(cog, "_respond", new_callable=AsyncMock) as mock_respond:
        await cog.on_message(msg)
        mock_respond.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_ignores_no_mention():
    bot = make_bot()
    cog = ChatCog(bot)
    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = False
    msg.mentions = []
    with patch.object(cog, "_respond", new_callable=AsyncMock) as mock_respond:
        await cog.on_message(msg)
        mock_respond.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_ignores_alerts_channel_no_mention():
    bot = make_bot()
    cog = ChatCog(bot)
    bot_user = MagicMock()
    bot_user.id = 999
    bot.user = bot_user
    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = False
    msg.guild = MagicMock()
    msg.mentions = []
    msg.content = "what caused this?"
    msg.channel = MagicMock()
    msg.channel.id = ALERTS_CHANNEL_ID
    with patch.object(cog, "_respond", new_callable=AsyncMock) as mock_respond:
        await cog.on_message(msg)
        mock_respond.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_responds_to_mention_in_alerts_channel():
    bot = make_bot()
    cog = ChatCog(bot)
    bot_user = MagicMock()
    bot_user.id = 999
    bot.user = bot_user
    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = False
    msg.guild = MagicMock()
    msg.mentions = [bot_user]
    msg.content = "<@999> what caused this?"
    msg.channel = MagicMock()
    msg.channel.id = ALERTS_CHANNEL_ID
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    msg.channel.typing = MagicMock(return_value=cm)
    with patch.object(cog, "_respond", new_callable=AsyncMock) as mock_respond:
        await cog.on_message(msg)
        mock_respond.assert_called_once_with(msg, "what caused this?")


@pytest.mark.asyncio
async def test_on_message_responds_to_dm_without_mention():
    bot = make_bot()
    cog = ChatCog(bot)
    bot_user = MagicMock()
    bot_user.id = 999
    bot.user = bot_user
    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = False
    msg.guild = None
    msg.mentions = []
    msg.content = "what is the disk usage?"
    msg.channel = MagicMock()
    msg.channel.id = 222
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    msg.channel.typing = MagicMock(return_value=cm)
    with patch.object(cog, "_respond", new_callable=AsyncMock) as mock_respond:
        await cog.on_message(msg)
        mock_respond.assert_called_once_with(msg, "what is the disk usage?")


@pytest.mark.asyncio
async def test_on_message_responds_to_mention():
    bot = make_bot()
    cog = ChatCog(bot)
    bot_user = MagicMock()
    bot_user.id = 999
    bot.user = bot_user
    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = False
    msg.author.display_name = "alice"
    msg.mentions = [bot_user]
    msg.content = f"<@999> what is the disk usage?"
    msg.channel = MagicMock()
    msg.channel.id = 111
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    msg.channel.typing = MagicMock(return_value=cm)
    with patch.object(cog, "_respond", new_callable=AsyncMock) as mock_respond:
        await cog.on_message(msg)
        mock_respond.assert_called_once_with(msg, "what is the disk usage?")


@pytest.mark.asyncio
async def test_respond_calls_llm_and_replies():
    bot = make_bot()
    cog = ChatCog(bot)
    msg = MagicMock(spec=discord.Message)
    msg.channel = MagicMock()
    msg.channel.id = 42
    msg.reply = AsyncMock()
    with patch("cogs.chat.llm.chat", new_callable=AsyncMock, return_value="server01 disk at 90%") as mock_chat:
        await cog._respond(msg, "disk usage?")
        mock_chat.assert_called_once()
        call_args = mock_chat.call_args[0][0]
        assert call_args[0]["role"] == "system"
        assert call_args[0]["content"] == SYSTEM_PROMPT
        assert call_args[-1]["role"] == "user"
        assert call_args[-1]["content"] == "disk usage?"
        msg.reply.assert_called_once_with("server01 disk at 90%")


@pytest.mark.asyncio
async def test_respond_injects_live_snapshot_for_sitrep():
    bot = make_bot()
    cog = ChatCog(bot)
    msg = MagicMock(spec=discord.Message)
    msg.channel = MagicMock()
    msg.channel.id = 42
    msg.reply = AsyncMock()
    with patch("cogs.chat.llm.chat", new_callable=AsyncMock, return_value="all healthy") as mock_chat, \
         patch("cogs.chat.sysstats.gather_sitrep", new_callable=AsyncMock, return_value="**Live snapshot:** 40 containers"):
        await cog._respond(msg, "give me a sitrep")
        call_args = mock_chat.call_args[0][0]
        assert "40 containers" in call_args[0]["content"]
