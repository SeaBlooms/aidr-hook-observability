import json

import pytest

from aidr_obs.verdict import classify


def _j(obj):
    return json.dumps(obj)


@pytest.mark.parametrize(
    ("stdout", "event", "expected"),
    [
        ("", "PreToolUse", "allow"),
        ("\n", "Stop", "allow"),
        ("{}", "PreToolUse", "allow"),
        (
            _j(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "secret file",
                    }
                }
            ),
            "PreToolUse",
            "block",
        ),
        (
            _j({"hookSpecificOutput": {"permissionDecision": "ask", "permissionDecisionReason": "confirm"}}),
            "PreToolUse",
            "ask",
        ),
        (
            _j({"hookSpecificOutput": {"permissionDecision": "allow", "updatedInput": {"command": "ls"}}}),
            "PreToolUse",
            "mask",
        ),
        (_j({"hookSpecificOutput": {"permissionDecision": "allow"}}), "PreToolUse", "allow"),
        (_j({"decision": "block", "reason": "PII in prompt"}), "UserPromptSubmit", "block"),
        (_j({"decision": "block", "reason": "continue: leaked key"}), "Stop", "block"),
        (_j({"continue": False, "stopReason": "policy"}), "PostToolUse", "block"),
        (_j({"hookSpecificOutput": {"updatedMCPToolOutput": "[masked]"}}), "PostToolUse", "mask"),
        (_j({"hookSpecificOutput": {"additionalContext": "be careful"}}), "UserPromptSubmit", "context"),
        (_j({"hookSpecificOutput": {"decision": {"behavior": "deny"}}}), "PermissionRequest", "block"),
        ("plain text", "UserPromptSubmit", "context"),
        ("plain text", "PreToolUse", "other"),
        (_j([1, 2]), "PreToolUse", "other"),
        (_j({"something": "new"}), "PreToolUse", "other"),
        (_j({"permission": "deny", "user_message": "no"}), "beforeShellExecution", "block"),
        (_j({"permission": "ask"}), "beforeMCPExecution", "ask"),
        (_j({"permission": "allow"}), "beforeReadFile", "allow"),
        (_j({"continue": False, "user_message": "PII"}), "beforeSubmitPrompt", "block"),
        (_j({"continue": True}), "beforeSubmitPrompt", "allow"),
        (_j({"updated_mcp_tool_output": "[Blocked by Noma Security]"}), "postToolUse", "mask"),
        (_j({"permission": "allow", "agent_message": "careful"}), "preToolUse", "context"),
    ],
)
def test_classify(stdout, event, expected):
    assert classify(stdout, event)["verdict"] == expected


def test_reason_is_extracted():
    out = _j({"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "ssh key read"}})
    assert classify(out, "PreToolUse")["reason"] == "ssh key read"


def test_dry_run_stdout_is_a_payload_not_a_decision():
    payload = _j({"hook_event_name": "PreToolUse", "decision": "block"})
    assert classify(payload, "PreToolUse", dry_run=True)["verdict"] == "dry_run"
    assert classify("", "PreToolUse", dry_run=True)["verdict"] == "allow"
