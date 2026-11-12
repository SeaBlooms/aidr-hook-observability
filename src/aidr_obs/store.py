"""SQLite store and incremental ingest of the Noma debug logs and the tap's capture.jsonl.

Ingest is incremental and dedup-safe:
- Each physical file is tracked by (device, inode) plus a fingerprint of its first
  bytes, with the byte offset already read. RotatingFileHandler renames on rotation,
  which keeps the inode, so a rotated file is never re-read; a new file starts at 0.
- Every log line is keyed by a hash of (agent, line text, occurrence), and every
  capture record by its id, so re-reading a file never duplicates rows.

Runs are rebuilt from all stored lines on each ingest, which also joins runs that
were split across two ingests.
"""

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from . import logparse, paths, verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS log_lines (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    ts TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    level TEXT NOT NULL,
    pid INTEGER NOT NULL,
    location TEXT,
    message TEXT NOT NULL,
    source_file TEXT
);
CREATE INDEX IF NOT EXISTS log_lines_agent_pid ON log_lines(agent, pid, ts_ms);
CREATE INDEX IF NOT EXISTS log_lines_level ON log_lines(level, ts_ms);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    pid INTEGER NOT NULL,
    start_ts TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    event TEXT,
    stdin_bytes INTEGER,
    key_source TEXT,
    installed_config TEXT,
    mcp_artifacts INTEGER,
    discovery_json TEXT,
    project_root TEXT,
    outcome TEXT NOT NULL,
    http_status INTEGER,
    url TEXT,
    post_bytes INTEGER,
    sender TEXT,
    retries INTEGER,
    fallbacks INTEGER,
    error_count INTEGER,
    hook_file TEXT
);
CREATE INDEX IF NOT EXISTS runs_start ON runs(start_ms);

CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    agent TEXT,
    event TEXT,
    tool_name TEXT,
    session_id TEXT,
    cwd TEXT,
    latency_ms INTEGER,
    exit_code INTEGER,
    timed_out INTEGER,
    child_pid INTEGER,
    hook_path TEXT,
    hook_version TEXT,
    dry_run INTEGER,
    verdict TEXT,
    reason TEXT,
    stdin_json TEXT,
    stdout_text TEXT,
    stdout_truncated INTEGER,
    stderr_bytes INTEGER,
    tap_error TEXT,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS captures_ts ON captures(ts_ms);

