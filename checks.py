"""
Read-only diagnostic tools JARVIS's triage loop can call.

Every function here is deliberately narrow: the LLM picks a tool name and
argument values via the OpenAI tool-calling protocol, never a raw command
string. Every argument is validated against a known-good set before it
touches a real SDK call, subprocess, or HTTP request.
"""

import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import docker


def _known_container_names(client) -> set[str]:
    return {c.name for c in client.containers.list(all=True)}


def docker_inspect(container: str) -> dict:
    client = docker.from_env()
    known = _known_container_names(client)
    if container not in known:
        return {"error": f"unknown container: {container!r}"}
    c = client.containers.get(container)
    return {
        "status": c.status,
        "restart_count": c.attrs.get("RestartCount", 0),
        "exit_code": c.attrs.get("State", {}).get("ExitCode", 0),
    }


def docker_logs(container: str, since_minutes: int = 20) -> str:
    client = docker.from_env()
    known = _known_container_names(client)
    if container not in known:
        return f"error: unknown container: {container!r}"
    clamped = max(1, min(since_minutes, 60))
    since_dt = datetime.now(timezone.utc) - timedelta(minutes=clamped)
    c = client.containers.get(container)
    raw = c.logs(since=since_dt, timestamps=False, stdout=True, stderr=True)
    return raw.decode("utf-8", errors="replace")[:4000]


_PING_ALLOWLIST = {
    "10.0.3.9",    # node1
    "10.0.0.12",   # Alpha60
    "10.0.0.44",   # vault44
    "10.0.0.1",    # UDR (Servers zone)
    "10.0.3.1",    # UDR (IoT zone)
    "10.0.0.10",   # AdGuard
}

_HEALTH_URL_ALLOWLIST = {
    "http://localai-litellm:4000/health",
    "http://monitoring-prometheus:9090/-/healthy",
    "http://monitoring-loki:3100/ready",
    "http://traefik:8082/ping",
}


def ping(host: str) -> dict:
    if host not in _PING_ALLOWLIST:
        return {"error": f"host not in allowlist: {host!r}"}
    result = subprocess.run(
        ["ping", "-c", "3", "-W", "2", host],
        capture_output=True, text=True, timeout=10,
    )
    return {"reachable": result.returncode == 0, "output": result.stdout[-500:]}


def curl_health(url: str) -> dict:
    if url not in _HEALTH_URL_ALLOWLIST:
        return {"error": f"url not in allowlist: {url!r}"}
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return {"status": resp.status, "body": resp.read(500).decode("utf-8", errors="replace")}
    except urllib.error.URLError as e:
        return {"error": str(e)}
