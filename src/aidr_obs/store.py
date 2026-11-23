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
    source_file TEXT,
    device TEXT NOT NULL DEFAULT ''
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
    hook_file TEXT,
    device TEXT NOT NULL DEFAULT ''
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
    run_id TEXT,
    device TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS captures_ts ON captures(ts_ms);

CREATE TABLE IF NOT EXISTS ingest_state (
    file_key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    offset INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS push_heartbeat (
    device TEXT PRIMARY KEY,
    status_json TEXT NOT NULL,
    received_ms INTEGER NOT NULL
);
"""

JEV_REVIEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jev_reviews (
    capture_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    stance TEXT NOT NULL,
    note TEXT NOT NULL,
    model TEXT,
    persona_sha256 TEXT,
    asked_at INTEGER NOT NULL,
    PRIMARY KEY (capture_id, backend)
);
CREATE INDEX IF NOT EXISTS jev_reviews_asked ON jev_reviews(asked_at);
"""

_FINGERPRINT_BYTES = 256
# How far a capture's start may sit from its run's first log line and still be joined.
_LINK_WINDOW_MS = 10_000


def connect(db_file: Path | None = None) -> sqlite3.Connection:
    db_file = db_file or paths.db_path()
    paths.ensure_private_dir(db_file.parent)
    new = not db_file.exists()
    # Several Macs push while the UI ingests. Wait for the writer instead of failing
    # the request, and let readers proceed during that write.
    conn = sqlite3.connect(db_file, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate_push_state(conn)
    _migrate_jev_reviews(conn)
    _migrate_device_columns(conn)
    if new:
        try:
            os.chmod(db_file, 0o600)
        except OSError:
            pass
    return conn


PUSH_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_state (
    device TEXT NOT NULL DEFAULT '',
    stream TEXT NOT NULL,
    file_key TEXT NOT NULL,
    offset INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    PRIMARY KEY (device, stream)
);
"""


def _migrate_push_state(conn: sqlite3.Connection) -> None:
    """Key the shipper offset by Mac as well as stream.

    A table from before that change has no device column. Its rows stay, under an empty
    device, so a new Mac does not inherit another Mac's byte offset.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(push_state)")]
    if cols and "device" not in cols:
        with conn:
            conn.execute("ALTER TABLE push_state RENAME TO push_state_v1")
    conn.executescript(PUSH_STATE_SCHEMA)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'push_state_v1'").fetchone():
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO push_state (device, stream, file_key, offset, updated_ms)"
                " SELECT '', stream, file_key, offset, updated_ms FROM push_state_v1"
            )
            conn.execute("DROP TABLE push_state_v1")


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> bool:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if not cols or name in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    return True


def device_of(path: Path, data_dir: Path | None = None) -> str:
    """Mac name for a file under data/devices/<name>/, or '' for the legacy spool."""
    root = (data_dir or paths.data_dir()) / "devices"
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return ""
    return relative.parts[0] if relative.parts else ""


def _backfill_capture_devices(conn: sqlite3.Connection, data_dir: Path | None = None) -> None:
    """Set device on captures already ingested from a per-Mac spool."""
    data_dir = data_dir or paths.data_dir()
    for path in capture_files(data_dir):
        device = device_of(path, data_dir)
        if not device:
            continue
        ids = []
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("id"):
                        ids.append(rec["id"])
        except OSError:
            continue
        if ids:
            conn.executemany(
                "UPDATE captures SET device = ? WHERE id = ? AND device = ''",
                [(device, capture_id) for capture_id in ids],
            )


def _migrate_device_columns(conn: sqlite3.Connection) -> None:
    """Remember which Mac a capture or log line came from.

    Existing rows stay blank until the spool files name a Mac. That backfill runs once,
    when the column is added, so later requests do not re-read the files.
    """
    added = _ensure_column(conn, "captures", "device", "device TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "log_lines", "device", "device TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "runs", "device", "device TEXT NOT NULL DEFAULT ''")
    if added:
        with conn:
            _backfill_capture_devices(conn)


def _migrate_jev_reviews(conn: sqlite3.Connection) -> None:
    """Create jev_reviews, or rekey a pre-backend table by (capture_id, backend).

    Rows from before backend selection all came from the AI Gateway, so they become 'gateway'.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jev_reviews)")]
    if cols and "backend" not in cols:
        with conn:
            conn.execute("ALTER TABLE jev_reviews RENAME TO jev_reviews_v1")
            conn.execute("DROP INDEX IF EXISTS jev_reviews_asked")
    conn.executescript(JEV_REVIEWS_SCHEMA)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jev_reviews_v1'").fetchone():
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO jev_reviews"
                " (capture_id, backend, stance, note, model, persona_sha256, asked_at)"
                " SELECT capture_id, 'gateway', stance, note, model, persona_sha256, asked_at FROM jev_reviews_v1"
            )
            conn.execute("DROP TABLE jev_reviews_v1")


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


def log_dirs(log_dir: Path | None = None, data_dir: Path | None = None) -> list[Path]:
    """The legacy Noma log dir, plus one noma/ dir per Mac that ships to this server."""
    found = [log_dir or paths.noma_log_dir()]
    devices = (data_dir or paths.data_dir()) / "devices"
    if devices.is_dir():
        for child in sorted(devices.iterdir()):
            noma = child / "noma"
            if child.is_dir() and noma.is_dir():
                found.append(noma)
    return found


def ingest_logs(conn: sqlite3.Connection, log_dir: Path | None = None, data_dir: Path | None = None) -> int:
    added = 0
    for directory in log_dirs(log_dir, data_dir):
        added += _ingest_log_dir(conn, directory)
    return added


def _ingest_log_dir(conn: sqlite3.Connection, log_dir: Path) -> int:
    added = 0
    device = device_of(log_dir)
    for path, agent in logparse.discover_log_files(log_dir):
        try:
            data = _read_new(conn, path)
        except OSError:
            continue
        seen: dict[str, int] = {}
        for line in logparse.parse_lines(data.decode("utf-8", "replace")):
            n = seen.get(line.raw, 0)
            seen[line.raw] = n + 1
            cur = conn.execute(
                "INSERT OR IGNORE INTO log_lines"
                " (id, agent, ts, ts_ms, level, pid, location, message, source_file, device)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                    device or device_of(path),
                ),
            )
            added += cur.rowcount
    return added


def rebuild_runs(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM runs")
    total = 0
    groups = conn.execute("SELECT DISTINCT agent, COALESCE(device, '') AS device FROM log_lines").fetchall()
    for agent, device in groups:
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
                "SELECT ts, ts_ms, level, pid, location, message FROM log_lines"
                " WHERE agent = ? AND COALESCE(device, '') = ? ORDER BY ts_ms, rowid",
                (agent, device),
            )
        ]
        for run in logparse.group_runs(agent, lines):
            run.device = device
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, agent, pid, start_ts, start_ms, end_ms, duration_ms, event,"
                " stdin_bytes, key_source, installed_config, mcp_artifacts, discovery_json, project_root, outcome,"
                " http_status, url, post_bytes, sender, retries, fallbacks, error_count, hook_file, device)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    device,
                ),
            )
            total += 1
    return total


def _capture_pair(base: Path) -> list[Path]:
    rotated = base.with_name(base.name + ".1")
    return [path for path in (rotated, base) if path.is_file()]


def capture_files(data_dir: Path | None = None) -> list[Path]:
    """Legacy capture.jsonl plus one capture.jsonl per Mac under devices/."""
    data_dir = data_dir or paths.data_dir()
    found = _capture_pair(data_dir / "capture.jsonl")
    devices = data_dir / "devices"
    if devices.is_dir():
        for child in sorted(devices.iterdir()):
            if child.is_dir():
                found.extend(_capture_pair(child / "capture.jsonl"))
    return found


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
        device = device_of(path, data_dir)
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
                " stdout_text, stdout_truncated, stderr_bytes, tap_error, device) VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*_capture_row(rec), device),
            )
            added += cur.rowcount
    return added


def link_captures(conn: sqlite3.Connection) -> None:
    """Join each capture to the debug-log run of its child hook process (same pid, close in time).

    Nearest ``start_ms`` within the window wins. SQLite rejects ``ORDER BY`` expressions that
    reference the outer ``captures`` row (``no such column: captures.ts_ms``), so the distance
    is matched with ``MIN`` instead; ties break on the earliest ``start_ms``.
    """
    conn.execute(
        """
        UPDATE captures SET run_id = (
            SELECT r.run_id FROM runs r
            WHERE r.pid = captures.child_pid AND r.agent = captures.agent
              AND COALESCE(r.device, '') = COALESCE(captures.device, '')
              AND ABS(r.start_ms - captures.ts_ms) <= ?
              AND ABS(r.start_ms - captures.ts_ms) = (
                  SELECT MIN(ABS(r2.start_ms - captures.ts_ms)) FROM runs r2
                  WHERE r2.pid = captures.child_pid AND r2.agent = captures.agent
                    AND COALESCE(r2.device, '') = COALESCE(captures.device, '')
                    AND ABS(r2.start_ms - captures.ts_ms) <= ?
              )
            ORDER BY r.start_ms LIMIT 1
        )
        WHERE child_pid IS NOT NULL
        """,
        (_LINK_WINDOW_MS, _LINK_WINDOW_MS),
    )


def ingest(conn: sqlite3.Connection, log_dir: Path | None = None, data_dir: Path | None = None) -> dict:
    with conn:
        lines = ingest_logs(conn, log_dir, data_dir)
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
        reviews = conn.execute("DELETE FROM jev_reviews WHERE capture_id NOT IN (SELECT id FROM captures)").rowcount
        rebuild_runs(conn)
        link_captures(conn)
    return {
        "captures_deleted": captures,
        "log_lines_deleted": lines,
        "capture_records_dropped": rewritten,
        "jev_reviews_deleted": reviews,
    }
