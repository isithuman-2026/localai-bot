import time
import pytest
from unittest.mock import patch
from pathlib import Path
import tempfile
import os


@pytest.fixture(autouse=True)
def tmp_db(tmp_path):
    """Redirect DB_PATH to a temp file for each test."""
    db = tmp_path / "test_memory.db"
    with patch("memory.DB_PATH", db):
        yield db


import memory


def test_write_and_search_fact():
    memory.write_fact("node1", "node1 runs Ubuntu 22.04 with 32GB RAM", source="manual")
    results = memory.search_facts("node1 Ubuntu")
    assert len(results) == 1
    assert "Ubuntu" in results[0]["content"]
    assert results[0]["topic"] == "node1"


def test_search_returns_empty_on_no_match():
    memory.write_fact("grafana", "Grafana runs on port 3000", source="manual")
    results = memory.search_facts("kubernetes")
    assert results == []


def test_search_limit():
    for i in range(10):
        memory.write_fact("service", f"service fact {i} node1", source="test")
    results = memory.search_facts("service node1", limit=3)
    assert len(results) <= 3


def test_log_and_retrieve_observation():
    obs_id = memory.log_observation(event="disk 95%", summary="Disk nearly full on /dev/sda1", host="node1")
    assert obs_id > 0
    recent = memory.recent_observations(limit=5)
    assert any(r["host"] == "node1" and "Disk" in r["summary"] for r in recent)


def test_is_suppressed_match():
    memory.add_suppression("tmdb timeout", reason="known flaky external API")
    suppressed, reason = memory.is_suppressed("Alert: tmdb timeout after 30s")
    assert suppressed is True
    assert "tmdb" in reason.lower() or "flaky" in reason.lower()


def test_is_suppressed_no_match():
    memory.add_suppression("tmdb timeout", reason="known flaky")
    suppressed, _ = memory.is_suppressed("disk usage 95% on node1")
    assert suppressed is False


def test_suppression_expired():
    past = int(time.time()) - 1
    memory.add_suppression("eth4 storm", reason="old issue", expires=past)
    suppressed, _ = memory.is_suppressed("eth4 storm detected")
    assert suppressed is False


def test_suppression_permanent():
    memory.add_suppression("NOTHING_NOTABLE", reason="noise filter", expires=0)
    suppressed, reason = memory.is_suppressed("🔴 NOTHING_NOTABLE in review")
    assert suppressed is True


def test_add_suppression_replace():
    memory.add_suppression("pattern_x", reason="first")
    memory.add_suppression("pattern_x", reason="updated")
    suppressed, reason = memory.is_suppressed("contains pattern_x here")
    assert suppressed is True
    assert reason == "updated"


def test_upsert_alert_history_creates_new():
    history = memory.upsert_alert_history("fp_abc123", "disk full", 0.9, "high")
    assert history["fingerprint"] == "fp_abc123"
    assert history["occurrence_count"] == 1
    assert history["last_root_cause"] == "disk full"
    assert history["last_confidence"] == 0.9
    assert history["last_severity"] == "high"
    assert history["auto_suppressed"] == 0


def test_upsert_alert_history_increments_count():
    memory.upsert_alert_history("fp_inc", "oom killer", 0.7, "medium")
    memory.upsert_alert_history("fp_inc", "oom killer", 0.8, "medium")
    history = memory.get_alert_history("fp_inc")
    assert history["occurrence_count"] == 2
    assert history["last_confidence"] == 0.8


def test_get_alert_history_returns_none_for_unknown():
    result = memory.get_alert_history("fp_notexist")
    assert result is None


def test_check_auto_suppress_not_triggered_below_threshold():
    for _ in range(3):
        memory.upsert_alert_history("fp_low_count", "tmdb timeout", 0.95, "low")
    suppressed, reason = memory.check_auto_suppress("fp_low_count", min_occurrences=5)
    assert suppressed is False
    assert reason == ""


def test_check_auto_suppress_triggers_at_threshold():
    for _ in range(5):
        memory.upsert_alert_history("fp_autosup", "tmdb 429", 0.88, "low")
    suppressed, reason = memory.check_auto_suppress("fp_autosup", min_occurrences=5, min_confidence=0.80)
    assert suppressed is True
    assert "5" in reason or "auto-suppressed" in reason.lower()


def test_check_auto_suppress_not_triggered_for_high_severity():
    for _ in range(10):
        memory.upsert_alert_history("fp_high_sev", "container down", 0.95, "high")
    suppressed, _ = memory.check_auto_suppress("fp_high_sev", min_occurrences=5)
    assert suppressed is False


def test_check_auto_suppress_already_suppressed():
    for _ in range(5):
        memory.upsert_alert_history("fp_already", "eth4 storm", 0.85, "low")
    memory.check_auto_suppress("fp_already", min_occurrences=5)
    suppressed, reason = memory.check_auto_suppress("fp_already", min_occurrences=5)
    assert suppressed is True


