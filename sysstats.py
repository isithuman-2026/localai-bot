"""
Live homelab status snapshot pulled from Prometheus.
Used to ground JARVIS's sitrep answers in real metrics instead of LLM guesswork.
"""

import os
import time

import httpx

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://monitoring-prometheus:9090")
_TIMEOUT = 10

_SITREP_PHRASES = (
    "sitrep", "situation report", "status update", "status of the lab",
    "give me an overview", "homelab environment", "lab environment",
    "how's everything", "hows everything", "how is everything",
    "health check", "holistic view",
)

SITREP_ADDENDUM = (
    "\n\nThe user is asking for a status overview. A live data snapshot is provided below, already "
    "split into categories (node1, Containers, Network, NAS). Use ONLY those numbers for any claim "
    "about current system state — do not invent or guess numbers not present in the snapshot. "
    "Reproduce the same category headers as separate sections in your reply, each with its own short "
    "bullet points underneath — do NOT merge everything into one flat bullet list. You may write more "
    "than three sentences for this one answer."
)


def looks_like_sitrep_request(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in _SITREP_PHRASES)


async def _query(client: httpx.AsyncClient, expr: str) -> list[dict]:
    try:
        resp = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": expr}, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("data", {}).get("result", [])
    except Exception:
        return []


def _scalar(result: list[dict], default: float = 0.0) -> float:
    if not result:
        return default
    return float(result[0]["value"][1])


def _fmt_gib(num_bytes: float) -> str:
    return f"{num_bytes / (1024 ** 3):.1f} GiB"


def _fmt_uptime(seconds: float) -> str:
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    if days:
        return f"{days}d {hours}h"
    minutes = int((seconds % 3600) // 60)
    return f"{hours}h {minutes}m"


async def gather_sitrep() -> str:
    """Query Prometheus for a holistic homelab snapshot. Returns a formatted block, or an
    empty string if Prometheus is unreachable (caller should fall back gracefully)."""
    async with httpx.AsyncClient() as client:
        load1 = _scalar(await _query(client, 'node_load1{instance="node1"}'))
        load5 = _scalar(await _query(client, 'node_load5{instance="node1"}'))
        load15 = _scalar(await _query(client, 'node_load15{instance="node1"}'))
        mem_total = _scalar(await _query(client, 'node_memory_MemTotal_bytes{instance="node1"}'))
        mem_avail = _scalar(await _query(client, 'node_memory_MemAvailable_bytes{instance="node1"}'))
        boot_time = _scalar(await _query(client, 'node_boot_time_seconds{instance="node1"}'))
        fs_size = _scalar(await _query(client, 'node_filesystem_size_bytes{instance="node1", mountpoint="/"}'))
        fs_avail = _scalar(await _query(client, 'node_filesystem_avail_bytes{instance="node1", mountpoint="/"}'))

        containers_total = _scalar(await _query(client, "count(container_last_seen)"))
        unhealthy_result = await _query(client, 'container_health_state{name!="", instance="node1"} == 0')
        unhealthy_names = sorted({r["metric"].get("name", "?") for r in unhealthy_result})

        vuln_crit = _scalar(await _query(client, "sum(dockhand_vuln_critical)"))
        vuln_high = _scalar(await _query(client, "sum(dockhand_vuln_high)"))

        wan_result = await _query(client, "unpoller_wan_uptime_percentage")
        wans = [(r["metric"].get("wan_name", "?"), float(r["value"][1])) for r in wan_result]

        speedtest_down = _scalar(await _query(client, "unpoller_device_speedtest_download"))
        speedtest_up = _scalar(await _query(client, "unpoller_device_speedtest_upload"))
        speedtest_latency = _scalar(await _query(client, "unpoller_device_speedtest_latency_seconds"))
        speedtest_rundate = _scalar(await _query(client, "unpoller_device_speedtest_rundate_seconds"))

        disk_result = await _query(client, "synology_disk_health_status")
        bad_disks = [
            f"{r['metric'].get('nas', '?')}/{r['metric'].get('diskID', '?')}"
            for r in disk_result
            if r["value"][1] != "1"
        ]

    if not (load1 or mem_total or containers_total):
        return ""

    sections = ["**Live snapshot (Prometheus, just queried):**"]

    if mem_total:
        mem_used_pct = (1 - mem_avail / mem_total) * 100 if mem_total else 0
        disk_used_pct = (1 - fs_avail / fs_size) * 100 if fs_size else 0
        uptime = _fmt_uptime(time.time() - boot_time) if boot_time else "unknown"
        sections.append(
            "**node1**\n"
            f"- Uptime: {uptime}\n"
            f"- Load: {load1:.2f}/{load5:.2f}/{load15:.2f}\n"
            f"- RAM: {mem_used_pct:.0f}% used ({_fmt_gib(mem_total - mem_avail)}/{_fmt_gib(mem_total)})\n"
            f"- Disk: {disk_used_pct:.0f}% used"
        )

    if containers_total:
        health_note = "all healthy" if not unhealthy_names else f"{len(unhealthy_names)} unhealthy: {', '.join(unhealthy_names)}"
        container_lines = [f"- {int(containers_total)} total, {health_note}"]
        if vuln_crit or vuln_high:
            container_lines.append(f"- Vulnerabilities (dockhand): {int(vuln_crit)} critical, {int(vuln_high)} high")
        sections.append("**Containers**\n" + "\n".join(container_lines))

    if wans:
        wan_lines = [f"- {name} uptime {pct:.0f}%" for name, pct in wans]
        if speedtest_down or speedtest_up:
            age = _fmt_uptime(time.time() - speedtest_rundate) if speedtest_rundate else "unknown"
            wan_lines.append(
                f"- Last speedtest ({age} ago): {speedtest_down:.0f} Mbps down / "
                f"{speedtest_up:.0f} Mbps up, {speedtest_latency * 1000:.0f}ms latency"
            )
        sections.append("**Network**\n" + "\n".join(wan_lines))

    if disk_result:
        disk_note = "all disks healthy" if not bad_disks else f"attention needed: {', '.join(bad_disks)}"
        sections.append(f"**NAS (Synology)**\n- {disk_note}")

    return "\n\n".join(sections)
