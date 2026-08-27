import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from discord.ext import commands
import discord
import os

os.environ.setdefault("ALERTS_CHANNEL_ID", "1488857934061633697")

from cogs.alerts import AlertsCog, DISCLAUDE_BOT_ID, TRIAGE_PROMPT_JSON, ASK_PROMPT, _format_triage_reply, TRIAGE_PROMPT_HYPOTHESIS


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


def _tool_response(result_dict):
    """A no-tool-call chat_with_tools response carrying the final JSON verdict."""
    return {"role": "assistant", "content": json.dumps(result_dict), "tool_calls": None}


@pytest.mark.asyncio
async def test_auto_triage_calls_chat_with_tools():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_TRIAGE_RESULT)) as mock_tools, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        mock_tools.assert_called_once()
        call_messages = mock_tools.call_args[0][0]
        assert call_messages[0]["role"] == "system"
        assert call_messages[0]["content"] == TRIAGE_PROMPT_JSON
        msg.reply.assert_called_once()


@pytest.mark.asyncio
async def test_ignores_disclaude_own_posts():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, content="Fixed the reply gate bug, redeploying now.")
    msg.author.id = DISCLAUDE_BOT_ID

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock) as mock_tools:
        await cog.on_message(msg)
        mock_tools.assert_not_called()
        msg.reply.assert_not_called()


@pytest.mark.asyncio
async def test_auto_triage_reply_contains_severity_and_cause():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_TRIAGE_RESULT)), \
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

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_TRIAGE_RESULT)), \
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

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock) as mock_tools:
        await cog.on_message(msg)
        mock_tools.assert_not_called()


@pytest.mark.asyncio
async def test_auto_triage_ignores_human_message():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=False)
    msg.mentions = []

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock) as mock_tools:
        await cog.on_message(msg)
        mock_tools.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_skips_already_seen_message():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)
    cog._seen.add(msg.id)

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock) as mock_tools:
        await cog.on_message(msg)
        mock_tools.assert_not_called()


@pytest.mark.asyncio
async def test_llm_suppress_flag_adds_suppression():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)
    suppress_result = {**FAKE_TRIAGE_RESULT, "suppress": True, "cause": "known tmdb noise"}

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(suppress_result)), \
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
         patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock) as mock_tools:
        await cog.on_message(msg)
        mock_tools.assert_not_called()
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
async def test_tool_loop_failure_falls_back_to_plain_chat():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock, side_effect=ValueError("bad response")), \
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

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock) as mock_tools:
        await cog.on_message(msg)
        mock_tools.assert_not_called()
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

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_TRIAGE_RESULT)), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(firing_msg)

    assert cog._active_alerts.get("Monitored Container Down") == 100

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock) as mock_tools:
        await cog.on_message(resolved_msg)
        mock_tools.assert_not_called()
        reply_text = resolved_msg.reply.call_args[0][0]
        assert "100" in reply_text
        assert "Monitored Container Down" not in cog._active_alerts


@pytest.mark.asyncio
async def test_grafana_firing_triages_and_tracks():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, content=GRAFANA_FIRING)
    msg.id = 55

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_TRIAGE_RESULT)), \
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
         patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_HYPOTHESIS_RESULT)) as mock_tools, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        call_messages = mock_tools.call_args_list[0][0][0]
        assert call_messages[0]["content"] == TRIAGE_PROMPT_HYPOTHESIS


@pytest.mark.asyncio
async def test_single_cause_prompt_used_for_known_fingerprint():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, content="container down alert")
    existing_history = {"fingerprint": "fp_known", "occurrence_count": 3, "last_seen": 1700000000}

    with patch("cogs.alerts.memory.get_alert_history", return_value=existing_history), \
         patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_TRIAGE_RESULT)) as mock_tools, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        call_messages = mock_tools.call_args_list[0][0][0]
        assert call_messages[0]["content"] == TRIAGE_PROMPT_JSON


@pytest.mark.asyncio
async def test_hypothesis_reply_shows_all_three():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True, content="container down alert")

    with patch("cogs.alerts.memory.get_alert_history", return_value=None), \
         patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_HYPOTHESIS_RESULT)), \
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


# --- adaptive tool loop tests ---

