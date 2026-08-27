"""
Mutating remediation actions JARVIS can execute, gated by human confirmation.

Kept in its own module (not checks.py) so read vs. write is a file boundary,
not just a comment. Same shape as checks.py: named function, hard allowlist,
dispatch table. Nothing here runs from the LLM tool loop directly — alerts.py
only calls dispatch() after a human confirms via reaction.
"""

import os
import time
from pathlib import Path

import docker

REMEDIATE_DOCKER_HOST = os.environ.get("REMEDIATE_DOCKER_HOST", "tcp://jarvis-socket-proxy:2375")


def _client() -> docker.DockerClient:
    return docker.DockerClient(base_url=REMEDIATE_DOCKER_HOST)

# Passive containers: nothing else depends on their uptime, restarting drops
# a few seconds of metrics/logs and nothing else. Safe to auto-execute.
AUTO_RESTART_ALLOWLIST = {
    "unpoller",
    "homelab-adguard-exporter",
    "homelab-dockhand-exporter",
    "monitoring-blackbox-exporter",
    "monitoring-node-exporter",
    "monitoring-cadvisor",
    "monitoring-snmp-exporter",
}

# Functional pipeline components: a mid-restart timing issue could drop real
# data (log ingestion, cron jobs). Require a human 👍 before executing.
CONFIRM_RESTART_ALLOWLIST = {
    "homelab-vector",
    "homelab-scripts",
}

_RESTART_ALLOWLIST = AUTO_RESTART_ALLOWLIST | CONFIRM_RESTART_ALLOWLIST

# Tools with no target-dependent risk — always auto-execute.
_ALWAYS_AUTO_TOOLS = {"prune_old_logs"}


def requires_confirmation(tool: str, args: dict) -> bool:
    """True if a human 👍 must gate this action before dispatch() runs it."""
    if tool in _ALWAYS_AUTO_TOOLS:
        return False
    if tool == "restart_container":
        return args.get("container") not in AUTO_RESTART_ALLOWLIST
    return True


def restart_container(container: str) -> dict:
    if container not in _RESTART_ALLOWLIST:
        return {"error": f"container not in restart allowlist: {container!r}"}
    client = _client()
    c = client.containers.get(container)
    c.restart(timeout=10)
    c.reload()
    return {"restarted": container, "status": c.status}


LOGS_DIR = Path("/homelab-logs")
LOG_RETENTION_DAYS = 7


def prune_old_logs() -> dict:
    if not LOGS_DIR.is_dir():
        return {"error": f"logs dir not mounted: {LOGS_DIR}"}
    cutoff = time.time() - (LOG_RETENTION_DAYS * 86400)
    deleted = []
    for f in LOGS_DIR.rglob("*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            deleted.append(f.name)
    return {"deleted_count": len(deleted), "deleted": deleted[:20]}


_DISPATCH_TABLE = {
    "restart_container": restart_container,
    "prune_old_logs": prune_old_logs,
}


def dispatch(name: str, arguments: dict) -> dict:
    fn = _DISPATCH_TABLE.get(name)
    if fn is None:
        return {"error": f"unknown remediation: {name!r}"}
    try:
        result = fn(**arguments)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as e:
        return {"error": str(e)}


REMEDIATION_TOOL_NAMES = set(_DISPATCH_TABLE.keys())

REMEDIATE_TOOL_SCHEMA = [
    {"type": "function", "function": {
        "name": "restart_container",
        "description": "Restart a known non-critical Docker container.",
        "parameters": {"type": "object", "properties": {"container": {"type": "string"}}, "required": ["container"]},
    }},
    {"type": "function", "function": {
        "name": "prune_old_logs",
        "description": "Delete syslog/docker log JSON files older than the 7-day retention window.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
]
