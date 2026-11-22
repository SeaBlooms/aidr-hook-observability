"""Mac side of a remote dashboard: push new capture and Noma log lines to `aidr-obs serve`.

Runs as its own process (a LaunchAgent), never inside the tap: the tap stays offline and
only appends to capture.jsonl. Every INTERVAL_SECONDS the shipper reads whole lines that
were appended to ~/.aidr-obs/capture.jsonl and each active ~/.noma/*guardrails-debug.log,
pushes them (POST /api/push, see push.py), and sends a heartbeat with `tap status`.

Offsets are kept per stream with the identity of the file they belong to, so a rotation
(the active file renamed to `.1`) finishes the old file before starting the new one.

Config: ~/.aidr-obs/ship.env (mode 600) or the environment, which wins.
  AIDR_OBS_PUSH_URL    default http://127.0.0.1:8770 (the SSH tunnel to the server)
  AIDR_OBS_PUSH_TOKEN  required; same value as the server's
  AIDR_OBS_SHIP_DEVICE default: this host name
Plain http is allowed only to loopback, so captures never cross the network unencrypted;
use the tunnel, or https.
"""

import base64
import json
import os
import plistlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import logparse, paths, push, tapinstall

INTERVAL_SECONDS = 15
CHUNK_BYTES = 512 * 1024
DEFAULT_URL = "http://127.0.0.1:8770"
LABEL = "com.seablooms.aidr-obs.ship"
TIMEOUT_SECONDS = 30
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class ShipError(Exception):
    """Config or transport failure; the message never includes the token or file contents."""


def env_path() -> Path:
    return paths.data_dir() / "ship.env"


def state_path() -> Path:
    return paths.data_dir() / "ship_state.json"


def log_path() -> Path:
    return paths.data_dir() / "ship.log"


def plist_path() -> Path:
    return paths.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def read_env_file(path: Path) -> dict:
    out = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def load_config() -> dict:
    file_env = read_env_file(env_path())

    def get(key: str, default: str = "") -> str:
        return (os.environ.get(key) or file_env.get(key) or default).strip()

    url = get("AIDR_OBS_PUSH_URL", DEFAULT_URL).rstrip("/")
    tok = get("AIDR_OBS_PUSH_TOKEN")
    if not tok:
        raise ShipError("AIDR_OBS_PUSH_TOKEN is not set (put it in " + str(env_path()) + ")")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ShipError("AIDR_OBS_PUSH_URL must be an http(s) URL")
    if parts.scheme == "http" and parts.hostname.lower() not in _LOOPBACK:
        raise ShipError("plain http is only allowed to loopback; use the SSH tunnel or https")
    return {"url": url, "token": tok, "device": get("AIDR_OBS_SHIP_DEVICE", socket.gethostname())}


def file_key(path: Path) -> str:
    st = path.stat()
    born = int(getattr(st, "st_birthtime", 0) or 0)
    return f"{st.st_dev}:{st.st_ino}:{born}"


def sources(data_dir: Path | None = None, log_dir: Path | None = None) -> list[tuple[str, Path]]:
    """(stream, active file) pairs: capture.jsonl plus each unrotated Noma debug log present."""
    data_dir = data_dir or paths.data_dir()
    log_dir = log_dir or paths.noma_log_dir()
    found = [(push.CAPTURE_STREAM, data_dir / push.CAPTURE_STREAM)]
    if log_dir.is_dir():
        for path in sorted(log_dir.iterdir()):
            m = logparse.LOG_NAME_RE.match(path.name)
            if m and m.group(2) is None and path.is_file():
                found.append((path.name, path))
    return found


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    paths.ensure_private_dir(paths.data_dir())
    tmp = state_path().with_name(state_path().name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, state_path())


