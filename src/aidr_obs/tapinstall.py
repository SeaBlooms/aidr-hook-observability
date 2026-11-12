"""Wire the capture tap into Claude Code and Cursor, and back out again.

Claude Code: the tap's entries go into ~/.claude/settings.json and the Noma plugin is
disabled (its files are never written). Cursor: the tap's entries go into the
user-level ~/.cursor/hooks.json and run the Noma Cursor hook from the installed plugin or
a checkout given with --cursor-hook (only ever read).

Every entry is a single shell command string with no "args": Cursor also runs the
hooks in ~/.claude/settings.json and ignores "args", so a split command would run a
bare `uv` there. Before anything is written, each exact command is run once through
/bin/sh with a sample event and must exit 0 with no stdout, so a broken entry never
reaches an agent.

Existing hooks are left alone: the tap's entries are recognised by the tap path and
only those are added or removed.
"""

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import paths, tap

CLAUDE_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "PermissionDenied")
CURSOR_EVENTS = (
    "preToolUse",
    "beforeShellExecution",
    "beforeMCPExecution",
    "beforeReadFile",
    "beforeSubmitPrompt",
    "postToolUse",
    "afterFileEdit",
    "afterAgentResponse",
)
CLAUDE_TIMEOUT = 25
CURSOR_TIMEOUT = 30
TAP_PATH = Path(tap.__file__).resolve()
TAP_MARKER = "aidr_obs/tap.py"
# Copied from Claude Code's settings env into the Cursor tap config so both agents behave alike.
SHARED_ENV = ("NOMA_DEBUG", "NOMA_DRYRUN", "NOMA_API_URL")
SELFTEST_EVENTS = {
    "claude-code": {
        "hook_event_name": "PreToolUse",
        "session_id": "aidr-obs-selftest",
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
    },
    "cursor": {
        "hook_event_name": "beforeShellExecution",
        "conversation_id": "aidr-obs-selftest",
        "command": "true",
        "cwd": "/",
    },
}


class SelfTestFailed(RuntimeError):
    pass


def settings_path() -> Path:
    return paths.claude_dir() / "settings.json"


def cursor_hooks_path() -> Path:
    return paths.cursor_dir() / "hooks.json"


def state_path() -> Path:
    return paths.data_dir() / "tap-state.json"


def config_path() -> Path:
    return paths.data_dir() / "config.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".aidr-obs.tmp")
    mode = path.stat().st_mode & 0o777 if path.exists() else mode
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def tap_command(agent: str) -> str:
    uv = shutil.which("uv") or "uv"
    return " ".join(shlex.quote(p) for p in (uv, "run", "--no-project", str(TAP_PATH), "--agent", agent))


def _is_tap_hook(hook) -> bool:
    if not isinstance(hook, dict):
        return False
    text = json.dumps([hook.get("command"), hook.get("args")])
    return TAP_MARKER in text.replace("\\\\", "/")


# --- Claude Code settings.json: {"hooks": {Event: [{"hooks": [entry, ...]}]}} ---------------------


def claude_entry() -> dict:
    return {"hooks": [{"type": "command", "command": tap_command("claude-code"), "timeout": CLAUDE_TIMEOUT}]}


def _strip_claude(settings: dict) -> int:
    removed = 0
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(inner, list):
                kept_groups.append(group)
                continue
            kept = [h for h in inner if not _is_tap_hook(h)]
            removed += len(inner) - len(kept)
            if kept:
                kept_groups.append(dict(group, hooks=kept))
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


def claude_events(settings: dict) -> list[str]:
    out = []
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            if any(_is_tap_hook(h) for h in (group or {}).get("hooks") or []):
                out.append(event)
                break
    return out


def plugin_enabled(settings: dict) -> bool:
    """Treat a missing flag as enabled so install never leaves the plugin and the tap both running."""
    return (settings.get("enabledPlugins") or {}).get(tap.PLUGIN_ID) is not False