@pytest.mark.asyncio
async def test_triage_loop_dispatches_tool_call_then_answers():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    round_1 = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "docker_inspect", "arguments": '{"container":"homelab-vector"}'}}],
    }
    round_2 = {"role": "assistant",
               "content": '{"severity":"low","cause":"container healthy","confidence":0.9,"commands":[],"next_step":"none","suppress":false}'}

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock, side_effect=[round_1, round_2]) as mock_tools, \
         patch("cogs.alerts.checks.dispatch", return_value={"status": "running"}) as mock_dispatch, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)

    assert mock_tools.call_count == 2
    mock_dispatch.assert_called_once_with("docker_inspect", {"container": "homelab-vector"})
    msg.reply.assert_called_once()

    second_call_messages = mock_tools.call_args_list[1][0][0]
    assistant_tool_msg = second_call_messages[2]
    tool_result_msg = second_call_messages[3]
    assert assistant_tool_msg["role"] == "assistant"
    assert assistant_tool_msg["tool_calls"] == round_1["tool_calls"]
    assert tool_result_msg["role"] == "tool"
    assert tool_result_msg["tool_call_id"] == "call_1"


@pytest.mark.asyncio
async def test_triage_loop_stops_at_round_4():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    always_tool_call = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_x", "type": "function",
                         "function": {"name": "ping", "arguments": '{"host":"10.0.3.9"}'}}],
    }

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock, return_value=always_tool_call) as mock_tools, \
         patch("cogs.alerts.checks.dispatch", return_value={"reachable": True}), \
         patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock,
               return_value={"severity": "medium", "cause": "unresolved after 4 rounds", "confidence": 0.3, "commands": [], "next_step": "manual review", "suppress": False}) as mock_forced, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)

    assert mock_tools.call_count == 4
    mock_forced.assert_called_once()
    msg.reply.assert_called_once()


@pytest.mark.asyncio
async def test_triage_loop_tool_error_does_not_crash():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    round_1 = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "docker_inspect", "arguments": '{"container":"x"}'}}],
    }
    round_2 = {"role": "assistant",
               "content": '{"severity":"medium","cause":"could not verify","confidence":0.4,"commands":[],"next_step":"manual check","suppress":false}'}

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock, side_effect=[round_1, round_2]), \
         patch("cogs.alerts.checks.dispatch", return_value={"error": "unknown container"}), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)

    msg.reply.assert_called_once()
    reply_text = msg.reply.call_args[0][0]
    assert "could not verify" in reply_text.lower() or "MEDIUM" in reply_text


# --- remediation confirm-gate tests ---

FAKE_TRIAGE_WITH_REMEDIATION = {
    **FAKE_TRIAGE_RESULT,
    "remediation": {"tool": "restart_container", "args": {"container": "homelab-vector"}},
}


@pytest.mark.asyncio
async def test_triage_with_remediation_stores_pending_and_reacts():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    sent_message = MagicMock()
    sent_message.id = 9001
    sent_message.add_reaction = AsyncMock()
    msg.reply = AsyncMock(return_value=sent_message)

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_TRIAGE_WITH_REMEDIATION)), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)

    assert cog._pending_remediations[9001][0] == "restart_container"
    assert cog._pending_remediations[9001][1] == {"container": "homelab-vector"}
    sent_message.add_reaction.assert_called_once_with("👍")
    reply_text = msg.reply.call_args[0][0]
    assert "Remediation available" in reply_text
    assert "restart_container" in reply_text


FAKE_TRIAGE_WITH_AUTO_REMEDIATION = {
    **FAKE_TRIAGE_RESULT,
    "remediation": {"tool": "restart_container", "args": {"container": "monitoring-blackbox-exporter"}},
}


@pytest.mark.asyncio
async def test_triage_with_low_impact_remediation_auto_executes():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    sent_message = MagicMock()
    sent_message.id = 9003
    sent_message.add_reaction = AsyncMock()
    sent_message.reply = AsyncMock()
    msg.reply = AsyncMock(return_value=sent_message)

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_TRIAGE_WITH_AUTO_REMEDIATION)), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""), \
         patch("cogs.alerts.remediate.dispatch", return_value={"restarted": "monitoring-blackbox-exporter", "status": "running"}) as mock_dispatch:
        await cog.on_message(msg)

    mock_dispatch.assert_called_once_with("restart_container", {"container": "monitoring-blackbox-exporter"})
    sent_message.add_reaction.assert_not_called()
    assert cog._pending_remediations == {}
    sent_message.reply.assert_called_once()
    assert "Auto-remediated" in sent_message.reply.call_args[0][0]
    reply_text = msg.reply.call_args[0][0]
    assert "auto-remediating" in reply_text.lower()


