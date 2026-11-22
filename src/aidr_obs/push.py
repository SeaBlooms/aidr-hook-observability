"""Server side of `aidr-obs ship`: accept new capture and Noma log lines pushed from a Mac.

Used when the dashboard runs somewhere else (a container on a home server). The tap and
the Noma hooks keep writing files on the Mac; the shipper sends what they append, and
this module spools it where ingest reads it: the legacy paths when a push names no
device, and devices/<device>/ once the shipper names its Mac.

Each stream is one Mac file name (`capture.jsonl` or `<agent>-guardrails-debug.log`).
The body also names the Mac (`device`). `push_state` remembers which Mac file (file_key)
and byte offset was accepted last, separately for each device, and the bytes are spooled
under `devices/<device>/` so a second Mac does not share the first Mac's offset:
- same file_key, offset equal to the stored end: append and advance;
- same file_key, any other offset: 409 with the expected offset, so a retry or a lost
  reply never duplicates or skips bytes;
- a new file_key (first push, or the Mac file rotated): accept the offset as the start.

Env: AIDR_OBS_PUSH_TOKEN (required; pushes are refused while it is unset),
AIDR_OBS_REMOTE=1 (Health reads the Mac's tap status from the latest heartbeat).
"""

import base64
import binascii
import hmac
import json
import os
import re
import threading
import time
from pathlib import Path

from . import logparse, paths

_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

CAPTURE_STREAM = "capture.jsonl"
MAX_BODY_BYTES = 1_000_000
MAX_STATUS_BYTES = 64_000
SPOOL_ROTATE_BYTES = 20 * 1024 * 1024
_LOCK = threading.Lock()


class PushError(Exception):
    def __init__(self, message: str, status: int = 400, extra: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


def token() -> str:
    return (os.environ.get("AIDR_OBS_PUSH_TOKEN") or "").strip()


def remote_mode() -> bool:
    return os.environ.get("AIDR_OBS_REMOTE", "").strip() == "1"


def check_auth(header: str | None) -> None:
    expected = token()
    if not expected:
        raise PushError("push is disabled: set AIDR_OBS_PUSH_TOKEN on the server", 403)
    scheme, _, given = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), expected.encode()):
        raise PushError("missing or invalid push token", 401)


def clean_device(device: str) -> str:
    """A Mac name safe to use as one path segment. Empty means the legacy single-Mac spool."""
    name = (device or "").strip()
    if not name:
        return ""
    if not _DEVICE_RE.fullmatch(name) or ".." in name:
        raise PushError("device name is not allowed")
    return name


def spool_path(stream: str, device: str = "") -> Path:
    """Server file for a stream. Only active (unrotated) names; the server rotates its own spool.

    A named device is stored under devices/<device>/ so each Mac has its own offset and file.
    """
    if stream == CAPTURE_STREAM:
        relative = Path(CAPTURE_STREAM)
    else:
        match = logparse.LOG_NAME_RE.match(stream)
        if match is None or match.group(2) is not None or "/" in stream:
            raise PushError("unknown stream: " + stream[:80])
        relative = Path("noma") / stream
    if device:
        return paths.data_dir() / "devices" / device / relative
    if stream == CAPTURE_STREAM:
        return paths.capture_path()
    return paths.noma_log_dir() / stream


def _append(path: Path, raw: bytes) -> None:
    paths.ensure_private_dir(path.parent)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    if size and size + len(raw) > SPOOL_ROTATE_BYTES:
        os.replace(path, path.with_name(path.name + ".1"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)


def accept(conn, body: dict) -> dict:
    """Validate one chunk, append it to the spool, and record the new end offset."""
    stream = body.get("stream")
    file_key = body.get("file_key")
    offset = body.get("offset")
    data_b64 = body.get("data_b64")
    raw_device = body.get("device", "")
    if raw_device is None:
        raw_device = ""
    if not isinstance(raw_device, str):
        raise PushError("device must be a string")
    device = clean_device(raw_device)
    if not isinstance(stream, str) or not stream:
        raise PushError("stream is required")
    if not isinstance(file_key, str) or not file_key or len(file_key) > 200:
        raise PushError("file_key is required")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise PushError("offset must be a non-negative integer")
    if not isinstance(data_b64, str):
        raise PushError("data_b64 is required")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PushError("data_b64 is not valid base64") from exc
    if raw and not raw.endswith(b"\n"):
        raise PushError("data must be whole lines")
    path = spool_path(stream, device)

    with _LOCK:
        row = conn.execute(
            "SELECT file_key, offset FROM push_state WHERE device = ? AND stream = ?", (device, stream)
        ).fetchone()
        if row is not None and row["file_key"] == file_key and row["offset"] != offset:
            raise PushError("offset mismatch", 409, {"expected": row["offset"]})
        if raw:
            _append(path, raw)
        end = offset + len(raw)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO push_state (device, stream, file_key, offset, updated_ms) VALUES (?,?,?,?,?)",
                (device, stream, file_key, end, int(time.time() * 1000)),
            )
    return {"stream": stream, "device": device, "offset": end, "accepted_bytes": len(raw)}


def heartbeat(conn, body: dict) -> dict:
    raw_device = body.get("device")
    status = body.get("status")
    if not isinstance(raw_device, str):
        raise PushError("device is required")
    device = clean_device(raw_device)
    if not device:
        raise PushError("device is required")
    if not isinstance(status, dict):
        raise PushError("status must be an object")
    status_json = json.dumps(status, default=str)
    if len(status_json) > MAX_STATUS_BYTES:
        raise PushError("status too large", 413)
    now = int(time.time() * 1000)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO push_heartbeat (device, status_json, received_ms) VALUES (?,?,?)",
            (device, status_json, now),
        )
    return {"device": device, "received_ms": now}


def heartbeats(conn) -> list[dict]:
    """Every Mac's last heartbeat, newest first."""
    rows = conn.execute(
        "SELECT device, status_json, received_ms FROM push_heartbeat ORDER BY received_ms DESC"
    ).fetchall()
    out = []
    for row in rows:
        try:
            status = json.loads(row["status_json"])
        except ValueError:
            status = {}
        out.append({"device": row["device"], "status": status, "received_ms": row["received_ms"]})
    return out


def latest_heartbeat(conn) -> dict | None:
    row = conn.execute(
        "SELECT device, status_json, received_ms FROM push_heartbeat ORDER BY received_ms DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        status = json.loads(row["status_json"])
    except ValueError:
        status = {}
    return {"device": row["device"], "status": status, "received_ms": row["received_ms"]}