def _set_plugin(enabled: bool) -> str:
    """Toggle the plugin with the claude CLI; fall back to editing enabledPlugins directly."""
    claude = os.environ.get("AIDR_OBS_CLAUDE_BIN") or shutil.which("claude")
    action = "enable" if enabled else "disable"
    if claude:
        try:
            done = subprocess.run(
                [claude, "plugin", action, "--scope", "user", tap.PLUGIN_ID], capture_output=True, text=True, timeout=60
            )
            if done.returncode == 0 and plugin_enabled(_load(settings_path())) == enabled:
                return "claude plugin " + action
        except (OSError, subprocess.TimeoutExpired):
            pass
    settings = _load(settings_path())
    settings.setdefault("enabledPlugins", {})[tap.PLUGIN_ID] = enabled
    _save(settings_path(), settings)
    return "edited enabledPlugins"


# --- Cursor hooks.json: {"version": 1, "hooks": {event: [entry, ...]}} -----------------------------


def cursor_entry() -> dict:
    return {"command": tap_command("cursor"), "timeout": CURSOR_TIMEOUT}


def _strip_cursor(doc: dict) -> int:
    removed = 0
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = [h for h in entries if not _is_tap_hook(h)]
        removed += len(entries) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return removed


def cursor_events(doc: dict) -> list[str]:
    return [e for e, entries in (doc.get("hooks") or {}).items() if any(_is_tap_hook(h) for h in entries or [])]


def _is_noma_cursor_plugin(root: Path) -> bool:
    try:
        manifest = _load(root / ".cursor-plugin" / "plugin.json")
    except (OSError, ValueError):
        return False
    author = manifest.get("author")
    author = author.get("name") if isinstance(author, dict) else author
    return author == "Noma Security" or manifest.get("displayName") == "Noma Guardrails"


def find_cursor_hook(explicit: Path | None = None) -> Path:
    """--cursor-hook, else the hook a previous install used, else an installed Noma Cursor plugin."""
    if explicit:
        return explicit
    configured = tap.agent_config(tap.load_config(), "cursor").get("hook")
    if configured and Path(configured).is_file():
        return Path(configured)
    found = [
        h
        for h in sorted((paths.cursor_dir() / "plugins").glob("**/scripts/hook.py"))
        if _is_noma_cursor_plugin(h.parent.parent)
    ]
    if found:
        return max(found, key=lambda h: tap._version_key(tap._plugin_version(h)))
    raise SelfTestFailed(
        "no Noma Cursor hook found: install the Noma Cursor plugin, or pass --cursor-hook"
        " <path to cursor-guardrails/scripts/hook.py> (for example in a noma-marketplace checkout)"
    )


def _cursor_plugin_events(hook: Path) -> tuple[str, ...]:
    """The events the plugin's own hooks.json registers, so the tap follows upstream; else the known list."""
    try:
        events = tuple(_load(hook.parent.parent / "hooks" / "hooks.json").get("hooks") or {})
        return events or CURSOR_EVENTS
    except (OSError, ValueError):
        return CURSOR_EVENTS


def _cursor_env(explicit: dict | None) -> dict:
    if explicit is not None:
        return explicit
    claude_env = _load(settings_path()).get("env") or {}
    env = {k: str(claude_env[k]) for k in SHARED_ENV if claude_env.get(k)}
    # Only in dry-run: then the key never leaves the machine, so copying a placeholder is harmless.
    if env.get("NOMA_DRYRUN") and claude_env.get("NOMA_API_KEY"):
        env["NOMA_API_KEY"] = str(claude_env["NOMA_API_KEY"])
    return env


# --- self-test, backups, state ---------------------------------------------------------------------


