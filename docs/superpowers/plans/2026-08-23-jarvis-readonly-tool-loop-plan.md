# JARVIS Read-Only Fact-Finding Tool Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give JARVIS's alert triage an adaptive, bounded loop where the LLM can call fixed, read-only Python functions (docker inspect/logs, ping, curl health, Prometheus/Loki query, disk usage) to gather live facts before answering, replacing the existing fixed low-confidence verification pass.

**Architecture:** New `checks.py` module holds every tool as a plain Python function plus a `dispatch()` gate and an OpenAI-format `TOOL_SCHEMA` list. `llm.py` gains `chat_with_tools()` (posts with a `tools` param, returns the raw message dict instead of just `.content`, since the caller needs to inspect `tool_calls`). `cogs/alerts.py`'s `_triage()` runs a loop (max 4 rounds): each round calls `llm.chat_with_tools()`, and if the response has `tool_calls`, dispatches each to `checks.dispatch()` and appends the result back into the conversation before looping; if the response is a final verdict (matches the existing `TRIAGE_PROMPT_JSON`/`HYPOTHESIS` schema, no tool call), the loop ends immediately — adaptive, not fixed-length. `_verification_pass()` and its call site are removed.

**Tech Stack:** Python 3, `discord.py`, `httpx` (async HTTP to LiteLLM), `docker` SDK (new dependency), `pytest` + `pytest-asyncio`, `unittest.mock`.

**Spec:** `docs/superpowers/plans/2026-08-23-jarvis-readonly-tool-loop.md`

## Global Constraints

- Every tool function is read-only — no restart/exec/rm/write capability anywhere in `checks.py`. This applies to every task below without exception.
- The LLM never composes a shell command string — it only selects a tool name and JSON argument values via the standard `tool_calls` protocol. No `shell=True` anywhere.
- Every argument the LLM supplies gets validated against a fixed allowlist or strict regex *before* it reaches a Docker SDK call, subprocess, or HTTP request — reject-if-unknown, not sanitize-and-proceed.
- `TRIAGE_PROMPT_JSON`/`TRIAGE_PROMPT_HYPOTHESIS` response schemas are unchanged — `_format_triage_reply()` requires no changes.
- Max 4 rounds per triage, loop exits early the moment the model returns a final verdict instead of a tool call.
- A tool function raising must not crash the loop — `checks.dispatch()` catches and returns `{"error": "<message>"}`.
- If `chat_with_tools()` itself fails, fall back to today's single-shot `chat_json()` call with no tools (graceful degradation to current behavior).

---

### Task 1: Add `docker` SDK dependency and docker.sock mount

**Files:**
- Modify: `requirements.txt`
- Modify: `docker-compose.yml`

**Interfaces:**
- Produces: `docker` Python package available for import in `checks.py` (Task 3); `/var/run/docker.sock` readable inside the `localai-jarvis` container.

- [ ] **Step 1: Add the dependency**

In `requirements.txt`, add (matching the version pin already used in `~/projects/homelab-monitor/requirements.txt` for consistency):

```
docker>=7.0.0
```

- [ ] **Step 2: Mount the socket read-only**

In `docker-compose.yml`, under `services.localai-jarvis.volumes`, add:

```yaml
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

`userns_mode: "host"` is already set on this service — the existing systemd `socket-acl.conf` drop-in (`setfacl -m u:100000:rw /var/run/docker.sock`, verified live 2026-08-23) already grants the userns-remap subordinate UID read/write access, so no additional host-side change is needed. Mount is `:ro` here since this container only ever inspects/reads logs, never needs write access to the socket itself.

- [ ] **Step 3: Rebuild and verify the mount**

```bash
cd /opt/localai-bot && docker compose build && docker compose up -d
docker exec localai-jarvis python3 -c "import docker; c = docker.from_env(); print(len(c.containers.list(all=True)), 'containers visible')"
```

Expected: prints a container count > 0, no permission errors.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt docker-compose.yml
git commit -m "feat: mount docker.sock read-only and add docker SDK dependency for JARVIS tool loop"
```

---

### Task 2: `llm.chat_with_tools()`

