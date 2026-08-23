import pytest
from unittest.mock import MagicMock, patch

import checks


def test_docker_inspect_rejects_unknown_container():
    mock_client = MagicMock()
    mock_client.containers.list.return_value = [MagicMock(name="homelab-vector")]
    with patch("checks.docker.from_env", return_value=mock_client):
        result = checks.docker_inspect("not-a-real-container")
    assert "error" in result
    assert "unknown container" in result["error"].lower()


def test_docker_inspect_returns_status_for_known_container():
    fake_container = MagicMock()
    fake_container.name = "homelab-vector"
    fake_container.status = "running"
    fake_container.attrs = {"RestartCount": 0, "State": {"ExitCode": 0}}

    mock_client = MagicMock()
    mock_client.containers.list.return_value = [fake_container]
    mock_client.containers.get.return_value = fake_container

    with patch("checks.docker.from_env", return_value=mock_client):
        result = checks.docker_inspect("homelab-vector")

    assert result["status"] == "running"
    assert result["restart_count"] == 0
    assert result["exit_code"] == 0


def test_docker_logs_clamps_since_minutes():
    fake_container = MagicMock()
    fake_container.name = "homelab-vector"
    fake_container.logs.return_value = b"log line 1\nlog line 2"

    mock_client = MagicMock()
    mock_client.containers.list.return_value = [fake_container]
    mock_client.containers.get.return_value = fake_container

    with patch("checks.docker.from_env", return_value=mock_client):
        result = checks.docker_logs("homelab-vector", since_minutes=9999)

    assert "log line 1" in result
    # since= kwarg passed to .logs() should reflect the clamp, not the raw 9999
    since_arg = fake_container.logs.call_args[1]["since"]
    from datetime import datetime, timezone, timedelta
    assert since_arg >= datetime.now(timezone.utc) - timedelta(minutes=61)


def test_docker_logs_rejects_unknown_container():
    mock_client = MagicMock()
    mock_client.containers.list.return_value = []
    with patch("checks.docker.from_env", return_value=mock_client):
        result = checks.docker_logs("not-a-real-container")
    assert result.startswith("error:")


def test_ping_rejects_host_not_in_allowlist():
    result = checks.ping("evil.example.com")
    assert "error" in result


def test_ping_allows_known_host(monkeypatch):
    fake_completed = MagicMock()
    fake_completed.returncode = 0
    fake_completed.stdout = "3 packets transmitted, 3 received, 0% packet loss"
    with patch("checks.subprocess.run", return_value=fake_completed) as mock_run:
        result = checks.ping("10.0.3.9")
    assert result["reachable"] is True
    args = mock_run.call_args[0][0]
    assert args[0] == "ping"
    assert "shell" not in mock_run.call_args[1] or mock_run.call_args[1].get("shell") is not True


def test_curl_health_rejects_url_not_in_allowlist():
    result = checks.curl_health("http://evil.example.com/steal")
    assert "error" in result


def test_curl_health_allows_known_endpoint():
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = b"OK"
    fake_cm = MagicMock()
    fake_cm.__enter__ = MagicMock(return_value=fake_resp)
    fake_cm.__exit__ = MagicMock(return_value=False)
    with patch("checks.urllib.request.urlopen", return_value=fake_cm):
        result = checks.curl_health("http://localai-litellm:4000/health")
    assert result["status"] == 200


def test_query_prometheus_rejects_shell_metacharacters():
    result = checks.query_prometheus("up{job='x'}; rm -rf /")
    assert "error" in result


def test_query_prometheus_sends_query_param():
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = b'{"status":"success","data":{"result":[]}}'
    fake_cm = MagicMock()
    fake_cm.__enter__ = MagicMock(return_value=fake_resp)
    fake_cm.__exit__ = MagicMock(return_value=False)
    with patch("checks.urllib.request.urlopen", return_value=fake_cm) as mock_open:
        result = checks.query_prometheus("up")
    assert result["status"] == "success"
    assert "query=up" in mock_open.call_args[0][0]


def test_query_loki_rejects_shell_metacharacters():
    result = checks.query_loki('{job="x"} |= "`whoami`"')
    assert "error" in result


def test_disk_usage_rejects_path_outside_allowlist():
    result = checks.disk_usage("/etc/shadow")
    assert "error" in result


def test_disk_usage_allows_root():
    with patch("checks.shutil.disk_usage", return_value=(1000, 500, 500)):
        result = checks.disk_usage("/")
    assert result["total"] == 1000
    assert result["used"] == 500


def test_dispatch_calls_known_tool():
    mock_fn = MagicMock(return_value={"status": "running"})
    with patch.dict(checks._DISPATCH_TABLE, {"docker_inspect": mock_fn}):
        result = checks.dispatch("docker_inspect", {"container": "homelab-vector"})
    mock_fn.assert_called_once_with(container="homelab-vector")
    assert result == {"status": "running"}


def test_dispatch_returns_error_for_unknown_tool():
    result = checks.dispatch("delete_everything", {})
    assert "error" in result


def test_dispatch_catches_exceptions():
    mock_fn = MagicMock(side_effect=RuntimeError("boom"))
    with patch.dict(checks._DISPATCH_TABLE, {"docker_inspect": mock_fn}):
        result = checks.dispatch("docker_inspect", {"container": "x"})
    assert "error" in result
    assert "boom" in result["error"]


def test_tool_schema_names_match_dispatch_table():
    schema_names = {t["function"]["name"] for t in checks.TOOL_SCHEMA}
    assert schema_names == set(checks._DISPATCH_TABLE.keys())
