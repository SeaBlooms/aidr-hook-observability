"""The tap must be byte-for-byte transparent, whatever happens to the capture step."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TAP = Path(__file__).resolve().parents[1] / "src" / "aidr_obs" / "tap.py"
VERDICT = (
    b'{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",'
    b'"permissionDecisionReason":"reads ~/.ssh"}}'
)
EVENT = {
    "hook_event_name": "PreToolUse",
    "session_id": "s-1",
    "cwd": "/home/alice/demo",
    "tool_name": "Bash",
    "tool_input": {"command": "cat ~/.ssh/id_rsa", "env": "API_KEY=supersecret"},
}


def _fake_hook(tmp_path, body):
    hook = tmp_path / "plugin" / "scripts" / "hook.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("import sys, os, time\n" + body)
    return hook


def _run_tap(env_dirs, stdin, args=(), **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NOMA_")}
    env.update({"AIDR_OBS_HOME": str(env_dirs["home"]), "AIDR_OBS_CLAUDE_DIR": str(env_dirs["claude"])})
    env.update(extra)
    return subprocess.run([sys.executable, str(TAP), *args], input=stdin, capture_output=True, env=env, timeout=30)


def _write_config(env_dirs, config):
    env_dirs["home"].mkdir(parents=True, exist_ok=True)
    (env_dirs["home"] / "config.json").write_text(json.dumps(config))


def _records(env_dirs):
    path = env_dirs["home"] / "capture.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def verdict_hook(tmp_path):
    return _fake_hook(
        tmp_path, f"sys.stdin.buffer.read()\nsys.stdout.buffer.write({VERDICT!r})\nsys.stderr.write('diag')\n"
    )


def test_verdict_passes_through_byte_for_byte(env, verdict_hook):
    out = _run_tap(env, json.dumps(EVENT).encode(), AIDR_OBS_HOOK=str(verdict_hook))
    assert out.stdout == VERDICT
    assert out.stderr == b"diag"
    assert out.returncode == 0


def test_capture_record_is_redacted_and_private(env, verdict_hook):
    _run_tap(env, json.dumps(EVENT).encode(), AIDR_OBS_HOOK=str(verdict_hook))
    (rec,) = _records(env)
    assert rec["event"] == "PreToolUse" and rec["tool_name"] == "Bash" and rec["session_id"] == "s-1"
    assert rec["verdict"] == "block" and rec["reason"] == "reads ~/.ssh"
    assert rec["child_pid"] and rec["latency_ms"] >= 0 and rec["exit_code"] == 0
    assert "supersecret" not in json.dumps(rec)
    assert rec["stdin"]["tool_input"]["command"] == "cat ~/.ssh/id_rsa"
    assert oct(os.stat(env["home"] / "capture.jsonl").st_mode & 0o777) == "0o600"
    assert oct(os.stat(env["home"]).st_mode & 0o777) == "0o700"


def test_stdin_and_env_reach_the_hook_unchanged(env, tmp_path):
    hook = _fake_hook(
        tmp_path,
        "data = sys.stdin.buffer.read()\nsys.stdout.buffer.write(data + os.environ.get('MARKER', '').encode())\n",
    )
    raw = b'{"hook_event_name":"Stop","x":"\xc3\xa9"}  trailing'
    out = _run_tap(env, raw, AIDR_OBS_HOOK=str(hook), MARKER="|env-ok")
    assert out.stdout == raw + b"|env-ok"


def test_child_exit_code_is_relayed(env, tmp_path):
    hook = _fake_hook(tmp_path, "sys.stdout.write('x')\nsys.exit(2)\n")
    out = _run_tap(env, b"{}", AIDR_OBS_HOOK=str(hook))
    assert out.stdout == b"x" and out.returncode == 2


def test_unwritable_capture_dir_does_not_change_output(env, verdict_hook, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    out = _run_tap(env, json.dumps(EVENT).encode(), AIDR_OBS_HOOK=str(verdict_hook), AIDR_OBS_HOME=str(blocker / "sub"))
    assert out.stdout == VERDICT and out.returncode == 0


def test_capture_crash_does_not_change_output(env, verdict_hook):
    env["home"].mkdir(parents=True)
    (env["home"] / "capture.jsonl").mkdir()
    out = _run_tap(env, json.dumps(EVENT).encode(), AIDR_OBS_HOOK=str(verdict_hook))
    assert out.stdout == VERDICT and out.returncode == 0


def test_missing_hook_fails_open(env):
    out = _run_tap(env, json.dumps(EVENT).encode(), AIDR_OBS_HOOK="/nonexistent/hook.py")
    assert out.stdout == b"" and out.returncode == 0
    (rec,) = _records(env)
    assert rec["tap_error"] == "Noma claude-code hook.py not found"


def test_timeout_fails_open(env, tmp_path):
    hook = _fake_hook(tmp_path, "sys.stdout.write('late'); sys.stdout.flush(); time.sleep(10)\n")
    out = _run_tap(env, b"{}", AIDR_OBS_HOOK=str(hook), AIDR_OBS_CHILD_TIMEOUT="0.5")
    assert out.stdout == b"" and out.returncode == 0
    assert _records(env)[0]["timed_out"] is True


def test_dry_run_payload_is_recorded_but_not_relayed(env, tmp_path):
    hook = _fake_hook(tmp_path, "sys.stdout.write(sys.stdin.read())\n")
    out = _run_tap(env, json.dumps(EVENT).encode(), AIDR_OBS_HOOK=str(hook), NOMA_DRYRUN="1")
    assert out.stdout == b"" and out.returncode == 0
    rec = _records(env)[0]
    assert rec["dry_run"] is True and rec["verdict"] == "dry_run" and rec["relayed"] is False
    assert json.loads(rec["stdout"])["tool_name"] == "Bash"
    assert "supersecret" not in rec["stdout"]


def test_dry_run_relay_can_be_enabled(env, tmp_path):
    hook = _fake_hook(tmp_path, "sys.stdout.write('payload')\n")
    _write_config(env, {"relay_dry_run_stdout": True})
    out = _run_tap(env, b"{}", AIDR_OBS_HOOK=str(hook), NOMA_DRYRUN="1")
    assert out.stdout == b"payload"


def test_claude_tap_passes_cursor_events_through(env, verdict_hook):
    event = dict(EVENT, cursor_version="3.1.0")
    out = _run_tap(env, json.dumps(event).encode(), AIDR_OBS_HOOK=str(verdict_hook))
    assert out.stdout == b"" and out.returncode == 0
    assert _records(env) == []


CURSOR_EVENT = {
    "hook_event_name": "beforeShellExecution",
    "conversation_id": "conv-1",
    "cursor_version": "3.1.0",
    "workspace_roots": ["/home/alice/demo"],
    "command": "cat .env",
}
CURSOR_DENY = b'{"permission":"deny","user_message":"Blocked by Noma: secrets file","agent_message":"blocked"}'


def test_cursor_agent_uses_configured_hook_and_env(env, tmp_path):
    hook = _fake_hook(
        tmp_path,
        "sys.stdin.read()\n"
        "assert os.environ['NOMA_DEBUG'] == '1' and os.environ['CURSOR_PLUGIN_ROOT']\n"
        "assert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'\n"
        f"sys.stdout.buffer.write({CURSOR_DENY!r})\n",
    )
    (hook.parent.parent / ".cursor-plugin").mkdir()
    (hook.parent.parent / ".cursor-plugin" / "plugin.json").write_text('{"version": "1.6.0"}')
    _write_config(env, {"agents": {"cursor": {"hook": str(hook), "env": {"NOMA_DEBUG": "1"}}}})
    out = _run_tap(env, json.dumps(CURSOR_EVENT).encode(), args=["--agent", "cursor"])
    assert out.stdout == CURSOR_DENY and out.returncode == 0
    (rec,) = _records(env)
    assert rec["agent"] == "cursor" and rec["event"] == "beforeShellExecution" and rec["tool_name"] == "Shell"
    assert rec["session_id"] == "conv-1" and rec["cwd"] == "/home/alice/demo" and rec["hook_version"] == "1.6.0"
    assert rec["verdict"] == "block" and rec["reason"] == "Blocked by Noma: secrets file"


def test_cursor_agent_without_config_fails_open(env):
    out = _run_tap(env, json.dumps(CURSOR_EVENT).encode(), args=["--agent", "cursor"])
    assert out.stdout == b"" and out.returncode == 0
    assert _records(env)[0]["tap_error"] == "Noma cursor hook.py not found"


def test_resolves_newest_installed_plugin(env, tmp_path):
    cache = env["claude"] / "plugins" / "cache" / "noma" / "guardrails"
    for version in ("2.9.0", "2.10.0"):
        hook = cache / version / "scripts" / "hook.py"
        hook.parent.mkdir(parents=True)
        hook.write_text(f"import sys; sys.stdout.write({version!r})\n")
    (env["claude"] / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {"version": 2, "plugins": {"guardrails@noma": [{"installPath": str(cache / "2.9.0"), "version": "2.9.0"}]}}
        )
    )
    out = _run_tap(env, b"{}")
    assert out.stdout == b"2.10.0"
    assert _records(env)[0]["hook_version"] == "2.10.0"
