"""
Read-only diagnostic tools JARVIS's triage loop can call.

Every function here is deliberately narrow: the LLM picks a tool name and
argument values via the OpenAI tool-calling protocol, never a raw command
string. Every argument is validated against a known-good set before it
touches a real SDK call, subprocess, or HTTP request.
"""

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
