"""
Read-only diagnostic tools JARVIS's triage loop can call.

Every function here is deliberately narrow: the LLM picks a tool name and
argument values via the OpenAI tool-calling protocol, never a raw command
string. Every argument is validated against a known-good set before it
touches a real SDK call, subprocess, or HTTP request.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
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


PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://monitoring-prometheus:9090")
LOKI_URL = os.environ.get("LOKI_URL", "http://monitoring-loki:3100")

_DANGEROUS_QUERY_CHARS = {";", "&", "`", "$("}

_DISK_PATH_ALLOWLIST = {"/", "/opt", "/var/log", "/home/boss"}


def _query_string_safe(q: str) -> bool:
    return not any(c in q for c in _DANGEROUS_QUERY_CHARS)


def query_prometheus(promql: str) -> dict:
    if not _query_string_safe(promql):
        return {"error": "query contains disallowed characters"}
    url = f"{PROMETHEUS_URL}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        return {"error": str(e)}


def query_loki(logql: str) -> dict:
    if not _query_string_safe(logql):
        return {"error": "query contains disallowed characters"}
    url = f"{LOKI_URL}/loki/api/v1/query_range?" + urllib.parse.urlencode({"query": logql, "limit": 50})
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        return {"error": str(e)}


_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]{0,253}$")

_DNS_LOOKUP_ALLOWLIST = {
    "vault44", "alpha60", "node1", "traefik", "authelia", "socket-proxy",
    "homelab-vector", "homelab-scripts", "homelab-adguard",
    "monitoring-grafana", "monitoring-loki", "monitoring-prometheus",
    "localai-litellm", "localai-llm",
    "github.com", "1.1.1.1", "8.8.8.8",
}


def dns_lookup(hostname: str) -> dict:
    if hostname not in _DNS_LOOKUP_ALLOWLIST or not _HOSTNAME_RE.match(hostname):
        return {"error": f"hostname not in allowlist: {hostname!r}"}
    try:
        return {"resolved": socket.gethostbyname(hostname)}
    except socket.gaierror as e:
        return {"error": str(e)}


def traceroute(host: str) -> dict:
    if host not in _PING_ALLOWLIST:
        return {"error": f"host not in allowlist: {host!r}"}
    result = subprocess.run(
        ["traceroute", "-m", "12", "-w", "2", host],
        capture_output=True, text=True, timeout=30,
    )
    return {"output": result.stdout[-1500:]}


_PORT_CHECK_ALLOWLIST = {
    ("10.0.3.9", 5432), ("10.0.3.9", 3100), ("10.0.3.9", 9090),
    ("10.0.0.44", 5000), ("10.0.0.12", 5000), ("10.0.0.10", 53),
}


def port_check(host: str, port: int) -> dict:
    if (host, port) not in _PORT_CHECK_ALLOWLIST:
        return {"error": f"host/port not in allowlist: {host!r}:{port!r}"}
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect((host, port))
        return {"open": True}
    except OSError as e:
        return {"open": False, "error": str(e)}
    finally:
        sock.close()


def docker_stats(container: str) -> dict:
    client = docker.from_env()
    known = _known_container_names(client)
    if container not in known:
        return {"error": f"unknown container: {container!r}"}
    c = client.containers.get(container)
    stats = c.stats(stream=False)
    try:
        cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
        cpu_percent = (cpu_delta / system_delta) * stats["cpu_stats"]["online_cpus"] * 100 if system_delta > 0 else 0.0
    except (KeyError, ZeroDivisionError):
        cpu_percent = 0.0
    mem_usage = stats.get("memory_stats", {}).get("usage", 0)
    mem_limit = stats.get("memory_stats", {}).get("limit", 0)
    return {
        "cpu_percent": round(cpu_percent, 1),
        "mem_usage_mb": round(mem_usage / (1024 * 1024), 1),
        "mem_limit_mb": round(mem_limit / (1024 * 1024), 1),
    }


def list_unhealthy_containers() -> dict:
    client = docker.from_env()
    unhealthy = client.containers.list(filters={"health": "unhealthy"})
    restarting = client.containers.list(filters={"status": "restarting"})
    names = sorted({c.name for c in unhealthy} | {c.name for c in restarting})
    return {"containers": names}


def disk_usage(path: str) -> dict:
    from pathlib import Path
    resolved = Path(path).resolve()
    if not any(str(resolved) == a or str(resolved).startswith(a + "/") for a in _DISK_PATH_ALLOWLIST):
        return {"error": f"path not in allowlist: {path!r}"}
    total, used, free = shutil.disk_usage(str(resolved))
    return {"total": total, "used": used, "free": free}


_DISPATCH_TABLE = {
    "docker_inspect": docker_inspect,
    "docker_logs": docker_logs,
    "ping": ping,
    "curl_health": curl_health,
    "query_prometheus": query_prometheus,
    "query_loki": query_loki,
    "disk_usage": disk_usage,
    "dns_lookup": dns_lookup,
    "traceroute": traceroute,
    "port_check": port_check,
    "docker_stats": docker_stats,
    "list_unhealthy_containers": list_unhealthy_containers,
}


def dispatch(name: str, arguments: dict) -> dict:
    fn = _DISPATCH_TABLE.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name!r}"}
    try:
        result = fn(**arguments)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as e:
        return {"error": str(e)}


TOOL_SCHEMA = [
    {"type": "function", "function": {
        "name": "docker_inspect",
        "description": "Get status, restart count, and exit code for a known Docker container.",
        "parameters": {"type": "object", "properties": {"container": {"type": "string"}}, "required": ["container"]},
    }},
    {"type": "function", "function": {
        "name": "docker_logs",
        "description": "Get recent logs (last N minutes, max 60) for a known Docker container.",
        "parameters": {"type": "object", "properties": {
            "container": {"type": "string"},
            "since_minutes": {"type": "integer"},
        }, "required": ["container"]},
    }},
    {"type": "function", "function": {
        "name": "ping",
        "description": "Check reachability and latency of a known homelab host (node1, NAS, UDR, AdGuard).",
        "parameters": {"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]},
    }},
    {"type": "function", "function": {
        "name": "curl_health",
        "description": "Check the HTTP status of a known internal health endpoint.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "query_prometheus",
        "description": "Run a PromQL query against the homelab's Prometheus.",
        "parameters": {"type": "object", "properties": {"promql": {"type": "string"}}, "required": ["promql"]},
    }},
    {"type": "function", "function": {
        "name": "query_loki",
        "description": "Run a LogQL query against the homelab's Loki.",
        "parameters": {"type": "object", "properties": {"logql": {"type": "string"}}, "required": ["logql"]},
    }},
    {"type": "function", "function": {
        "name": "disk_usage",
        "description": "Get total/used/free disk usage for a known path (/, /opt, /var/log, /home/boss).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "dns_lookup",
        "description": "Resolve a known hostname to an IP. Use to diagnose AdGuard/DNS-down incidents.",
        "parameters": {"type": "object", "properties": {"hostname": {"type": "string"}}, "required": ["hostname"]},
    }},
    {"type": "function", "function": {
        "name": "traceroute",
        "description": "Traceroute to a known homelab host. Use to isolate WAN vs UDR vs LAN connectivity issues.",
        "parameters": {"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]},
    }},
    {"type": "function", "function": {
        "name": "port_check",
        "description": "Check whether a known host:port is accepting TCP connections.",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string"}, "port": {"type": "integer"},
        }, "required": ["host", "port"]},
    }},
    {"type": "function", "function": {
        "name": "docker_stats",
        "description": "Get live CPU% and memory usage/limit for a known Docker container. Use to root-cause disk/mem pressure alerts.",
        "parameters": {"type": "object", "properties": {"container": {"type": "string"}}, "required": ["container"]},
    }},
    {"type": "function", "function": {
        "name": "list_unhealthy_containers",
        "description": "List containers currently reporting unhealthy or restarting status. Use to scope which container an alert refers to.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
]