def test_log_observation_with_new_fields():
    obs_id = memory.log_observation(
        event="disk 95%",
        summary="Disk nearly full",
        host="node1",
        fingerprint="fp_xyz",
        root_cause="log rotation stopped",
        confidence=0.85,
        severity="high",
    )
    assert obs_id > 0
    recent = memory.recent_observations(limit=1)
    assert recent[0]["fingerprint"] == "fp_xyz"
    assert recent[0]["root_cause"] == "log rotation stopped"
    assert recent[0]["confidence"] == 0.85
    assert recent[0]["severity"] == "high"


def test_get_occurrence_rate_counts_recent_and_prior_windows(monkeypatch):
    day = 86400
    # 3 record_occurrence calls consume 3 time.time() values; get_occurrence_rate's "now" is the 4th
    times = iter([3 * day, 10 * day, 11 * day, 16 * day])
    monkeypatch.setattr(memory.time, "time", lambda: next(times))
    for _ in range(3):
        memory.record_occurrence("fp_rate")
    rates = memory.get_occurrence_rate("fp_rate", window_secs=7 * day)
    # now=16d; recent window (9d, 16d] -> occurrences at 10d, 11d
    # prior window (2d, 9d] -> occurrence at 3d
    assert rates["recent"] == 2
    assert rates["prior"] == 1


def test_check_rate_escalation_false_below_min_recent(monkeypatch):
    monkeypatch.setattr(memory.time, "time", lambda: 1_000_000)
    memory.record_occurrence("fp_quiet")
    escalate, reason = memory.check_rate_escalation("fp_quiet", min_recent=3)
    assert escalate is False
    assert reason == ""


def test_check_rate_escalation_true_when_no_prior_baseline(monkeypatch):
    monkeypatch.setattr(memory.time, "time", lambda: 1_000_000)
    for _ in range(3):
        memory.record_occurrence("fp_new_burst")
    escalate, reason = memory.check_rate_escalation("fp_new_burst", min_recent=3)
    assert escalate is True
    assert "rate increasing" in reason


def test_check_rate_escalation_true_when_rate_doubles(monkeypatch):
    day = 86400
    # 2 occurrences in the prior window, 4 in the recent window (4 >= 2*2.0) -> escalate
    times = iter([1 * day, 1 * day + 1, 8 * day, 8 * day + 1, 8 * day + 2, 8 * day + 3, 8 * day + 10])
    monkeypatch.setattr(memory.time, "time", lambda: next(times))
    for _ in range(6):
        memory.record_occurrence("fp_doubling")
    escalate, reason = memory.check_rate_escalation("fp_doubling", window_secs=7 * day, min_recent=3, multiplier=2.0)
    assert escalate is True
    assert "rate increasing" in reason


def test_check_rate_escalation_false_when_rate_steady(monkeypatch):
    day = 86400
    # 3 occurrences in the prior window, 3 in the recent window -> same rate, no escalation
    times = iter([3 * day, 4 * day, 5 * day, 10 * day, 11 * day, 12 * day, 16 * day])
    monkeypatch.setattr(memory.time, "time", lambda: next(times))
    for _ in range(6):
        memory.record_occurrence("fp_steady")
    escalate, reason = memory.check_rate_escalation("fp_steady", window_secs=7 * day, min_recent=3, multiplier=2.0)
    assert escalate is False


def test_check_auto_suppress_reescalates_when_rate_increases(monkeypatch):
    day = 86400
    t = {"now": 0}
    monkeypatch.setattr(memory.time, "time", lambda: t["now"])

    for _ in range(5):
        memory.upsert_alert_history("fp_reescalate", "tmdb timeout", 0.9, "low")
    memory.check_auto_suppress("fp_reescalate", min_occurrences=5)  # marks auto_suppressed
    history = memory.get_alert_history("fp_reescalate")
    assert history["auto_suppressed"] == 1

    # jump forward and record a burst of fresh occurrences with no prior-window baseline
    t["now"] = 8 * day
    for _ in range(3):
        memory.record_occurrence("fp_reescalate")

    suppressed, reason = memory.check_auto_suppress("fp_reescalate", min_occurrences=5)
    assert suppressed is False
    assert reason.startswith("re-escalated")

    history = memory.get_alert_history("fp_reescalate")
    assert history["auto_suppressed"] == 0


def test_check_auto_suppress_stays_suppressed_when_rate_flat(monkeypatch):
    monkeypatch.setattr(memory.time, "time", lambda: 1_000_000)
    for _ in range(5):
        memory.upsert_alert_history("fp_stable", "tmdb timeout", 0.9, "low")
    memory.check_auto_suppress("fp_stable", min_occurrences=5)
    memory.record_occurrence("fp_stable")

    suppressed, reason = memory.check_auto_suppress("fp_stable", min_occurrences=5)
    assert suppressed is True
    assert reason.startswith("auto-suppressed")
