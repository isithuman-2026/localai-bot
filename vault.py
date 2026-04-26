"""
Search the Obsidian vault for notes relevant to an alert.
Prioritises TheLab/ (homelab-specific) then Areas/Security/.
Returns a markdown snippet to inject into triage context.
"""

import os
import re
from pathlib import Path

VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/vault"))
MAX_NOTE_CHARS = 800
MAX_NOTES = 3

# Search order: homelab-specific first, then security
SEARCH_DIRS = ["TheLab", "Areas/Security", "Areas/Infrastructure"]


def _keywords(text: str) -> list[str]:
    """Extract meaningful words from alert text for vault search."""
    stop = {"the", "a", "an", "is", "in", "on", "at", "to", "of", "and", "or", "for", "with", "from"}
    words = re.findall(r"[a-zA-Z]{4,}", text.lower())
    return list(dict.fromkeys(w for w in words if w not in stop))[:10]


def _score_file(path: Path, keywords: list[str]) -> int:
    """Count keyword hits in filename + first 2000 chars of content."""
    try:
        content = path.read_text(errors="ignore")[:2000].lower()
    except OSError:
        return 0
    name = path.stem.lower()
    score = 0
    for kw in keywords:
        if kw in name:
            score += 3
        score += content.count(kw)
    return score


def _snippet(path: Path) -> str:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return ""
    # Strip obsidian links/tags, collapse whitespace
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_NOTE_CHARS]


def search(alert_text: str) -> str:
    """Return vault context string relevant to alert_text, or empty string."""
    if not VAULT_PATH.exists():
        return ""

    keywords = _keywords(alert_text)
    if not keywords:
        return ""

    candidates: list[tuple[int, Path]] = []
    for dir_name in SEARCH_DIRS:
        search_dir = VAULT_PATH / dir_name
        if not search_dir.exists():
            continue
        for md in search_dir.rglob("*.md"):
            score = _score_file(md, keywords)
            if score > 0:
                candidates.append((score, md))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:MAX_NOTES]

    parts = []
    for score, path in top:
        rel = path.relative_to(VAULT_PATH)
        snippet = _snippet(path)
        if snippet:
            parts.append(f"### {rel}\n{snippet}")

    if not parts:
        return ""

    return "Relevant homelab notes from vault:\n\n" + "\n\n".join(parts)
