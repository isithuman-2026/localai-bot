import asyncio
import httpx
from datetime import datetime, timedelta

NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
CISA_KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"

_kev_data: list[dict] = []
_kev_fetched: datetime | None = None


async def _get_kev() -> list[dict]:
    global _kev_data, _kev_fetched
    now = datetime.utcnow()
    if _kev_fetched and (now - _kev_fetched) < timedelta(hours=24):
        return _kev_data
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(CISA_KEV_URL)
            resp.raise_for_status()
            _kev_data = resp.json().get("vulnerabilities", [])
            _kev_fetched = now
    except Exception as e:
        print(f"[vulnlookup] KEV fetch failed: {e}", flush=True)
    return _kev_data


async def _nvd_cve(cve_id: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(NVD_URL, params={"cveId": cve_id})
            resp.raise_for_status()
            items = resp.json().get("vulnerabilities", [])
            if not items:
                return None
            cve = items[0].get("cve", {})
            metrics = cve.get("metrics", {})
            score = None
            severity = None
            for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
                if key in metrics and metrics[key]:
                    m = metrics[key][0].get("cvssData", {})
                    score = m.get("baseScore")
                    severity = m.get("baseSeverity")
                    break
            descs = cve.get("descriptions", [])
            desc = next((d["value"] for d in descs if d.get("lang") == "en"), "")
            return {"id": cve_id, "score": score, "severity": severity, "description": desc[:200]}
    except Exception as e:
        print(f"[vulnlookup] NVD fetch {cve_id} failed: {e}", flush=True)
        return None


async def enrich_packages(packages: list[str]) -> str:
    """Returns a vuln context string to inject into an LLM prompt."""
    kev = await _get_kev()
    if not kev:
        return ""

    hits: list[dict] = []
    for pkg in packages:
        pkg_lower = pkg.lower().rstrip("0123456789-")  # strip trailing version digits
        for entry in kev:
            product = entry.get("product", "").lower()
            vendor = entry.get("vendorProject", "").lower()
            if pkg_lower and (pkg_lower in product or pkg_lower in vendor):
                hits.append({
                    "package": pkg,
                    "cve": entry.get("cveID", ""),
                    "name": entry.get("vulnerabilityName", ""),
                    "ransomware": entry.get("knownRansomwareCampaignUse", "Unknown"),
                })

    if not hits:
        return ""

    # Fetch CVSS scores for KEV hits (up to 5, with rate-limit spacing)
    enriched = []
    for hit in hits[:5]:
        detail = await _nvd_cve(hit["cve"])
        await asyncio.sleep(0.5)
        enriched.append({**hit, "nvd": detail})

    lines = ["CISA Known Exploited Vulnerabilities matched to installed packages:"]
    for e in enriched:
        nvd = e.get("nvd")
        score_str = f"CVSS {nvd['score']} ({nvd['severity']})" if nvd and nvd.get("score") else "CVSS unknown"
        ransomware = " | ransomware-linked" if e["ransomware"] == "Known" else ""
        lines.append(f"  {e['package']} → {e['cve']} {score_str}{ransomware}: {e['name']}")
        if nvd and nvd.get("description"):
            lines.append(f"    {nvd['description']}")

    return "\n".join(lines)
