import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated dashboard home, Noma log dir and Claude dir; nothing touches the real ones."""
    dirs = {
        "home": tmp_path / "dash",
        "logs": tmp_path / "noma",
        "claude": tmp_path / "claude",
        "cursor": tmp_path / "cursor",
    }
    dirs["logs"].mkdir()
    dirs["claude"].mkdir()
    dirs["cursor"].mkdir()
    monkeypatch.setenv("AIDR_OBS_HOME", str(dirs["home"]))
    monkeypatch.setenv("AIDR_OBS_LOG_DIR", str(dirs["logs"]))
    monkeypatch.setenv("AIDR_OBS_CLAUDE_DIR", str(dirs["claude"]))
    monkeypatch.setenv("AIDR_OBS_CURSOR_DIR", str(dirs["cursor"]))
    for var in ("NOMA_DRYRUN", "AIDR_OBS_HOOK", "AIDR_OBS_NO_CAPTURE", "AIDR_OBS_CLAUDE_BIN"):
        monkeypatch.delenv(var, raising=False)
    return dirs


@pytest.fixture
def fixture_log(env):
    dest = env["logs"] / "claude-code-guardrails-debug.log"
    shutil.copy(FIXTURES / "claude-code-guardrails-debug.log", dest)
    return dest
