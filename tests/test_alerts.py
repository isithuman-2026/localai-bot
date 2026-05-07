import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from discord.ext import commands
import discord
import os

os.environ.setdefault("ALERTS_CHANNEL_ID", "1488857934061633697")

from cogs.alerts import AlertsCog, TRIAGE_PROMPT_JSON, ASK_PROMPT, _format_triage_reply, VERIFICATION_PROMPT_JSON, TRIAGE_PROMPT_HYPOTHESIS


@pytest.fixture(autouse=True)
def mock_memory():
    with patch("cogs.alerts.memory.search_facts", return_value=[]), \
         patch("cogs.alerts.memory.is_suppressed", return_value=(False, "")), \
         patch("cogs.alerts.memory.log_observation", return_value=1), \
         patch("cogs.alerts.memory.check_auto_suppress", return_value=(False, "")), \
         patch("cogs.alerts.memory.get_alert_history", return_value={
             "fingerprint": "fp_test",
             "occurrence_count": 1,
             "last_seen": 1700000000,
             "auto_suppressed": 0,
         }), \
         patch("cogs.alerts.memory.upsert_alert_history", return_value={
             "fingerprint": "fp_test",
             "occurrence_count": 1,
             "last_seen": 1700000000,
             "auto_suppressed": 0,
         }):
        yield


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


FAKE_TRIAGE_RESULT = {
    "severity": "high",
    "cause": "Disk filling due to log accumulation",
    "confidence": 0.85,
    "commands": ["ssh boss@node1 df -h", "ssh boss@node1 du -sh /var/log"],
    "next_step": "Rotate and clear old logs",
    "suppress": False,
}


@pytest.mark.asyncio
async def test_auto_triage_calls_chat_json():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=FAKE_TRIAGE_RESULT) as mock_json, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        mock_json.assert_called_once()
        call_messages = mock_json.call_args[0][0]
        assert call_messages[0]["role"] == "system"
        assert call_messages[0]["content"] == TRIAGE_PROMPT_JSON
        msg.reply.assert_called_once()


@pytest.mark.asyncio
async def test_auto_triage_reply_contains_severity_and_cause():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=FAKE_TRIAGE_RESULT), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        reply_text = msg.reply.call_args[0][0]
        assert "HIGH" in reply_text
        assert "Disk filling due to log accumulation" in reply_text
        assert "85%" in reply_text


@pytest.mark.asyncio
async def test_auto_triage_reply_contains_commands():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=FAKE_TRIAGE_RESULT), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        reply_text = msg.reply.call_args[0][0]
        assert "ssh boss@node1 df -h" in reply_text


@pytest.mark.asyncio
async def test_auto_triage_ignores_wrong_channel():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, channel_id=9999999)

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock) as mock_json:
        await cog.on_message(msg)
        mock_json.assert_not_called()


@pytest.mark.asyncio
async def test_auto_triage_ignores_human_message():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=False)
    msg.mentions = []

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock) as mock_json:
        await cog.on_message(msg)
        mock_json.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_skips_already_seen_message():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)
    cog._seen.add(msg.id)

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock) as mock_json:
        await cog.on_message(msg)
        mock_json.assert_not_called()


@pytest.mark.asyncio
async def test_llm_suppress_flag_adds_suppression():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)
    suppress_result = {**FAKE_TRIAGE_RESULT, "suppress": True, "cause": "known tmdb noise"}

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=suppress_result), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""), \
         patch("cogs.alerts.memory.add_suppression") as mock_add_sup:
        await cog.on_message(msg)
        mock_add_sup.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        assert "Suppressed" in reply_text


@pytest.mark.asyncio
async def test_auto_suppress_from_history_skips_triage():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    with patch("cogs.alerts.memory.check_auto_suppress", return_value=(True, "auto-suppressed after 5 occurrences (low, 90% confidence)")), \
         patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock) as mock_json:
        await cog.on_message(msg)
        mock_json.assert_not_called()
        reply_text = msg.reply.call_args[0][0]
        assert "Auto-suppressed" in reply_text


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
        call_messages = mock_chat.call_args[0][0]
        assert call_messages[0]["content"] == ASK_PROMPT
        msg.reply.assert_called_once_with("LVM snapshot overflow.")


