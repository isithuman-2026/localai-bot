"""
Loki query helpers for JARVIS alert triage.
Fetches recent relevant logs to include as context in LLM responses.
"""

import json
import os
import time

import httpx

LOKI_URL = os.environ.get("LOKI_URL", "http://monitoring-loki:3100")
LOOKBACK_SECONDS = 1800  # 30 min context window for triage
MAX_LINES = 30


# Maps alert label keywords to focused LogQL queries
_QUERY_MAP = {
    "fail2ban": '{source="fail2ban"} |~ "Ban |Found |WARNING"',
    "ssh": '{source="fail2ban"} |~ "Found |Ban "',
    "oom": '{job="vector"} |~ "(?i)killed process|out of memory"',
    "synology auth": '{source="syslog", host=~"vault44|Alpha60"} |~ "(?i)login|auth|user"',
    "udr": '{source="syslog", host="Oumuamua"}',
    "authelia": '{source="docker"} |= "authelia"',
    "scraping": '{source="docker"} |= "monitoring-prometheus" |~ "scrape|target"',
    "prometheus": '{source="docker"} |= "monitoring-prometheus" |~ "scrape|target|error"',
    "10.0.0.44": '{source="syslog", host="vault44"}',
    "vault44": '{source="syslog", host="vault44"}',
    "tmdb": '{source="docker"} |~ "(?i)arr-sonarr|arr-radarr" |~ "(?i)TMDB|tmdb|metadata"',
    "sonarr": '{source="docker"} |= "arr-sonarr"',
    "radarr": '{source="docker"} |= "arr-radarr"',
    "prowlarr": '{source="docker"} |= "arr-prowlarr"',
    "520": '{source="docker"} |= "arr-prowlarr" |~ "520|indexer"',
    "429": '{source="docker"} |= "arr-prowlarr" |~ "429|rate"',
}


async def fetch_context(alert_label: str, alert_text: str) -> str:
    """
    Given an alert label and text, fetch relevant recent log lines from Loki.
    Returns a formatted string to inject into the LLM prompt.
    """
    query = _pick_query(alert_label, alert_text)
    if not query:
        return ""

    now_ns = int(time.time() * 1e9)
    start_ns = now_ns - int(LOOKBACK_SECONDS * 1e9)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": start_ns,
                    "end": now_ns,
                    "limit": MAX_LINES,
                    "direction": "backward",
                },
            )
            resp.raise_for_status()
    except Exception as exc:
        return f"(Loki fetch failed: {exc})"

    lines = []
    for stream in resp.json().get("data", {}).get("result", []):
        for _ts, value in stream.get("values", []):
            try:
                msg = json.loads(value).get("message", value)
            except (json.JSONDecodeError, AttributeError):
                msg = value
            lines.append(msg)

    if not lines:
        return "(No recent logs found in Loki for this alert type.)"

    lines = lines[:MAX_LINES]
    block = "\n".join(f"  {l}" for l in reversed(lines))
    return f"Recent logs from Loki (last 30 min, newest last):\n```\n{block}\n```"


def _pick_query(label: str, text: str) -> str | None:
    label_lower = label.lower()
    text_lower = text.lower()
    for keyword, query in _QUERY_MAP.items():
        if keyword in label_lower or keyword in text_lower:
            return query
    return None
