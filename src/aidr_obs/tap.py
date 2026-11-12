#!/usr/bin/env python3
"""Transparent capture wrapper around a Noma guardrails hook (Claude Code or Cursor).

The agent runs this in place of the Noma hook (see `aidr-obs tap install`):

    uv run --no-project <repo>/src/aidr_obs/tap.py --agent claude-code|cursor   # event JSON on stdin

It runs the Noma hook.py unchanged as a child process, with the same stdin and
environment, relays the child's stdout/stderr byte-for-byte and exits with the
child's exit code. Only after the output has been relayed does it append one
redacted record to ~/.aidr-obs/capture.jsonl.

Two deliberate exceptions to byte-for-byte relaying:
- With NOMA_DRYRUN set, the hook's stdout is the payload it would have sent, not a
  decision. It is recorded but not relayed (unless relay_dry_run_stdout is set in
  the config), so the agent never mistakes a payload for a verdict.
- Cursor also runs the hooks in ~/.claude/settings.json, stamping its events with
  cursor_version. The Noma Claude hook skips those, so the claude-code tap passes
  them straight through without running the hook or recording anything; the
  cursor tap reports them.

Fail open: if the hook can't be found or times out, emit nothing and exit 0 (the
agent's default behavior). A capture failure never changes what was relayed. The
relay path is stdlib-only and imports nothing from this package; the capture step
imports redact/verdict lazily inside a try. No network calls.
"""

import json
import os
import subprocess
import sys
import time
import uuid

PLUGIN_ID = "guardrails@noma"
AGENTS = ("claude-code", "cursor")
CHILD_TIMEOUT_SECONDS = 23  # under the 25s (Claude Code) and 30s (Cursor) hook timeouts
FIELD_LIMIT = 4000
STDOUT_LIMIT = 16000
ROTATE_BYTES = 20 * 1024 * 1024
# Cursor events that carry no tool_name, named for the dashboard.
CURSOR_TOOL_NAMES = {
    "beforeShellExecution": "Shell",
    "beforeReadFile": "Read",
    "afterFileEdit": "Edit",
    "beforeSubmitPrompt": None,
    "afterAgentResponse": None,
}


def _home():
    return os.environ.get("HOME") or os.path.expanduser("~")


def _claude_dir():
    return os.environ.get("AIDR_OBS_CLAUDE_DIR") or os.path.join(_home(), ".claude")


def _data_dir():
    return os.environ.get("AIDR_OBS_HOME") or os.path.join(_home(), ".aidr-obs")


def load_config():
    """~/.aidr-obs/config.json, or {} (missing or unreadable)."""
    try:
        with open(os.path.join(_data_dir(), "config.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def agent_config(config, agent):
    section = (config.get("agents") or {}).get(agent)
    return section if isinstance(section, dict) else {}


def _version_key(v):
    parts = []
    for p in str(v).split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(-1)
    return parts


def _resolve_claude_hook():
    candidates = []
    try:
        with open(os.path.join(_claude_dir(), "plugins", "installed_plugins.json"), encoding="utf-8") as f:
            entries = (json.load(f).get("plugins") or {}).get(PLUGIN_ID) or []
        for e in entries:
            if isinstance(e, dict) and e.get("installPath"):
                candidates.append((e.get("version") or os.path.basename(e["installPath"]), e["installPath"]))
    except Exception:
        pass
    cache = os.path.join(_claude_dir(), "plugins", "cache", "noma", "guardrails")
    try:
        for name in os.listdir(cache):
            candidates.append((name, os.path.join(cache, name)))
    except Exception:
        pass
    for version, root in sorted(candidates, key=lambda c: _version_key(c[0]), reverse=True):
        hook = os.path.join(root, "scripts", "hook.py")
        if os.path.isfile(hook):
            return hook, str(version)
    return None, None


def _plugin_version(hook):
    """.version from the plugin manifest next to hook.py's scripts/ dir, or None."""
    root = os.path.dirname(os.path.dirname(hook))
    for manifest in (".cursor-plugin", ".claude-plugin", ".plugin"):
        try:
            with open(os.path.join(root, manifest, "plugin.json"), encoding="utf-8") as f:
                return str(json.load(f).get("version"))
        except Exception:
            continue
    return None


def resolve_hook(agent="claude-code", config=None):
    """(path to the Noma hook.py for this agent, plugin version), or (None, None)."""
    override = os.environ.get("AIDR_OBS_HOOK") or agent_config(config or {}, agent).get("hook")
    if override:
        return (override, _plugin_version(override)) if os.path.isfile(override) else (None, None)
    if agent == "claude-code":
        return _resolve_claude_hook()
    return None, None


def _write(stream, data):
    try:
        stream.write(data)
        stream.flush()
    except Exception:
        pass


def _append_record(record):
    data_dir = _data_dir()
    os.makedirs(data_dir, 0o700, exist_ok=True)
    path = os.path.join(data_dir, "capture.jsonl")
    try:
        if os.path.getsize(path) > ROTATE_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _parse_event(raw_stdin):
    try:
        obj = json.loads(raw_stdin.decode("utf-8", "replace"))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _tool_name(agent, event_obj):
    if not event_obj:
        return None
    if event_obj.get("tool_name"):
        return event_obj.get("tool_name")
    if agent == "cursor":
        return CURSOR_TOOL_NAMES.get(event_obj.get("hook_event_name"))
    return None


def _capture(agent, raw_stdin, event_obj, result, started, ended, hook, version, dry_run, relayed, tap_error):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from aidr_obs import redact, verdict

    event = event_obj.get("hook_event_name") if event_obj else None
    stdout_text = result["stdout"].decode("utf-8", "replace")
    decision = verdict.classify(stdout_text, event, dry_run)
    try:
        parsed_out = json.loads(stdout_text) if stdout_text.strip() else None
    except ValueError:
        parsed_out = None
    if parsed_out is not None:
        stored_out = json.dumps(redact.redact(parsed_out, FIELD_LIMIT), ensure_ascii=False)
    else:
        stored_out = redact.redact_str(stdout_text)
    millis = int((started % 1) * 1000)
    cwd = None
    if event_obj:
        roots = event_obj.get("workspace_roots")
        cwd = event_obj.get("cwd") or (roots[0] if isinstance(roots, list) and roots else None)

    _append_record(
        {
            "id": uuid.uuid4().hex,
            "v": 1,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)) + f".{millis:03d}",
            "ts_ms": int(started * 1000),
            "end_ms": int(ended * 1000),
            "agent": agent,
            "event": event,
            "tool_name": _tool_name(agent, event_obj),
            "session_id": (event_obj.get("session_id") or event_obj.get("conversation_id")) if event_obj else None,
            "cwd": cwd or os.getcwd(),
            "latency_ms": int((ended - started) * 1000),
            "exit_code": result["code"],
            "timed_out": result["timed_out"],
            "child_pid": result["pid"],
            "hook_path": hook,
            "hook_version": version,
            "dry_run": dry_run,
            "relayed": relayed,
            "verdict": decision["verdict"],
            "reason": redact.truncate_str(redact.redact_str(decision["reason"]), FIELD_LIMIT)
            if decision["reason"]
            else None,
            "stdin_bytes": len(raw_stdin),
            "stdin": redact.redact(event_obj, FIELD_LIMIT)
            if event_obj is not None
            else redact.truncate_str(redact.redact_str(raw_stdin.decode("utf-8", "replace")), FIELD_LIMIT),
            "stdout_bytes": len(result["stdout"]),
            "stdout": redact.truncate_str(stored_out, STDOUT_LIMIT),
            "stdout_truncated": len(stored_out) > STDOUT_LIMIT,
            "stderr_bytes": len(result["stderr"]),
            "tap_error": tap_error,
        }
    )