@pytest.mark.asyncio
async def test_chat_json_failure_falls_back_to_plain_chat():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, side_effect=ValueError("bad json")), \
         patch("cogs.alerts.llm.chat", new_callable=AsyncMock, return_value="fallback plain text") as mock_plain, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        mock_plain.assert_called_once()
        msg.reply.assert_called_once_with("fallback plain text")


GRAFANA_FIRING = (
    "Firing\n\nValue: C0=1\nLabels:\n\nalertname = Monitored Container Down\n"
    "grafana_folder = Homelab Alerts\nseverity = critical\n"
)

GRAFANA_RESOLVED = (
    "Resolved\n\nValue: C0=-1\nLabels:\n\nalertname = Monitored Container Down\n"
    "grafana_folder = Homelab Alerts\nseverity = critical\ngrafana_state_reason = NoData\n"
)


@pytest.mark.asyncio
async def test_grafana_resolved_skips_triage():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, content=GRAFANA_RESOLVED)

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock) as mock_json:
        await cog.on_message(msg)
        mock_json.assert_not_called()
        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        assert "Resolved" in reply_text
        assert "no action needed" in reply_text


@pytest.mark.asyncio
async def test_grafana_resolved_correlates_to_firing():
    bot = make_bot()
    cog = AlertsCog(bot)

    firing_msg = make_alert_message(bot_authored=True, content=GRAFANA_FIRING)
    firing_msg.id = 100

    resolved_msg = make_alert_message(bot_authored=True, content=GRAFANA_RESOLVED)
    resolved_msg.id = 101

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=FAKE_TRIAGE_RESULT), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(firing_msg)

    assert cog._active_alerts.get("Monitored Container Down") == 100

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock) as mock_json:
        await cog.on_message(resolved_msg)
        mock_json.assert_not_called()
        reply_text = resolved_msg.reply.call_args[0][0]
        assert "100" in reply_text
        assert "Monitored Container Down" not in cog._active_alerts


@pytest.mark.asyncio
async def test_grafana_firing_triages_and_tracks():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, content=GRAFANA_FIRING)
    msg.id = 55

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=FAKE_TRIAGE_RESULT), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)

    assert cog._active_alerts.get("Monitored Container Down") == 55


# --- _format_triage_reply unit tests ---

def test_format_triage_reply_basic():
    result = {
        "severity": "high",
        "cause": "disk nearly full",
        "confidence": 0.9,
        "commands": ["ssh boss@node1 df -h"],
        "next_step": "clear /var/log",
    }
    reply = _format_triage_reply(result)
    assert "**[HIGH]**" in reply
    assert "disk nearly full" in reply
    assert "90%" in reply
    assert "ssh boss@node1 df -h" in reply
    assert "clear /var/log" in reply


def test_format_triage_reply_shows_occurrence_count():
    result = {"severity": "low", "cause": "tmdb rate limit", "confidence": 0.8, "commands": [], "next_step": ""}
    history = {"occurrence_count": 4, "last_seen": 1700000000, "auto_suppressed": 0}
    reply = _format_triage_reply(result, history)
    assert "4x" in reply


def test_format_triage_reply_no_commands():
    result = {"severity": "low", "cause": "noise", "confidence": 0.5, "commands": [], "next_step": ""}
    reply = _format_triage_reply(result)
    assert "**[LOW]**" in reply
    assert "noise" in reply
    assert "```" not in reply


def test_format_triage_reply_caps_commands_at_three():
    result = {
        "severity": "medium",
        "cause": "test",
        "confidence": 0.7,
        "commands": ["cmd1", "cmd2", "cmd3", "cmd4", "cmd5"],
        "next_step": "",
    }
    reply = _format_triage_reply(result)
    assert "cmd4" not in reply
    assert "cmd3" in reply


FAKE_LOW_CONFIDENCE_RESULT = {
    "severity": "medium",
    "cause": "Unknown network issue",
    "confidence": 0.45,
    "commands": ["ssh boss@node1 ip link show"],
    "next_step": "Investigate network interface",
    "suppress": False,
}

