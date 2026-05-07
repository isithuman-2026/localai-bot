"""
SQLite + FTS5 persistent memory for JARVIS.

Tables:
  facts         — learned knowledge about services/hosts
  observations  — alert triage history (with structured fields)
  suppressions  — known-noisy patterns to skip
  alert_history — per-fingerprint occurrence tracking for adaptive suppression
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "jarvis_memory.db"

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS facts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    topic   TEXT NOT NULL,
    content TEXT NOT NULL,
    source  TEXT NOT NULL DEFAULT '',
    ts      INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    topic, content, content=facts, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, topic, content) VALUES (new.id, new.topic, new.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, topic, content)
    VALUES ('delete', old.id, old.topic, old.content);
END;

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    host        TEXT NOT NULL DEFAULT '',
    event       TEXT NOT NULL,
    summary     TEXT NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    root_cause  TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL DEFAULT 0.0,
    severity    TEXT NOT NULL DEFAULT '',
    ts          INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS suppressions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL UNIQUE,
    reason  TEXT NOT NULL DEFAULT '',
    expires INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alert_history (
    fingerprint          TEXT NOT NULL UNIQUE,
    first_seen           INTEGER NOT NULL DEFAULT (unixepoch()),
    last_seen            INTEGER NOT NULL DEFAULT (unixepoch()),
    occurrence_count     INTEGER NOT NULL DEFAULT 1,
    last_root_cause      TEXT NOT NULL DEFAULT '',
    last_confidence      REAL NOT NULL DEFAULT 0.0,
    last_severity        TEXT NOT NULL DEFAULT '',
    auto_suppressed      INTEGER NOT NULL DEFAULT 0,
    auto_suppress_reason TEXT NOT NULL DEFAULT ''
);
"""

_OBSERVATIONS_MIGRATIONS = [
    "ALTER TABLE observations ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE observations ADD COLUMN root_cause TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE observations ADD COLUMN confidence REAL NOT NULL DEFAULT 0.0",
    "ALTER TABLE observations ADD COLUMN severity TEXT NOT NULL DEFAULT ''",
]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(observations)")}
    for stmt in _OBSERVATIONS_MIGRATIONS:
        col = stmt.split("ADD COLUMN ")[1].split()[0]
        if col not in existing:
            try:
                conn.execute(stmt)
                conn.commit()
            except Exception:
                pass


def write_fact(topic: str, content: str, source: str = "") -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO facts (topic, content, source) VALUES (?, ?, ?)",
            (topic, content, source),
        )
        return cur.lastrowid


def _fts_query(text: str) -> str:
    import re
    words = re.findall(r"[a-zA-Z0-9]{3,}", text)
    return " ".join(words[:20])


def search_facts(query: str, limit: int = 5) -> list[dict]:
    fts_q = _fts_query(query)
    if not fts_q:
        return []
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT f.id, f.topic, f.content, f.source, f.ts
            FROM facts_fts fts
            JOIN facts f ON fts.rowid = f.id
            WHERE facts_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_q, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def log_observation(
    event: str,
    summary: str,
    host: str = "",
    fingerprint: str = "",
    root_cause: str = "",
    confidence: float = 0.0,
    severity: str = "",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO observations (host, event, summary, fingerprint, root_cause, confidence, severity) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (host, event, summary, fingerprint, root_cause, confidence, severity),
        )
        return cur.lastrowid


def recent_observations(limit: int = 10) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM observations ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def is_suppressed(text: str) -> tuple[bool, str]:
    now = int(time.time())
    with _connect() as conn:
        rows = conn.execute(
            "SELECT pattern, reason, expires FROM suppressions WHERE expires = 0 OR expires > ?",
            (now,),
        ).fetchall()
    for row in rows:
        if row["pattern"].lower() in text.lower():
            return True, row["reason"]
    return False, ""


def add_suppression(pattern: str, reason: str = "", expires: int = 0) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR REPLACE INTO suppressions (pattern, reason, expires) VALUES (?, ?, ?)",
            (pattern, reason, expires),
        )
        return cur.lastrowid


def upsert_alert_history(
    fingerprint: str,
    root_cause: str,
    confidence: float,
    severity: str,
) -> dict:
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO alert_history (fingerprint, first_seen, last_seen, occurrence_count, last_root_cause, last_confidence, last_severity)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                last_seen        = excluded.last_seen,
                occurrence_count = occurrence_count + 1,
                last_root_cause  = excluded.last_root_cause,
                last_confidence  = excluded.last_confidence,
                last_severity    = excluded.last_severity
            """,
            (fingerprint, now, now, root_cause, confidence, severity),
        )
        row = conn.execute(
            "SELECT * FROM alert_history WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
    return dict(row)


def get_alert_history(fingerprint: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM alert_history WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
    return dict(row) if row else None


def check_auto_suppress(
    fingerprint: str,
    min_occurrences: int = 5,
    min_confidence: float = 0.80,
) -> tuple[bool, str]:
    history = get_alert_history(fingerprint)
    if not history:
        return False, ""
    if history["auto_suppressed"]:
        return True, history["auto_suppress_reason"]
    if (
        history["occurrence_count"] >= min_occurrences
        and history["last_confidence"] >= min_confidence
        and history["last_severity"] in ("low", "medium")
    ):
        reason = (
            f"auto-suppressed after {history['occurrence_count']} occurrences "
            f"({history['last_severity']}, {history['last_confidence']:.0%} confidence)"
        )
        _mark_auto_suppressed(fingerprint, reason)
        return True, reason
    return False, ""


def _mark_auto_suppressed(fingerprint: str, reason: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE alert_history SET auto_suppressed = 1, auto_suppress_reason = ? WHERE fingerprint = ?",
            (reason, fingerprint),
        )
