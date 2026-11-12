import json
import subprocess

import pytest

from aidr_obs import tapinstall

ORIGINAL = {
    "hooks": {
        "PreToolUse": [{"hooks": [{"type": "command", "command": "/opt/other/notify"}]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": "/opt/other/notify"}]}],
    },
    "env": {"NOMA_DEBUG": "1", "NOMA_DRYRUN": "1", "NOMA_API_KEY": "placeholder"},
    "enabledPlugins": {"guardrails@noma": True, "something@else": True},
}
CURSOR_DENY = '{"permission":"deny","user_message":"nope"}'


@pytest.fixture
def settings(env, monkeypatch, tmp_path):
    path = env["claude"] / "settings.json"
    path.write_text(json.dumps(ORIGINAL, indent=2))
    # A claude CLI that always fails, so the enabledPlugins fallback is what's exercised.
    fake = tmp_path / "fake-claude-cli"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("AIDR_OBS_CLAUDE_BIN", str(fake))
    return path


@pytest.fixture
def cursor_hook(tmp_path):
    """A stand-in Noma Cursor plugin checkout: hook.py plus its hooks/hooks.json event list."""
    root = tmp_path / "cursor-guardrails"
    (root / "scripts").mkdir(parents=True)
    (root / "hooks").mkdir()
    hook = root / "scripts" / "hook.py"
    hook.write_text(
        f"import sys, os\nsys.stdin.read()\nif not os.environ.get('NOMA_DRYRUN'):\n"
        f"    sys.stdout.write({CURSOR_DENY!r})\n"
    )
    (root / "hooks" / "hooks.json").write_text(
        json.dumps(
            {"version": 1, "hooks": {"beforeShellExecution": [{"command": "x"}], "beforeReadFile": [{"command": "x"}]}}
        )
    )
    return hook


def _load(path):
    return json.loads(path.read_text())


def test_entries_are_single_shell_commands():
    for entry in (tapinstall.claude_entry()["hooks"][0], tapinstall.cursor_entry()):
        assert "args" not in entry
        assert entry["command"].split()[1:3] == ["run", "--no-project"]
        assert "aidr_obs/tap.py --agent" in entry["command"]


def test_install_claude_keeps_existing_hooks(settings, env):
    out = tapinstall.install("claude-code")["claude-code"]
    data = _load(settings)
    assert data["enabledPlugins"] == {"guardrails@noma": False, "something@else": True}
    assert data["env"] == ORIGINAL["env"]
    assert data["hooks"]["PreToolUse"][0] == ORIGINAL["hooks"]["PreToolUse"][0]
    assert data["hooks"]["SessionEnd"] == ORIGINAL["hooks"]["SessionEnd"]
    assert sorted(tapinstall.claude_events(data)) == sorted(tapinstall.CLAUDE_EVENTS)
    entry = data["hooks"]["Stop"][0]["hooks"][0]
    assert entry["command"].endswith("--agent claude-code") and entry["timeout"] == 25
    assert json.loads(open(out["backup"]).read()) == ORIGINAL
    assert out["self_test"]["exit"] == 0
    assert not (env["cursor"] / "hooks.json").exists()


def test_install_twice_does_not_duplicate(settings):
    tapinstall.install("claude-code")
    tapinstall.install("claude-code")
    data = _load(settings)
    assert len(data["hooks"]["Stop"]) == 1 and len(data["hooks"]["PreToolUse"]) == 2


def test_uninstall_claude_restores_original_bytes(settings):
    original_bytes = settings.read_bytes()
    tapinstall.install("claude-code")
    tapinstall.install("claude-code")
    out = tapinstall.uninstall("claude-code")["claude-code"]
    assert out["hooks_removed"] == len(tapinstall.CLAUDE_EVENTS)
    assert out["restored_original_bytes"] is True and settings.read_bytes() == original_bytes
    assert tapinstall.status()["state"] is None


def test_uninstall_keeps_changes_made_after_install(settings):
    tapinstall.install("claude-code")
    data = _load(settings)
    data["theme"] = "light"
    settings.write_text(json.dumps(data))
    out = tapinstall.uninstall("claude-code")["claude-code"]
    assert out["restored_original_bytes"] is False
    assert _load(settings) == {**ORIGINAL, "theme": "light"}


def test_uninstall_keeps_plugin_disabled_if_it_was(settings):
    data = _load(settings)
    data["enabledPlugins"]["guardrails@noma"] = False
    settings.write_text(json.dumps(data))
    tapinstall.install("claude-code")
    tapinstall.uninstall("claude-code")
    assert _load(settings)["enabledPlugins"]["guardrails@noma"] is False


def test_failed_self_test_writes_nothing(settings, env, monkeypatch, cursor_hook):
    original = settings.read_bytes()
    monkeypatch.setattr(tapinstall, "tap_command", lambda agent: "uv")  # the bare-uv mistake
    with pytest.raises(tapinstall.SelfTestFailed):
        tapinstall.install("all", cursor_hook=cursor_hook)
    assert settings.read_bytes() == original
    assert not (env["cursor"] / "hooks.json").exists()
    assert not (env["home"] / "config.json").exists()


def test_install_cursor_creates_user_hooks(settings, env, cursor_hook):
    out = tapinstall.install("cursor", cursor_hook=cursor_hook)["cursor"]
    doc = _load(env["cursor"] / "hooks.json")
    assert doc["version"] == 1
    assert sorted(doc["hooks"]) == ["beforeReadFile", "beforeShellExecution"]
    assert doc["hooks"]["beforeShellExecution"][0]["command"].endswith("--agent cursor")
    config = _load(env["home"] / "config.json")
    assert config["agents"]["cursor"]["hook"] == str(cursor_hook.resolve())
    # Dry-run in Claude's env, so the placeholder key is copied too.
    assert config["agents"]["cursor"]["env"] == {"NOMA_DEBUG": "1", "NOMA_DRYRUN": "1", "NOMA_API_KEY": "placeholder"}
    assert out["self_test"]["exit"] == 0
    assert _load(settings) == ORIGINAL


def test_cursor_key_not_copied_outside_dry_run(settings, env, cursor_hook):
    data = _load(settings)
    del data["env"]["NOMA_DRYRUN"]
    settings.write_text(json.dumps(data))
    tapinstall.install("cursor", cursor_hook=cursor_hook)
    assert _load(env["home"] / "config.json")["agents"]["cursor"]["env"] == {"NOMA_DEBUG": "1"}


def test_installed_cursor_command_relays_verdict_like_cursor_would(settings, env, cursor_hook):
    tapinstall.install("cursor", cursor_hook=cursor_hook, cursor_env={})
    command = _load(env["cursor"] / "hooks.json")["hooks"]["beforeShellExecution"][0]["command"]
    event = {"hook_event_name": "beforeShellExecution", "conversation_id": "c", "command": "ls"}
    done = subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps(event).encode(),
        capture_output=True,
        env={
            "HOME": str(env["home"].parent),
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
            "AIDR_OBS_HOME": str(env["home"]),
        },
        timeout=30,
    )
    assert done.returncode == 0 and done.stdout.decode() == CURSOR_DENY
    rec = json.loads((env["home"] / "capture.jsonl").read_text().splitlines()[0])
    assert rec["agent"] == "cursor" and rec["verdict"] == "block"


def test_uninstall_cursor_deletes_file_it_created(settings, env, cursor_hook):
    tapinstall.install("cursor", cursor_hook=cursor_hook)
    out = tapinstall.uninstall("cursor")["cursor"]
    assert out["deleted_file"] is True and not (env["cursor"] / "hooks.json").exists()
    assert "cursor" not in _load(env["home"] / "config.json").get("agents", {})


def test_uninstall_cursor_keeps_existing_user_hooks(settings, env, cursor_hook):
    path = env["cursor"] / "hooks.json"
    path.write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "/opt/mine"}]}}))
    original = path.read_bytes()
    tapinstall.install("cursor", cursor_hook=cursor_hook)
    assert _load(path)["hooks"]["stop"] == [{"command": "/opt/mine"}]
    out = tapinstall.uninstall("cursor")["cursor"]
    assert out["restored_original_bytes"] is True and path.read_bytes() == original