**Files:**
- Modify: `llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: nothing new — same `LITELLM_URL`/`MODEL` constants already in `llm.py`.
- Produces: `async def chat_with_tools(messages: list[dict], tools: list[dict], max_tokens: int = 1000) -> dict` — returns the raw `message` dict from the API response (`{"role": ..., "content": ..., "tool_calls": [...]}`, `tool_calls` absent if the model didn't call one). Task 4 consumes this directly.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_chat_with_tools_returns_tool_calls():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "docker_inspect", "arguments": '{"container":"homelab-vector"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    tools = [{"type": "function", "function": {"name": "docker_inspect", "parameters": {}}}]

    with patch("llm.httpx.AsyncClient", return_value=mock_client):
        result = await llm.chat_with_tools(
            [{"role": "user", "content": "check homelab-vector"}], tools
        )

    assert result["tool_calls"][0]["function"]["name"] == "docker_inspect"
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[1]["json"]["tools"] == tools
    assert call_kwargs[1]["json"]["max_tokens"] == 1000


@pytest.mark.asyncio
async def test_chat_with_tools_returns_content_when_no_tool_call():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{
            "message": {"role": "assistant", "content": '{"severity":"low","cause":"fine"}'},
            "finish_reason": "stop",
        }]
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("llm.httpx.AsyncClient", return_value=mock_client):
        result = await llm.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])

    assert "tool_calls" not in result
    assert result["content"] == '{"severity":"low","cause":"fine"}'
```

Add these to `tests/test_llm.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm.py -k chat_with_tools -v`
Expected: FAIL with `AttributeError: module 'llm' has no attribute 'chat_with_tools'`

- [ ] **Step 3: Implement**

In `llm.py`, add:

```python
async def chat_with_tools(messages: list[dict], tools: list[dict], max_tokens: int = 1000) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LITELLM_URL,
            json={"model": MODEL, "messages": messages, "tools": tools, "max_tokens": max_tokens},
            headers={"Authorization": "Bearer dummy"},
            timeout=180.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_llm.py -v`
Expected: all PASS (including the two new tests and every existing `test_chat_*`/`test_chat_json_*` test unchanged).

- [ ] **Step 5: Commit**

```bash
git add llm.py tests/test_llm.py
git commit -m "feat: add llm.chat_with_tools for OpenAI-style tool calling"
```

---

### Task 3a: `checks.py` — Docker tools

**Files:**
- Create: `checks.py`
- Test: `tests/test_checks.py`

**Interfaces:**
- Consumes: `docker` SDK (Task 1).
- Produces: `def docker_inspect(container: str) -> dict`, `def docker_logs(container: str, since_minutes: int = 20) -> str`. Task 3d's `dispatch()` and `TOOL_SCHEMA` consume these by name.

- [ ] **Step 1: Write the failing tests**

```python
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
```

Create `tests/test_checks.py` with these.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_checks.py -v`
Expected: FAIL — `checks` module doesn't exist yet.

- [ ] **Step 3: Implement**

Create `checks.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_checks.py -v`
Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add checks.py tests/test_checks.py
git commit -m "feat: add checks.py docker_inspect/docker_logs read-only tools"
```

---

### Task 3b: `checks.py` — Network tools (ping, curl_health)

**Files:**
- Modify: `checks.py`
- Test: `tests/test_checks.py`

**Interfaces:**
- Produces: `def ping(host: str) -> dict`, `def curl_health(url: str) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_checks.py -k "ping or curl_health" -v`
Expected: FAIL — `AttributeError`, functions don't exist.

- [ ] **Step 3: Implement**

Add to `checks.py` (with new imports `subprocess`, `urllib.request`, `urllib.error` at the top):

```python
import subprocess
import urllib.error
import urllib.request

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_checks.py -v`
Expected: all PASS (previous 4 + new 4 = 8).

- [ ] **Step 5: Commit**

```bash
git add checks.py tests/test_checks.py
git commit -m "feat: add checks.py ping/curl_health read-only tools"
```

---

### Task 3c: `checks.py` — Prometheus/Loki query + disk usage tools

**Files:**
- Modify: `checks.py`
- Test: `tests/test_checks.py`

**Interfaces:**
- Produces: `def query_prometheus(promql: str) -> dict`, `def query_loki(logql: str) -> dict`, `def disk_usage(path: str) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_checks.py -k "prometheus or loki or disk_usage" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to `checks.py` (new imports `json`, `os`, `shutil`, `urllib.parse` at top):

```python
import json
import os
import shutil
import urllib.parse

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


