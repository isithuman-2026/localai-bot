import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from discord.ext import commands
import discord
import os

os.environ.setdefault("ALERTS_CHANNEL_ID", "1488857934061633697")

from cogs.alerts import AlertsCog, TRIAGE_PROMPT


def make_bot():
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    bot_user = MagicMock()
    bot_user.id = 999
    bot.user = bot_user
    return bot


def make_alert_message(bot_authored=True, content="ERROR: disk usage 95%", channel_id=1488857934061633697):
    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = bot_authored
    msg.author.id = 777 if bot_authored else 123
    msg.content = content
    msg.channel = MagicMock()
    msg.channel.id = channel_id
    msg.id = 42
    msg.mentions = []

    async def fake_history(limit=5):
        yield msg
    msg.channel.history = fake_history
    msg.reply = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_auto_triage_bot_message_in_alerts_channel():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    with patch("cogs.alerts.llm.chat", new_callable=AsyncMock, return_value="High. Disk filling. Run `df -h` and clear logs.") as mock_chat:
        await cog.on_message(msg)
        mock_chat.assert_called_once()
        call_messages = mock_chat.call_args[0][0]
        assert call_messages[0]["role"] == "system"
        assert call_messages[0]["content"] == TRIAGE_PROMPT
        msg.reply.assert_called_once_with("High. Disk filling. Run `df -h` and clear logs.")


@pytest.mark.asyncio
async def test_auto_triage_ignores_wrong_channel():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, channel_id=9999999)

    with patch("cogs.alerts.llm.chat", new_callable=AsyncMock) as mock_chat:
        await cog.on_message(msg)
        mock_chat.assert_not_called()


@pytest.mark.asyncio
async def test_auto_triage_ignores_human_message():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=False)
    msg.mentions = []

    with patch("cogs.alerts.llm.chat", new_callable=AsyncMock) as mock_chat:
        await cog.on_message(msg)
        mock_chat.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_skips_already_seen_message():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)
    cog._seen.add(msg.id)

    with patch("cogs.alerts.llm.chat", new_callable=AsyncMock) as mock_chat:
        await cog.on_message(msg)
        mock_chat.assert_not_called()


@pytest.mark.asyncio
async def test_mention_in_alerts_channel_responds():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=False)
    msg.mentions = [bot.user]
    msg.content = "<@999> what caused this?"
    msg.author.bot = False

    with patch("cogs.alerts.llm.chat", new_callable=AsyncMock, return_value="LVM snapshot overflow.") as mock_chat:
        await cog.on_message(msg)
        mock_chat.assert_called_once()
        msg.reply.assert_called_once_with("LVM snapshot overflow.")