def self_test(agent: str, command: str, extra_config: dict | None = None) -> dict:
    """Run the exact hook command through /bin/sh as the agent would; it must exit 0 with no stdout.

    Uses a throwaway data dir so nothing is captured, and a dry-run child so nothing is sent.
    """
    tmp_home = paths.ensure_private_dir(paths.data_dir() / "selftest")
    if extra_config is not None:
        _save(tmp_home / "config.json", extra_config)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NOMA_", "AIDR_OBS_"))}
    env.update({"AIDR_OBS_HOME": str(tmp_home), "NOMA_DRYRUN": "1", "NOMA_API_KEY": "aidr-obs-selftest"})
    started = time.time()
    try:
        done = subprocess.run(
            ["/bin/sh", "-c", command],
            input=json.dumps(SELFTEST_EVENTS[agent]).encode(),
            capture_output=True,
            env=env,
            timeout=CLAUDE_TIMEOUT if agent == "claude-code" else CURSOR_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise SelfTestFailed(agent + " hook command timed out: " + command) from e
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)
    if done.returncode != 0 or done.stdout.strip():
        raise SelfTestFailed(
            agent
            + " hook command failed self-test (exit "
            + str(done.returncode)
            + "): "
            + command
            + "\nstdout: "
            + done.stdout.decode(errors="replace")[:500]
            + "\nstderr: "
            + done.stderr.decode(errors="replace")[:500]
        )
    return {"exit": 0, "seconds": round(time.time() - started, 2)}


def _backup(path: Path, name: str) -> Path | None:
    if not path.exists():
        return None
    backup_dir = paths.ensure_private_dir(paths.data_dir() / "backups")
    dest = backup_dir / (name + "." + time.strftime("%Y%m%d-%H%M%S") + f".{time.time_ns() % 10**9:09d}")
    shutil.copy2(path, dest)
    os.chmod(dest, 0o600)
    return dest


def _state() -> dict:
    data = _load(state_path()) if state_path().exists() else {}
    return data if all(isinstance(v, dict) for v in data.values()) else {}


def _save_state(state: dict) -> None:
    if any(state.values()):
        paths.ensure_private_dir(paths.data_dir())
        _save(state_path(), state)
    elif state_path().exists():
        state_path().unlink()


def _restore_if_unchanged(path: Path, backup: str | None) -> bool:
    """Nothing else changed since install: put the original bytes back so its formatting survives too."""
    if backup and Path(backup).exists() and path.exists() and _load(path) == _load(Path(backup)):
        shutil.copyfile(backup, path)
        return True
    return False


# --- install / uninstall / status ------------------------------------------------------------------


def _install_claude(state: dict) -> dict:
    command = tap_command("claude-code")
    test = self_test("claude-code", command)
    sp = settings_path()
    previous = state.get("claude-code") or {}
    backup = previous.get("backup")
    if not (backup and Path(backup).exists()):
        made = _backup(sp, "settings.json")
        backup = str(made) if made else None
    settings = _load(sp)
    was_enabled = previous.get("plugin_was_enabled", plugin_enabled(settings))
    how = _set_plugin(False) if plugin_enabled(settings) else "already disabled"

    settings = _load(sp)
    _strip_claude(settings)
    hooks = settings.setdefault("hooks", {})
    for event in CLAUDE_EVENTS:
        hooks.setdefault(event, []).append(claude_entry())
    _save(sp, settings)
    state["claude-code"] = {
        "installed_ms": int(time.time() * 1000),
        "plugin_was_enabled": was_enabled,
        "backup": backup,
        "file": str(sp),
    }
    return {"file": str(sp), "backup": backup, "plugin": how, "events": list(CLAUDE_EVENTS), "self_test": test}


