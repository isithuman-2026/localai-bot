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