def _child_timeout():
    try:
        return min(float(os.environ.get("AIDR_OBS_CHILD_TIMEOUT") or CHILD_TIMEOUT_SECONDS), CHILD_TIMEOUT_SECONDS)
    except ValueError:
        return CHILD_TIMEOUT_SECONDS


def _run_child(hook, raw_stdin, env):
    proc = subprocess.Popen(
        [sys.executable, hook], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    try:
        out, err = proc.communicate(raw_stdin, timeout=_child_timeout())
        return {"pid": proc.pid, "code": proc.returncode, "stdout": out, "stderr": err, "timed_out": False}
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return {"pid": proc.pid, "code": None, "stdout": b"", "stderr": b"", "timed_out": True}


def _child_env(agent, hook, config):
    env = dict(os.environ)
    for name, value in (agent_config(config, agent).get("env") or {}).items():
        if isinstance(name, str) and isinstance(value, str):
            env.setdefault(name, value)
    root = os.path.dirname(os.path.dirname(hook))
    env.setdefault("CURSOR_PLUGIN_ROOT" if agent == "cursor" else "CLAUDE_PLUGIN_ROOT", root)
    # The hook may run straight from a git checkout: never leave __pycache__ in it.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _agent_from_argv(argv):
    if "--agent" in argv:
        i = argv.index("--agent")
        if i + 1 < len(argv) and argv[i + 1] in AGENTS:
            return argv[i + 1]
    return "claude-code"


def main(argv=None):
    agent = _agent_from_argv(sys.argv[1:] if argv is None else argv)
    try:
        raw_stdin = sys.stdin.buffer.read()
    except Exception:
        raw_stdin = b""
    event_obj = _parse_event(raw_stdin)
    if agent == "claude-code" and event_obj and event_obj.get("cursor_version"):
        return 0

    started = time.time()
    result = {"pid": None, "code": 0, "stdout": b"", "stderr": b"", "timed_out": False}
    tap_error = None
    hook, version = None, None
    config = {}
    dry_run = bool(os.environ.get("NOMA_DRYRUN"))
    try:
        config = load_config()
        hook, version = resolve_hook(agent, config)
        if hook is None:
            tap_error = "Noma " + agent + " hook.py not found"
        else:
            env = _child_env(agent, hook, config)
            dry_run = bool(env.get("NOMA_DRYRUN"))
            result = _run_child(hook, raw_stdin, env)
            if result["timed_out"]:
                tap_error = "hook timed out after " + str(_child_timeout()) + "s"
    except Exception as e:
        tap_error = "run hook: " + type(e).__name__ + ": " + str(e)
    ended = time.time()

    relayed = not dry_run or bool(config.get("relay_dry_run_stdout"))
    if relayed:
        _write(sys.stdout.buffer, result["stdout"])
    _write(sys.stderr.buffer, result["stderr"])

    if not os.environ.get("AIDR_OBS_NO_CAPTURE"):
        try:
            _capture(agent, raw_stdin, event_obj, result, started, ended, hook, version, dry_run, relayed, tap_error)
        except Exception:
            pass
    code = result["code"]
    return code if isinstance(code, int) and code >= 0 else 0


if __name__ == "__main__":
    sys.exit(main())