def _install_cursor(state: dict, hook: Path, env: dict | None) -> dict:
    hook = hook.expanduser().resolve()
    if not hook.is_file():
        raise SelfTestFailed("Noma Cursor hook not found: " + str(hook))
    config = tap.load_config()
    config.setdefault("agents", {})["cursor"] = {"hook": str(hook), "env": _cursor_env(env)}
    command = tap_command("cursor")
    test = self_test("cursor", command, {"agents": {"cursor": {"hook": str(hook), "env": {}}}})

    hp = cursor_hooks_path()
    previous = state.get("cursor") or {}
    created = previous.get("created", not hp.exists())
    backup = previous.get("backup")
    if not (backup and Path(backup).exists()):
        made = _backup(hp, "cursor-hooks.json")
        backup = str(made) if made else None
    paths.ensure_private_dir(paths.data_dir())
    _save(config_path(), config)

    doc = _load(hp) or {"version": 1}
    doc.setdefault("version", 1)
    _strip_cursor(doc)
    hooks = doc.setdefault("hooks", {})
    events = _cursor_plugin_events(hook)
    for event in events:
        hooks.setdefault(event, []).append(cursor_entry())
    _save(hp, doc, mode=0o644)
    state["cursor"] = {"installed_ms": int(time.time() * 1000), "created": created, "backup": backup, "file": str(hp)}
    return {
        "file": str(hp),
        "backup": backup,
        "hook": str(hook),
        "env": sorted(config["agents"]["cursor"]["env"]),
        "events": list(events),
        "self_test": test,
    }


def install(agent: str = "all", cursor_hook: Path | None = None, cursor_env: dict | None = None) -> dict:
    """Install for one agent or both. Self-tests run first; a failure writes nothing for that agent."""
    state = _state()
    out = {}
    try:
        if agent in ("all", "cursor"):
            out["cursor"] = _install_cursor(state, find_cursor_hook(cursor_hook), cursor_env)
        if agent in ("all", "claude-code"):
            out["claude-code"] = _install_claude(state)
    finally:
        _save_state(state)
    return out


def _uninstall_claude(state: dict) -> dict:
    sp = settings_path()
    settings = _load(sp)
    removed = _strip_claude(settings)
    if removed:
        _save(sp, settings)
    info = state.pop("claude-code", {})
    plugin = "left as is"
    if info.get("plugin_was_enabled", True) and not plugin_enabled(_load(sp)):
        plugin = _set_plugin(True)
    restored = _restore_if_unchanged(sp, info.get("backup"))
    return {"file": str(sp), "hooks_removed": removed, "plugin": plugin, "restored_original_bytes": restored}


def _uninstall_cursor(state: dict) -> dict:
    hp = cursor_hooks_path()
    info = state.pop("cursor", {})
    doc = _load(hp)
    removed = _strip_cursor(doc)
    deleted = restored = False
    if removed:
        if info.get("created") and not doc.get("hooks") and set(doc) <= {"version", "hooks"}:
            hp.unlink()
            deleted = True
        else:
            _save(hp, doc)
            restored = _restore_if_unchanged(hp, info.get("backup"))
    config = tap.load_config()
    if (config.get("agents") or {}).pop("cursor", None) is not None:
        _save(config_path(), config)
    return {"file": str(hp), "hooks_removed": removed, "deleted_file": deleted, "restored_original_bytes": restored}


def uninstall(agent: str = "all") -> dict:
    state = _state()
    out = {}
    try:
        if agent in ("all", "cursor"):
            out["cursor"] = _uninstall_cursor(state)
        if agent in ("all", "claude-code"):
            out["claude-code"] = _uninstall_claude(state)
    finally:
        _save_state(state)
    return out


def status() -> dict:
    settings = _load(settings_path())
    cursor_doc = _load(cursor_hooks_path())
    config = tap.load_config()
    claude_hook, claude_version = tap.resolve_hook("claude-code", config)
    cursor_hook, cursor_version = tap.resolve_hook("cursor", config)
    capture = paths.capture_path()
    return {
        "claude-code": {
            "tap_events": claude_events(settings),
            "plugin_enabled": plugin_enabled(settings),
            "hook": claude_hook,
            "hook_version": claude_version,
            "file": str(settings_path()),
        },
        "cursor": {
            "tap_events": cursor_events(cursor_doc),
            "hook": cursor_hook,
            "hook_version": cursor_version,
            "env": sorted(tap.agent_config(config, "cursor").get("env") or {}),
            "file": str(cursor_hooks_path()),
        },
        "capture_file": str(capture),
        "capture_bytes": capture.stat().st_size if capture.exists() else 0,
        "state": _state() or None,
    }
