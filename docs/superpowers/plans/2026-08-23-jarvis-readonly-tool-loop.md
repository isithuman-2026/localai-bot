# JARVIS Read-Only Fact-Finding Tool Loop — Design

Status: approved by user 2026-08-23, ready for implementation plan.

## Problem

JARVIS's triage (`cogs/alerts.py`) already grounds every alert in real context — `vault.search()` (ChromaDB semantic search over the Obsidian vault), `lokiquery.fetch_context()` (live Loki logs), `memory.search_facts()` (known-facts DB) — but only ever *suggests* commands as text (`"commands":["ssh boss@node1 <cmd>"]`). It never executes anything itself. User wants it to run specific read-only checks itself as part of a bounded, adaptive fact-finding loop before finalizing a verdict.

## Verified facts this design relies on

- **Tool-calling works live**: tested `gemma4:e4b` via the `localai-litellm` proxy with an OpenAI-style `tools` param — model correctly reasons about which tool to call and with what arguments, and (given enough `max_tokens`) emits a proper `tool_calls` response (`finish_reason: "tool_calls"`). Confirmed via a real chat-completion round-trip, not assumed.
- **Overhead per round**: the model's visible chain-of-thought reasoning consumes ~250-900 tokens before it acts. A test with `max_tokens: 200` truncated mid-reasoning (`finish_reason: "length"`, no tool call emitted) — `max_tokens: 900` succeeded. Any implementation must budget for this per round, not reuse the current `max_tokens: 800` default from `llm.chat()` unmodified for tool-call rounds.
- **Resource headroom is not a constraint**: GPU (AMD 780M iGPU via Vulkan) had 16.6GB VRAM free after model load (22GB total, ~5.4GB used by the model); `n_ctx=16384` with quantized KV cache (~83MB) comfortably fits 4 rounds of accumulating tool-call history. Host RAM: ~10GB available. The real cost is **latency**, not memory — `llama-server` runs with `--parallel 1` (single inference slot, sequential, no concurrency), so a multi-round triage adds real wall-clock time and a burst of simultaneous alerts queues behind each other. User confirmed this latency is acceptable (automated platform, not a synchronous wait).

## Architecture

New module: `/opt/localai-bot/checks.py`, parallel to `vault.py`/`lokiquery.py`/`memory.py`. Each tool is a plain Python function — the LLM never composes a command string, only picks a tool name and argument values via the standard OpenAI `tools`/`tool_calls` protocol (already proven to work above).

### Tools