def _post(cfg: dict, path: str, body: dict) -> tuple[int, dict]:
    raw = json.dumps(body).encode()
    req = urllib.request.Request(
        cfg["url"] + path,
        data=raw,
        method="POST",
        headers={
            "Authorization": "Bearer " + cfg["token"],
            "X-Aidr-Obs": "1",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}")
        except ValueError:
            detail = {}
        return exc.code, detail if isinstance(detail, dict) else {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ShipError("cannot reach " + cfg["url"] + " (is the SSH tunnel up?): " + type(exc).__name__) from exc


def _read_lines(path: Path, offset: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(CHUNK_BYTES)
    end = data.rfind(b"\n")
    return data[: end + 1] if end >= 0 else b""


def _send_file(cfg: dict, stream: str, path: Path, entry: dict, state: dict) -> int:
    """Push whole lines from entry['offset'] to the end of path. Returns bytes accepted."""
    sent = 0
    conflicts = 0
    while True:
        size = path.stat().st_size
        if entry["offset"] > size:
            entry["offset"] = 0
        data = _read_lines(path, entry["offset"])
        if not data:
            return sent
        status, reply = _post(
            cfg,
            "/api/push",
            {
                "stream": stream,
                "file_key": entry["file_key"],
                "offset": entry["offset"],
                "data_b64": base64.b64encode(data).decode("ascii"),
            },
        )
        if status == 409 and isinstance(reply.get("expected"), int):
            conflicts += 1
            if conflicts > 3:
                raise ShipError(f"{stream}: server keeps rejecting the offset")
            entry["offset"] = min(reply["expected"], size)
            state[stream] = entry
            save_state(state)
            continue
        if status != 200:
            raise ShipError(f"{stream}: push failed (HTTP {status}): {reply.get('error', '')}"[:300])
        entry["offset"] = int(reply["offset"])
        sent += len(data)
        state[stream] = entry
        save_state(state)


def baseline(state: dict, data_dir: Path | None = None, log_dir: Path | None = None) -> dict:
    """Mark every source as already shipped up to its current end (for use after migrating aidr.db)."""
    for stream, path in sources(data_dir, log_dir):
        if path.is_file():
            state[stream] = {"file_key": file_key(path), "offset": path.stat().st_size}
    save_state(state)
    return state


def ship_once(cfg: dict, state: dict, data_dir: Path | None = None, log_dir: Path | None = None) -> dict:
    """One pass over every source. Returns {stream: bytes accepted}."""
    out = {}
    for stream, active in sources(data_dir, log_dir):
        entry = state.get(stream)
        sent = 0
        if entry and active.is_file() and file_key(active) != entry.get("file_key"):
            rotated = active.with_name(active.name + ".1")
            if rotated.is_file() and file_key(rotated) == entry.get("file_key"):
                sent += _send_file(cfg, stream, rotated, entry, state)
            entry = None
        if not active.is_file():
            continue
        if entry is None:
            entry = {"file_key": file_key(active), "offset": 0}
        sent += _send_file(cfg, stream, active, entry, state)
        state[stream] = entry
        out[stream] = sent
    save_state(state)
    return out


def send_heartbeat(cfg: dict) -> None:
    try:
        status = tapinstall.status()
    except (OSError, ValueError) as exc:
        status = {"error": str(exc), "claude-code": {}, "cursor": {}}
    code, reply = _post(cfg, "/api/push/heartbeat", {"device": cfg["device"], "status": status})
    if code != 200:
        raise ShipError(f"heartbeat failed (HTTP {code}): {reply.get('error', '')}"[:300])


def run(from_end: bool = False, once: bool = False) -> int:
    cfg = load_config()
    state = load_state()
    if from_end:
        state = baseline(state)
    while True:
        try:
            sent = ship_once(cfg, state)
            send_heartbeat(cfg)
            total = sum(sent.values())
            if total or once:
                print(time.strftime("%Y-%m-%d %H:%M:%S"), "shipped", total, "bytes", flush=True)
        except ShipError as exc:
            print(time.strftime("%Y-%m-%d %H:%M:%S"), "error:", exc, file=sys.stderr, flush=True)
            if once:
                return 1
        if once:
            return 0
        time.sleep(INTERVAL_SECONDS)


# --- LaunchAgent ---------------------------------------------------------------------------------


def plist_bytes() -> bytes:
    log = str(log_path())
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [sys.executable, "-m", "aidr_obs.cli", "ship", "run"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "WorkingDirectory": str(paths.data_dir()),
            "StandardOutPath": log,
            "StandardErrorPath": log,
            "ProcessType": "Background",
        }
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def install(from_end: bool = False) -> dict:
    load_config()
    paths.ensure_private_dir(paths.data_dir())
    if from_end:
        baseline(load_state())
    dest = plist_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain + "/" + LABEL)
    dest.write_bytes(plist_bytes())
    boot = _launchctl("bootstrap", domain, str(dest))
    return {
        "plist": str(dest),
        "loaded": boot.returncode == 0,
        "launchctl": (boot.stderr or boot.stdout).strip()[:300],
        "log": str(log_path()),
        "from_end": from_end,
    }


def uninstall() -> dict:
    out = _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    dest = plist_path()
    removed = dest.exists()
    if removed:
        dest.unlink()
    return {"plist": str(dest), "removed": removed, "unloaded": out.returncode == 0}


def status() -> dict:
    loaded = _launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0
    try:
        cfg = load_config()
        config = {"url": cfg["url"], "device": cfg["device"], "token_set": True}
    except ShipError as exc:
        config = {"error": str(exc)}
    return {
        "plist": str(plist_path()),
        "installed": plist_path().exists(),
        "loaded": loaded,
        "config": config,
        "state": load_state(),
        "log": str(log_path()),
    }