@pytest.mark.asyncio
async def test_triage_without_remediation_does_not_react():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    sent_message = MagicMock()
    sent_message.id = 9002
    sent_message.add_reaction = AsyncMock()
    msg.reply = AsyncMock(return_value=sent_message)

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value=_tool_response(FAKE_TRIAGE_RESULT)), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)

    assert cog._pending_remediations == {}
    sent_message.add_reaction.assert_not_called()


def _make_reaction_payload(message_id=9001, channel_id=1488857934061633697, user_id=123, emoji="👍"):
    payload = MagicMock(spec=discord.RawReactionActionEvent)
    payload.message_id = message_id
    payload.channel_id = channel_id
    payload.user_id = user_id
    payload.emoji = emoji
    return payload


@pytest.mark.asyncio
async def test_reaction_confirms_and_runs_remediation():
    bot = make_bot()
    cog = AlertsCog(bot)
    cog._pending_remediations[9001] = ("restart_container", {"container": "homelab-vector"}, __import__("time").time())

    fetched_message = MagicMock()
    fetched_message.reply = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=fetched_message)
    bot.get_channel = MagicMock(return_value=channel)

    payload = _make_reaction_payload()
    with patch("cogs.alerts.remediate.dispatch", return_value={"restarted": "homelab-vector", "status": "running"}) as mock_dispatch:
        await cog.on_raw_reaction_add(payload)

    mock_dispatch.assert_called_once_with("restart_container", {"container": "homelab-vector"})
    fetched_message.reply.assert_called_once()
    assert "Ran" in fetched_message.reply.call_args[0][0]
    assert 9001 not in cog._pending_remediations


@pytest.mark.asyncio
async def test_reaction_ignores_wrong_emoji():
    bot = make_bot()
    cog = AlertsCog(bot)
    cog._pending_remediations[9001] = ("restart_container", {"container": "homelab-vector"}, __import__("time").time())

    payload = _make_reaction_payload(emoji="👎")
    with patch("cogs.alerts.remediate.dispatch") as mock_dispatch:
        await cog.on_raw_reaction_add(payload)

    mock_dispatch.assert_not_called()
    assert 9001 in cog._pending_remediations


@pytest.mark.asyncio
async def test_reaction_ignores_expired_pending():
    bot = make_bot()
    cog = AlertsCog(bot)
    cog._pending_remediations[9001] = (
        "restart_container", {"container": "homelab-vector"},
        __import__("time").time() - 700,
    )

    payload = _make_reaction_payload()
    with patch("cogs.alerts.remediate.dispatch") as mock_dispatch:
        await cog.on_raw_reaction_add(payload)

    mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_reaction_ignores_wrong_channel():
    bot = make_bot()
    cog = AlertsCog(bot)
    cog._pending_remediations[9001] = ("restart_container", {"container": "homelab-vector"}, __import__("time").time())

    payload = _make_reaction_payload(channel_id=1)
    with patch("cogs.alerts.remediate.dispatch") as mock_dispatch:
        await cog.on_raw_reaction_add(payload)

    mock_dispatch.assert_not_called()
    assert 9001 in cog._pending_remediations


@pytest.mark.asyncio
async def test_triage_loop_truncates_oversized_tool_result():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    round_1 = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "query_loki", "arguments": '{"logql":"{job=\\"x\\"}"}'}}],
    }
    round_2 = {"role": "assistant",
               "content": '{"severity":"low","cause":"noise","confidence":0.5,"commands":[],"next_step":"none","suppress":false}'}
    huge_result = {"data": "x" * 10000}

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock, side_effect=[round_1, round_2]) as mock_tools, \
         patch("cogs.alerts.checks.dispatch", return_value=huge_result), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)

    second_call_messages = mock_tools.call_args_list[1][0][0]
    tool_result_msg = second_call_messages[3]
    assert len(tool_result_msg["content"]) <= 4000


@pytest.mark.asyncio
async def test_triage_loop_round4_handles_non_dict_forced_answer():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    always_tool_call = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_x", "type": "function",
                         "function": {"name": "ping", "arguments": '{"host":"10.0.3.9"}'}}],
    }

    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock, return_value=always_tool_call), \
         patch("cogs.alerts.checks.dispatch", return_value={"reachable": True}), \
         patch("cogs.alerts.llm.chat_json", new_callable=AsyncMock, return_value="not a dict"), \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)

    msg.reply.assert_called_once()