| Function | Behavior | Argument validation |
|---|---|---|
| `docker_inspect(container: str) -> dict` | Status, restart count, exit code via Docker SDK (`docker.from_env()`, already used elsewhere in this codebase's sibling `infra_ai.py` pattern) | `container` must match a name from a live `client.containers.list(all=True)` call — reject unknown names before touching the SDK further |
| `docker_logs(container: str, since_minutes: int = 20) -> str` | Recent logs via Docker SDK | Same container validation; `since_minutes` clamped to `[1, 60]` |
| `ping(host: str) -> dict` | Reachability + latency, `subprocess.run(["ping", "-c", "3", "-W", "2", host], shell=False, ...)` | `host` must match a fixed allowlist (node1 `10.0.3.9`, Alpha60 `10.0.0.12`, vault44 `10.0.0.44`, UDR `10.0.0.1`/`10.0.3.1`, AdGuard `10.0.0.10`) or a strict private-IP regex — never passed to a shell |
| `curl_health(url: str) -> dict` | HTTP status + short body via `urllib`, no shell | `url` must match an allowlist of known internal health/status endpoints (Traefik, Grafana, litellm `/health`, etc.) — reject arbitrary URLs, especially anything resolving outside the LAN |
| `query_prometheus(promql: str) -> dict` | GET to `monitoring-prometheus:9090/api/v1/query` with `promql` as a request parameter | No shell involved by construction; still cap query length and reject anything containing `;`/`&`/backticks as defense-in-depth even though it's not shell-interpreted |
| `query_loki(logql: str) -> dict` | GET to `monitoring-loki:3100`'s query API, `logql` as a request parameter | Same defense-in-depth as above |
| `disk_usage(path: str) -> dict` | `shutil.disk_usage()` / `os.stat` mtime checks | `path` must resolve (via `Path.resolve()` + `is_relative_to()`, same pattern as `vault.py`'s existing path-safety checks) under one of a fixed allowlist: `/`, `/opt/*` top-level stack dirs, known log dirs — no arbitrary traversal |

Every function returns a JSON-serializable dict (or raises, caught by the loop — see Error handling). None of them accept a raw shell string from the model.

### New function needed in `llm.py`

`llm.py` currently has `chat(messages, max_tokens=800)` and `chat_json(messages)` — neither passes a `tools` param or parses `tool_calls` from the response. Needs a third function:

```python
async def chat_with_tools(messages: list[dict], tools: list[dict], max_tokens: int = 1000) -> dict:
    # posts with "tools": tools, returns the raw message dict (may contain
    # "tool_calls" and/or "content") rather than extracting .content like chat() does —
    # the loop needs to inspect finish_reason/tool_calls, not just get a string back
```

### Loop

Replaces the existing `VERIFICATION_PROMPT_JSON` second-pass entirely (was: triggered only on low confidence + Loki context present; now: the adaptive loop always runs and supersedes it).

```
messages = [system_prompt_with_tools, user_alert_content]
for round in 1..4:
    response = llm.chat_with_tools(messages, tools=TOOL_SCHEMA, max_tokens=1000)
    if response.tool_calls:
        for call in response.tool_calls:
            result = checks.dispatch(call.name, call.arguments)  # catches exceptions -> {"error": ...}
            messages.append(assistant_tool_call_message(call))
            messages.append(tool_result_message(call.id, result))
        continue  # next round
    else:
        # model emitted a final verdict matching TRIAGE_PROMPT_JSON/HYPOTHESIS schema — done
        return response
# round 4 exhausted with no verdict — force a final answer, no more tool access
final = llm.chat(messages + [force_answer_instruction], max_tokens=800)
return final
```

`TRIAGE_PROMPT_JSON`/`TRIAGE_PROMPT_HYPOTHESIS` schemas are unchanged — `_format_triage_reply()` and the Discord output format need no changes. The system prompt gains a tools description and an instruction to use them when they'd sharpen the diagnosis, and to stop and answer once confident rather than exhausting all 4 rounds by default (adaptive, not fixed).

### Error handling

- A tool function raising (Docker SDK error, network timeout, invalid path) is caught by `checks.dispatch()` and returned to the model as `{"error": "<message>"}` in the next round's `tool` message — the model can factor a failed check into its answer ("couldn't verify X, but based on Y...") rather than crashing the triage.
- If `llm.chat_with_tools()` itself fails (timeout, malformed response), fall back to today's single-shot `TRIAGE_PROMPT_JSON`/`HYPOTHESIS` call with no tools — triage degrades gracefully to current behavior rather than failing the whole alert.

### Testing

- Unit tests per tool function in `checks.py`, each with the relevant SDK/subprocess/HTTP call mocked — verify correct behavior AND that invalid/out-of-allowlist arguments are rejected before touching the real call.
- Loop-control tests in `test_alerts.py` (or a new `test_checks_loop.py`) with a mocked `llm.chat_with_tools()`: verify early stop on a verdict, verify the round-4 forced-answer path, verify a mid-loop tool error doesn't crash the loop.
- No changes needed to existing `test_alerts.py` tests that don't touch `_triage()`'s tool-loop internals.

## Out of scope for this change

- No write/mutating tools (restart, exec, rm) — read-only only, per the original ask.
- No changes to `_ask()` (the `@mention` chat path) — this is triage-only for now; could be extended later if useful, not requested.
- No new Grafana alerting or infra changes — this is purely `localai-bot` application logic.
