import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import sysstats


def _result(value, metric=None):
    return [{"metric": metric or {}, "value": [1700000000.0, str(value)]}]


_FAKE_DATA = {
    'node_load1{instance="node1"}': _result(0.5),
    'node_load5{instance="node1"}': _result(0.6),
    'node_load15{instance="node1"}': _result(0.7),
    'node_memory_MemTotal_bytes{instance="node1"}': _result(20 * 1024 ** 3),
    'node_memory_MemAvailable_bytes{instance="node1"}': _result(10 * 1024 ** 3),
    'node_boot_time_seconds{instance="node1"}': _result(1700000000.0 - 9 * 86400),
    'node_filesystem_size_bytes{instance="node1", mountpoint="/"}': _result(700 * 1024 ** 3),
    'node_filesystem_avail_bytes{instance="node1", mountpoint="/"}': _result(500 * 1024 ** 3),
    "count(container_last_seen)": _result(40),
    'container_health_state{name!="", instance="node1"} == 0': [],
    "sum(dockhand_vuln_critical)": _result(0),
    "sum(dockhand_vuln_high)": _result(2),
    "unpoller_wan_uptime_percentage": _result(100, {"wan_name": "AussieBB"}),
    "unpoller_device_speedtest_download": _result(749),
    "unpoller_device_speedtest_upload": _result(38),
    "unpoller_device_speedtest_latency_seconds": _result(0.003),
    "unpoller_device_speedtest_rundate_seconds": _result(1700000000.0 - 3600),
    "synology_disk_health_status": [
        {"metric": {"nas": "alpha60", "diskID": "Disk 1"}, "value": [1700000000.0, "1"]},
    ],
}


def _make_client(overrides=None):
    data = {**_FAKE_DATA, **(overrides or {})}

    async def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"data": {"result": data.get(params["query"], [])}})
        return resp

    client = AsyncMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_gather_sitrep_formats_live_data():
    with patch("sysstats.httpx.AsyncClient", return_value=_make_client()):
        result = await sysstats.gather_sitrep()
    assert "**node1**" in result
    assert "**Containers**" in result
    assert "**Network**" in result
    assert "**NAS (Synology)**" in result
    assert "40 total" in result
    assert "all healthy" in result
    assert "2 high" in result
    assert "AussieBB uptime 100%" in result
    assert "749 Mbps down" in result
    assert "38 Mbps up" in result
    assert "3ms latency" in result
    assert "all disks healthy" in result


@pytest.mark.asyncio
async def test_gather_sitrep_lists_unhealthy_containers():
    overrides = {
        'container_health_state{name!="", instance="node1"} == 0': _result(0, {"name": "arr-sonarr"}),
    }
    with patch("sysstats.httpx.AsyncClient", return_value=_make_client(overrides)):
        result = await sysstats.gather_sitrep()
    assert "1 unhealthy: arr-sonarr" in result


@pytest.mark.asyncio
async def test_gather_sitrep_empty_when_prometheus_unreachable():
    async def fake_get(*a, **k):
        raise Exception("connection refused")

    client = AsyncMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("sysstats.httpx.AsyncClient", return_value=client):
        result = await sysstats.gather_sitrep()
    assert result == ""


@pytest.mark.parametrize("text,expected", [
    ("give me a sitrep of our lab environment", True),
    ("Hey can you tell me more about the cyber incident?", False),
    ("I need a sitrep", True),
    ("how's everything looking", True),
])
def test_looks_like_sitrep_request(text, expected):
    assert sysstats.looks_like_sitrep_request(text) is expected
