from aidr_obs import logparse

from .conftest import FIXTURES


def _runs():
    lines = logparse.parse_lines((FIXTURES / "claude-code-guardrails-debug.log").read_text())
    return logparse.group_runs("claude-code", lines)


def test_agent_for_log():
    assert logparse.agent_for_log("claude-code-guardrails-debug.log") == "claude-code"
    assert logparse.agent_for_log("cursor-guardrails-debug.log.2") == "cursor"
    assert logparse.agent_for_log("guardrails-debug.log") == "unknown"
    assert logparse.agent_for_log("notes.txt") is None


def test_discover_orders_rotated_files_oldest_first(tmp_path):
    for name in (
        "claude-code-guardrails-debug.log",
        "claude-code-guardrails-debug.log.1",
        "claude-code-guardrails-debug.log.2",
        "kiro-guardrails-debug.log",
        "other.log",
    ):
        (tmp_path / name).write_text("")
    names = [p.name for p, _ in logparse.discover_log_files(tmp_path)]
    assert names == [
        "claude-code-guardrails-debug.log.2",
        "claude-code-guardrails-debug.log.1",
        "claude-code-guardrails-debug.log",
        "kiro-guardrails-debug.log",
    ]


def test_continuation_lines_join_previous_message():
    lines = logparse.parse_lines((FIXTURES / "claude-code-guardrails-debug.log").read_text())
    curl = [ln for ln in lines if ln.message.startswith("curl stderr")][0]
    assert curl.message.endswith("\nsecond line of curl stderr")
    assert all(ln.pid > 0 for ln in lines)


def test_runs_are_split_by_pid_and_gap():
    runs = _runs()
    assert [(r.pid, r.event, r.outcome) for r in runs] == [
        (1001, "UserPromptSubmit", "ok"),
        (1002, "PreToolUse", "dry_run"),
        (1003, "PostToolUse", "failed"),
        (1004, None, "no_key"),
        (1002, "Stop", "dry_run"),
    ]


def test_run_fields():
    ok, dry, failed, no_key, _ = _runs()
    assert ok.http_status == 200 and ok.sender == "curl" and ok.post_bytes == 812
    assert ok.mcp_artifacts == 1 and ok.project_root == "/home/alice/src/demo-project"
    assert ok.discovery[0] == {
        "scope": "user",
        "kind": "claude_json",
        "path": "/home/alice/.claude.json",
        "has_servers": True,
    }
    assert ok.installed_config == "none" and ok.key_source == "env" and ok.error_count == 1
    assert ok.duration_ms == 306 and ok.hook_file == "hook.py"
    assert dry.post_bytes == 714 and dry.stdin_bytes == 619
    assert failed.retries == 1 and failed.error_count == 2 and failed.key_source == "credential_store"
    assert no_key.key_source == "none"


def test_error_signature_folds_paths_and_numbers():
    a = logparse.error_signature(
        "read_json open/read /home/a/.claude/x.json: FileNotFoundError: [Errno 2] No such "
        "file or directory: '/home/a/.claude/x.json'"
    )
    b = logparse.error_signature(
        "read_json open/read /Users/b/.claude/y.json: FileNotFoundError: [Errno 2] No such "
        "file or directory: '/Users/b/.claude/y.json'"
    )
    assert a == b
    assert a.startswith("read_json open/read <path>")