def disk_usage(path: str) -> dict:
    from pathlib import Path
    resolved = Path(path).resolve()
    if not any(str(resolved) == a or str(resolved).startswith(a + "/") for a in _DISK_PATH_ALLOWLIST):
        return {"error": f"path not in allowlist: {path!r}"}
    total, used, free = shutil.disk_usage(str(resolved))
    return {"total": total, "used": used, "free": free}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_checks.py -v`
Expected: all PASS (8 + 5 = 13).

- [ ] **Step 5: Commit**

```bash
git add checks.py tests/test_checks.py
git commit -m "feat: add checks.py query_prometheus/query_loki/disk_usage read-only tools"
```

---

### Task 3d: `checks.py` — `TOOL_SCHEMA` and `dispatch()`

**Files:**
- Modify: `checks.py`
- Test: `tests/test_checks.py`

**Interfaces:**
- Consumes: every function from Tasks 3a-3c.
- Produces: `TOOL_SCHEMA: list[dict]` (OpenAI tools format, passed to `llm.chat_with_tools`), `def dispatch(name: str, arguments: dict) -> dict` — Task 4 consumes both directly.

- [ ] **Step 1: Write the failing tests**

```python
def test_dispatch_calls_known_tool():
    with patch("checks.docker_inspect", return_value={"status": "running"}) as mock_fn:
        result = checks.dispatch("docker_inspect", {"container": "homelab-vector"})
    mock_fn.assert_called_once_with(container="homelab-vector")
    assert result == {"status": "running"}


def test_dispatch_returns_error_for_unknown_tool():
    result = checks.dispatch("delete_everything", {})
    assert "error" in result


def test_dispatch_catches_exceptions():
    with patch("checks.docker_inspect", side_effect=RuntimeError("boom")):
        result = checks.dispatch("docker_inspect", {"container": "x"})
    assert "error" in result
    assert "boom" in result["error"]


def test_tool_schema_names_match_dispatch_table():
    schema_names = {t["function"]["name"] for t in checks.TOOL_SCHEMA}
    assert schema_names == set(checks._DISPATCH_TABLE.keys())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_checks.py -k "dispatch or tool_schema" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the bottom of `checks.py`:

```python
_DISPATCH_TABLE = {
    "docker_inspect": docker_inspect,
    "docker_logs": docker_logs,
    "ping": ping,
    "curl_health": curl_health,
    "query_prometheus": query_prometheus,
    "query_loki": query_loki,
    "disk_usage": disk_usage,
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
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_checks.py -v`
Expected: all PASS (13 + 4 = 17).

- [ ] **Step 5: Commit**

```bash
git add checks.py tests/test_checks.py
git commit -m "feat: add checks.py TOOL_SCHEMA and dispatch()"
```

---

### Task 4: Wire the adaptive loop into `cogs/alerts.py`

**Files:**
- Modify: `cogs/alerts.py`
- Test: `tests/test_alerts.py`

**Interfaces:**
- Consumes: `llm.chat_with_tools` (Task 2), `checks.dispatch`, `checks.TOOL_SCHEMA` (Task 3d).
- Produces: `_triage()` behavior change — existing external behavior (`_format_triage_reply()` output format, Discord reply content) is unchanged; only how the `result` dict is obtained changes.

- [ ] **Step 1: Update existing tests broken by the call-pattern change**

`test_auto_triage_calls_chat_json` (and similar tests asserting `llm.chat_json` is called) currently patch `cogs.alerts.llm.chat_json`. After this task, the primary path uses `llm.chat_with_tools` instead. Update the assertions in `tests/test_alerts.py`:

```python
@pytest.mark.asyncio
async def test_auto_triage_calls_chat_with_tools():
    bot = make_bot()
    cog = AlertsCog(bot)
    msg = make_alert_message(bot_authored=True)

    # First (and only, in this test) round: model answers directly, no tool call
    final_message = {"role": "assistant", "content": None, "tool_calls": None}
    with patch("cogs.alerts.llm.chat_with_tools", new_callable=AsyncMock,
               return_value={"role": "assistant", "content": '{"severity":"high","cause":"disk full","confidence":0.85,"commands":[],"next_step":"clear logs","suppress":false}'}) as mock_tools, \
         patch("cogs.alerts.lokiquery.fetch_context", new_callable=AsyncMock, return_value=""), \
         patch("cogs.alerts.vault.search", return_value=""):
        await cog.on_message(msg)
        mock_tools.assert_called()
        msg.reply.assert_called_once()
```