CREATE TABLE IF NOT EXISTS ingest_state (
    file_key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    offset INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL
);
"""

_FINGERPRINT_BYTES = 256
# How far a capture's start may sit from its run's first log line and still be joined.
_LINK_WINDOW_MS = 10_000


def connect(db_file: Path | None = None) -> sqlite3.Connection:
    db_file = db_file or paths.db_path()
    paths.ensure_private_dir(db_file.parent)
    new = not db_file.exists()
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if new:
        try:
            os.chmod(db_file, 0o600)
        except OSError:
            pass
    return conn


def _fingerprint(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read(_FINGERPRINT_BYTES)).hexdigest()


def _read_new(conn: sqlite3.Connection, path: Path) -> bytes:
    """Bytes appended to path since the last ingest, up to the last complete line."""
    st = path.stat()
    key = str(st.st_dev) + ":" + str(st.st_ino)
    fp = _fingerprint(path)
    row = conn.execute("SELECT fingerprint, offset FROM ingest_state WHERE file_key = ?", (key,)).fetchone()
    offset = 0
    if row and st.st_size >= row["offset"]:
        # A fingerprint taken before the file reached 256 bytes changes as it grows; still the same file.
        if row["fingerprint"] == fp or row["offset"] < _FINGERPRINT_BYTES:
            offset = row["offset"]
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    data = data[: end + 1] if end >= 0 else b""
    conn.execute(
        "INSERT OR REPLACE INTO ingest_state (file_key, path, fingerprint, offset, updated_ms) VALUES (?,?,?,?,?)",
        (key, str(path), fp, offset + len(data), int(time.time() * 1000)),
    )
    return data


def _line_id(agent: str, raw: str, occurrence: int) -> str:
    return hashlib.sha256((agent + "\0" + raw + "\0" + str(occurrence)).encode()).hexdigest()[:32]


def ingest_logs(conn: sqlite3.Connection, log_dir: Path | None = None) -> int:
    added = 0
    for path, agent in logparse.discover_log_files(log_dir or paths.noma_log_dir()):
        try:
            data = _read_new(conn, path)
        except OSError:
            continue
        seen: dict[str, int] = {}
        for line in logparse.parse_lines(data.decode("utf-8", "replace")):
            n = seen.get(line.raw, 0)
            seen[line.raw] = n + 1
            cur = conn.execute(
                "INSERT OR IGNORE INTO log_lines (id, agent, ts, ts_ms, level, pid, location, message, source_file)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    _line_id(agent, line.raw, n),
                    agent,
                    line.ts,
                    line.ts_ms,
                    line.level,
                    line.pid,
                    line.where,
                    line.message,
                    path.name,
                ),
            )
            added += cur.rowcount
    return added


def rebuild_runs(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM runs")
    total = 0
    agents = [r[0] for r in conn.execute("SELECT DISTINCT agent FROM log_lines")]
    for agent in agents:
        lines = [
            logparse.LogLine(
                ts=r["ts"],
                ts_ms=r["ts_ms"],
                level=r["level"],
                pid=r["pid"],
                where=r["location"] or "",
                message=r["message"],
                raw="",
            )
            for r in conn.execute(
                "SELECT ts, ts_ms, level, pid, location, message FROM log_lines WHERE agent = ? ORDER BY ts_ms, rowid",
                (agent,),
            )
        ]
        for run in logparse.group_runs(agent, lines):
            conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run.run_id,
                    run.agent,
                    run.pid,
                    run.start_ts,
                    run.start_ms,
                    run.end_ms,
                    run.duration_ms,
                    run.event,
                    run.stdin_bytes,
                    run.key_source,
                    run.installed_config,
                    run.mcp_artifacts,
                    json.dumps(run.discovery),
                    run.project_root,
                    run.outcome,
                    run.http_status,
                    run.url,
                    run.post_bytes,
                    run.sender,
                    run.retries,
                    run.fallbacks,
                    run.error_count,
                    run.hook_file,
                ),
            )
            total += 1
    return total


def capture_files(data_dir: Path | None = None) -> list[Path]:
    base = paths.capture_path() if data_dir is None else data_dir / "capture.jsonl"
    rotated = base.with_name(base.name + ".1")
    return [p for p in (rotated, base) if p.is_file()]


def _capture_row(rec: dict) -> tuple:
    v = rec.get("verdict")
    reason = rec.get("reason")
    if not v:
        c = verdict.classify(rec.get("stdout") or "", rec.get("event"), bool(rec.get("dry_run")))
        v, reason = c["verdict"], c["reason"]
    stdin = rec.get("stdin")
    return (
        rec["id"],
        rec.get("ts") or "",
        int(rec.get("ts_ms") or 0),
        rec.get("agent"),
        rec.get("event"),
        rec.get("tool_name"),
        rec.get("session_id"),
        rec.get("cwd"),
        rec.get("latency_ms"),
        rec.get("exit_code"),
        int(bool(rec.get("timed_out"))),
        rec.get("child_pid"),
        rec.get("hook_path"),
        rec.get("hook_version"),
        int(bool(rec.get("dry_run"))),
        v,
        reason,
        stdin if isinstance(stdin, str) else json.dumps(stdin, ensure_ascii=False),
        rec.get("stdout"),
        int(bool(rec.get("stdout_truncated"))),
        rec.get("stderr_bytes"),
        rec.get("tap_error"),
    )


def ingest_captures(conn: sqlite3.Connection, data_dir: Path | None = None) -> int:
    added = 0
    for path in capture_files(data_dir):
        try:
            data = _read_new(conn, path)
        except OSError:
            continue
        for raw in data.decode("utf-8", "replace").splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not rec.get("id"):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO captures (id, ts, ts_ms, agent, event, tool_name, session_id, cwd, latency_ms,"
                " exit_code, timed_out, child_pid, hook_path, hook_version, dry_run, verdict, reason, stdin_json,"
                " stdout_text, stdout_truncated, stderr_bytes, tap_error) VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _capture_row(rec),
            )
            added += cur.rowcount
    return added


def link_captures(conn: sqlite3.Connection) -> None:
    """Join each capture to the debug-log run of its child hook process (same pid, close in time)."""
    conn.execute(
        """
        UPDATE captures SET run_id = (
            SELECT r.run_id FROM runs r
            WHERE r.pid = captures.child_pid AND r.agent = captures.agent
              AND ABS(r.start_ms - captures.ts_ms) <= ?
            ORDER BY ABS(r.start_ms - captures.ts_ms) LIMIT 1
        )
        WHERE child_pid IS NOT NULL
        """,
        (_LINK_WINDOW_MS,),
    )


def ingest(conn: sqlite3.Connection, log_dir: Path | None = None, data_dir: Path | None = None) -> dict:
    with conn:
        lines = ingest_logs(conn, log_dir)
        captures = ingest_captures(conn, data_dir)
        runs = rebuild_runs(conn)
        link_captures(conn)
    return {"log_lines_added": lines, "captures_added": captures, "runs": runs}


def prune(conn: sqlite3.Connection, days: int, data_dir: Path | None = None) -> dict:
    """Drop captures and stored log lines older than `days`, and rewrite capture.jsonl without them.

    Only the dashboard's own files are touched; the Noma debug logs are never modified.
    """
    cutoff = int((time.time() - days * 86400) * 1000)
    rewritten = 0
    for path in capture_files(data_dir):
        kept = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    if int(json.loads(raw).get("ts_ms") or 0) < cutoff:
                        rewritten += 1
                        continue
                except (ValueError, AttributeError):
                    pass
                kept.append(raw)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, path)
    with conn:
        captures = conn.execute("DELETE FROM captures WHERE ts_ms < ?", (cutoff,)).rowcount
        lines = conn.execute("DELETE FROM log_lines WHERE ts_ms < ?", (cutoff,)).rowcount
        rebuild_runs(conn)
        link_captures(conn)
    return {"captures_deleted": captures, "log_lines_deleted": lines, "capture_records_dropped": rewritten}
