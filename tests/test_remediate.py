from unittest.mock import MagicMock, patch

import remediate


def test_restart_container_rejects_container_not_in_allowlist():
    result = remediate.restart_container("traefik")
    assert "error" in result


def test_restart_container_allows_known_container():
    fake_container = MagicMock()
    fake_container.status = "running"
    mock_client = MagicMock()
    mock_client.containers.get.return_value = fake_container
    with patch("remediate._client", return_value=mock_client):
        result = remediate.restart_container("homelab-vector")
    fake_container.restart.assert_called_once_with(timeout=10)
    assert result == {"restarted": "homelab-vector", "status": "running"}


def test_prune_old_logs_reports_missing_mount():
    with patch("remediate.LOGS_DIR") as mock_dir:
        mock_dir.is_dir.return_value = False
        result = remediate.prune_old_logs()
    assert "error" in result


def test_prune_old_logs_deletes_old_files_only(tmp_path):
    old_file = tmp_path / "old.json"
    old_file.write_text("{}")
    new_file = tmp_path / "new.json"
    new_file.write_text("{}")

    import os
    import time
    old_ts = time.time() - (remediate.LOG_RETENTION_DAYS + 1) * 86400
    os.utime(old_file, (old_ts, old_ts))

    with patch("remediate.LOGS_DIR", tmp_path):
        result = remediate.prune_old_logs()

    assert result["deleted_count"] == 1
    assert "old.json" in result["deleted"]
    assert not old_file.exists()
    assert new_file.exists()


def test_dispatch_calls_known_remediation():
    mock_fn = MagicMock(return_value={"restarted": "homelab-vector", "status": "running"})
    with patch.dict(remediate._DISPATCH_TABLE, {"restart_container": mock_fn}):
        result = remediate.dispatch("restart_container", {"container": "homelab-vector"})
    mock_fn.assert_called_once_with(container="homelab-vector")
    assert result["status"] == "running"


def test_dispatch_returns_error_for_unknown_tool():
    result = remediate.dispatch("delete_everything", {})
    assert "error" in result


def test_dispatch_catches_exceptions():
    mock_fn = MagicMock(side_effect=RuntimeError("boom"))
    with patch.dict(remediate._DISPATCH_TABLE, {"restart_container": mock_fn}):
        result = remediate.dispatch("restart_container", {"container": "x"})
    assert "error" in result
    assert "boom" in result["error"]


def test_requires_confirmation_false_for_always_auto_tool():
    assert remediate.requires_confirmation("prune_old_logs", {}) is False


def test_requires_confirmation_false_for_auto_tier_container():
    assert remediate.requires_confirmation("restart_container", {"container": "monitoring-blackbox-exporter"}) is False


def test_requires_confirmation_true_for_confirm_tier_container():
    assert remediate.requires_confirmation("restart_container", {"container": "homelab-vector"}) is True


def test_requires_confirmation_true_for_unknown_container():
    assert remediate.requires_confirmation("restart_container", {"container": "not-a-container"}) is True


def test_remediate_tool_schema_names_match_dispatch_table():
    schema_names = {t["function"]["name"] for t in remediate.REMEDIATE_TOOL_SCHEMA}
    assert schema_names == set(remediate._DISPATCH_TABLE.keys())