Find every other test in `tests/test_alerts.py` that patches `cogs.alerts.llm.chat_json` for the *triage* path (not the `_ask`/`@mention` path, which is untouched — see Global Constraints/spec's Out of Scope) and update similarly. Leave `VERIFICATION_PROMPT_JSON`-specific tests (if any target `_verification_pass` directly) — remove them, since `_verification_pass` is deleted in Step 3 below.

- [ ] **Step 2: Write new tests for loop behavior**

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_alerts.py -v`
Expected: FAIL — `_triage` doesn't call `chat_with_tools` yet, `checks` not imported in `cogs.alerts`.

- [ ] **Step 4: Implement**

In `cogs/alerts.py`:

1. Add `import checks` and `import json` near the top with the other imports.
2. Remove `VERIFICATION_PROMPT_JSON` constant, and remove the `_verification_pass` method entirely (lines ~385-410 as of 2026-08-23).
3. Remove the verification-pass call site (`if result.get("confidence", 1.0) < 0.6 and loki_ctx: result = await self._verification_pass(...)`).
4. Replace the single `try: result = await llm.chat_json(messages) except Exception: ...` block with a new adaptive loop. Add this as a new method on `AlertsCog`:

```python
    async def _run_tool_loop(self, system_prompt: str, user_content: str) -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        for _ in range(4):
            response = await llm.chat_with_tools(messages, checks.TOOL_SCHEMA)
            tool_calls = response.get("tool_calls")
            if not tool_calls:
                content = response.get("content", "")
                try:
                    return json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    return {"severity": "medium", "cause": content[:200], "confidence": 0.3,
                            "commands": [], "next_step": "manual review", "suppress": False}
            messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
            for call in tool_calls:
                fn_name = call["function"]["name"]
                fn_args = json.loads(call["function"]["arguments"])
                result = checks.dispatch(fn_name, fn_args)
                messages.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "content": json.dumps(result),
                })
        # Round 4 exhausted with no verdict — force a final answer, no tool access
        return await llm.chat_json(messages)
```

5. Replace the old call site (the `try:/except:` block calling `llm.chat_json(messages)` directly) with:

```python
            try:
                result = await self._run_tool_loop(system_prompt, user_content)
            except Exception:
                answer = await llm.chat(messages)
                await message.reply(answer[:1990])
                memory.log_observation(event=content[:200], summary=answer[:500], host=alert_label)
                return
```

(`messages` here still refers to the original `[system_prompt, user_content]` pair built earlier in `_triage` for the fallback path — no change needed to how that pair is constructed, only to what handles the primary path.)

6. Update `TRIAGE_PROMPT_JSON` and `TRIAGE_PROMPT_HYPOTHESIS` to mention tool availability — append to each, before the final "Rules:" block:

```
You have read-only diagnostic tools available (docker_inspect, docker_logs, ping, curl_health, query_prometheus, query_loki, disk_usage). Use one if it would sharpen your diagnosis — check the actual state rather than guessing. Once you're confident, stop calling tools and answer with the JSON schema below. Don't use tools for alerts that are already clear from the context provided.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_alerts.py -v`
Expected: all PASS, including every pre-existing test not touched by this change.

- [ ] **Step 6: Run the full test suite**

Run: `pytest tests/ -v`
Expected: all PASS across every test file — confirms no regression in `test_vault.py`, `test_memory.py`, `test_sysstats.py`, `test_chat.py`, `test_llm.py`, `test_checks.py`, `test_alerts.py`.

- [ ] **Step 7: Commit**

```bash
git add cogs/alerts.py tests/test_alerts.py
git commit -m "feat: replace fixed verification pass with adaptive read-only tool loop in JARVIS triage"
```

---

### Task 5: Deploy and verify live

**Files:** none (deployment/verification only)

- [ ] **Step 1: Rebuild and restart**

```bash
cd /opt/localai-bot && docker compose build && docker compose up -d
```

- [ ] **Step 2: Confirm startup is clean**

```bash
docker logs localai-jarvis --since 30s
```

Expected: Discord gateway connects, no import errors (`checks`, `docker` module resolve fine).

- [ ] **Step 3: Trigger a real alert and observe the loop**

Post (or wait for) a real alert in the `homelab-alerts` channel. Confirm via `docker logs localai-jarvis` that `chat_with_tools` is being called and, where relevant, that a tool result appears in the conversation before the final reply lands in Discord.

- [ ] **Step 4: Spot-check one tool call end-to-end**

Pick an alert where a tool would plausibly help (e.g. a container-related alert) and confirm the Discord reply reflects a live fact (e.g. cites an actual restart count or actual reachability) rather than a generic guess — this is the actual point of the feature, worth eyeballing once for real before calling it done.

---

## Self-Review Notes

- **Spec coverage**: all 7 tools from the spec are implemented (Tasks 3a-3c), the loop matches the spec's pseudocode (Task 4), `llm.chat_with_tools` matches the spec's signature (Task 2), the docker.sock mount is added (Task 1), error handling matches spec (dispatch catches exceptions, chat_with_tools failure falls back to chat_json), verification pass is removed (Task 4 Step 4.2-4.3).
- **Type consistency**: `dispatch(name: str, arguments: dict) -> dict` used consistently across Task 3d and Task 4's `_run_tool_loop`. `TOOL_SCHEMA` name matches `checks.TOOL_SCHEMA` used in both the schema-consistency test (3d) and the loop (Task 4).
- **No placeholders**: every step has real code, not descriptions of code.