FAKE_VERIFIED_RESULT = {
    "root_cause": "eth4 packet loss due to driver bug",
    "confidence": 0.82,
    "evidence": ["kernel: eth4: tx timeout", "kernel: eth4: reset adapter"],
    "recommended_actions": ["ssh boss@node1 dmesg | grep eth4"],
    "severity": "medium",
}


@pytest.mark.asyncio
async def test_verification_pass_triggered_on_low_confidence():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)
    loki_logs = "Recent logs from Loki:\n```\nkernel: eth4: tx timeout\n```"

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, side_effect=[FAKE_LOW_CONFIDENCE_RESULT, FAKE_VERIFIED_RESULT]) as mock_json, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=loki_logs), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        assert mock_json.call_count == 2
        second_call_messages = mock_json.call_args_list[1][0][0]
        assert second_call_messages[0]["content"] == VERIFICATION_PROMPT_JSON
        reply_text = msg.reply.call_args[0][0]
        assert "eth4 packet loss due to driver bug" in reply_text
        assert "eth4: tx timeout" in reply_text


@pytest.mark.asyncio
async def test_verification_pass_skipped_when_no_loki_context():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=FAKE_LOW_CONFIDENCE_RESULT) as mock_json, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        assert mock_json.call_count == 1


@pytest.mark.asyncio
async def test_verification_pass_skipped_on_high_confidence():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)
    loki_logs = "Recent logs from Loki:\n```\nsomething\n```"

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=FAKE_TRIAGE_RESULT) as mock_json, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=loki_logs), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        assert mock_json.call_count == 1


@pytest.mark.asyncio
async def test_verification_pass_failure_falls_back_to_first_result():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)
    loki_logs = "Recent logs from Loki:\n```\nsome log\n```"

    with patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, side_effect=[FAKE_LOW_CONFIDENCE_RESULT, ValueError("bad json")]), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=loki_logs), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        reply_text = msg.reply.call_args[0][0]
        assert "Unknown network issue" in reply_text


FAKE_HYPOTHESIS_RESULT = {
    "severity": "high",
    "hypotheses": [
        {"cause": "OOM killer terminated container", "confidence": 0.75, "commands": ["ssh boss@node1 dmesg | grep -i 'oom\\|kill'"]},
        {"cause": "Docker daemon crash", "confidence": 0.55, "commands": ["ssh boss@node1 journalctl -u docker --since '30min ago'"]},
        {"cause": "Host kernel panic", "confidence": 0.20, "commands": ["ssh boss@node1 last -x | head -10"]},
    ],
    "next_step": "Check dmesg first",
    "suppress": False,
}


@pytest.mark.asyncio
async def test_hypothesis_prompt_used_for_new_fingerprint():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, content="container down alert")

    with patch("cogs.alerts.memory.get_alert_history", return_value=None), \
         patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=FAKE_HYPOTHESIS_RESULT) as mock_json, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        call_messages = mock_json.call_args_list[0][0][0]
        assert call_messages[0]["content"] == TRIAGE_PROMPT_HYPOTHESIS


@pytest.mark.asyncio
async def test_single_cause_prompt_used_for_known_fingerprint():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, content="container down alert")
    existing_history = {"fingerprint": "fp_known", "occurrence_count": 3, "last_seen": 1700000000}

    with patch("cogs.alerts.memory.get_alert_history", return_value=existing_history), \
         patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=FAKE_TRIAGE_RESULT) as mock_json, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        call_messages = mock_json.call_args_list[0][0][0]
        assert call_messages[0]["content"] == TRIAGE_PROMPT_JSON


@pytest.mark.asyncio
async def test_hypothesis_reply_shows_all_three():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, content="container down alert")

    with patch("cogs.alerts.memory.get_alert_history", return_value=None), \
         patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value=FAKE_HYPOTHESIS_RESULT), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        reply_text = msg.reply.call_args[0][0]
        assert "OOM killer" in reply_text
        assert "Docker daemon crash" in reply_text
        assert "Host kernel panic" in reply_text
        assert "75%" in reply_text


def test_format_triage_reply_renders_hypotheses():
    result = FAKE_HYPOTHESIS_RESULT
    reply = _format_triage_reply(result)
    assert "**[HIGH]**" in reply
    assert "OOM killer" in reply
    assert "75%" in reply
    assert "Docker daemon crash" in reply
    assert "dmesg | grep" in reply
