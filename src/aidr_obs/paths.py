"""Filesystem locations, each overridable by environment variable (tests rely on this)."""

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def data_dir() -> Path:
    """Where the dashboard keeps its own state: capture.jsonl, aidr.db, backups."""
    custom = os.environ.get("AIDR_OBS_HOME")
    return Path(custom) if custom else home() / ".aidr-obs"


def noma_log_dir() -> Path:
    """Where the Noma hooks write their debug logs (read-only for us)."""
    custom = os.environ.get("AIDR_OBS_LOG_DIR")
    return Path(custom) if custom else home() / ".noma"


def db_path() -> Path:
    return data_dir() / "aidr.db"


def capture_path() -> Path:
    return data_dir() / "capture.jsonl"


def claude_dir() -> Path:
    custom = os.environ.get("AIDR_OBS_CLAUDE_DIR")
    return Path(custom) if custom else home() / ".claude"


def cursor_dir() -> Path:
    custom = os.environ.get("AIDR_OBS_CURSOR_DIR")
    return Path(custom) if custom else home() / ".cursor"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path