def test_status_reports_both_agents(settings, cursor_hook):
    s = tapinstall.status()
    assert s["claude-code"]["tap_events"] == [] and s["cursor"]["tap_events"] == []
    tapinstall.install("all", cursor_hook=cursor_hook)
    s = tapinstall.status()
    assert sorted(s["claude-code"]["tap_events"]) == sorted(tapinstall.CLAUDE_EVENTS)
    assert s["claude-code"]["plugin_enabled"] is False
    assert sorted(s["cursor"]["tap_events"]) == ["beforeReadFile", "beforeShellExecution"]
    assert s["cursor"]["hook"] == str(cursor_hook.resolve())


def _install_fake_plugin(env, version, author="Noma Security"):
    root = env["cursor"] / "plugins" / "cache" / "noma" / "guardrails" / version
    (root / "scripts").mkdir(parents=True)
    (root / ".cursor-plugin").mkdir()
    (root / "scripts" / "hook.py").write_text("import sys\nsys.stdin.read()\n")
    (root / ".cursor-plugin" / "plugin.json").write_text(json.dumps({"version": version, "author": {"name": author}}))
    return root / "scripts" / "hook.py"


def test_find_cursor_hook_prefers_explicit_then_installed_plugin(env, cursor_hook):
    assert tapinstall.find_cursor_hook(cursor_hook) == cursor_hook
    _install_fake_plugin(env, "1.2.0")
    newest = _install_fake_plugin(env, "1.10.0")
    _install_fake_plugin(env, "9.0.0", author="Someone Else")
    assert tapinstall.find_cursor_hook() == newest


def test_find_cursor_hook_reuses_previous_install(settings, env, cursor_hook):
    tapinstall.install("cursor", cursor_hook=cursor_hook)
    assert tapinstall.find_cursor_hook() == cursor_hook.resolve()


def test_find_cursor_hook_without_any_hook_explains(env):
    with pytest.raises(tapinstall.SelfTestFailed, match="--cursor-hook"):
        tapinstall.find_cursor_hook()
