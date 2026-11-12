"""Classify what a Noma hook wrote to stdout into a verdict.

The Noma hook prints the backend's decision verbatim (an empty stdout means the
agent's default behavior, i.e. allow). With NOMA_DRYRUN set, stdout is the payload
that would have been sent, not a decision.

Understands both decision shapes:
- Claude Code: hookSpecificOutput.permissionDecision, decision: block, continue: false,
  updatedInput / updated*ToolOutput, additionalContext, systemMessage.
- Cursor: permission: allow|deny|ask, continue: false, user_message / agent_message,
  and any updated_* replacement field.

Verdicts: allow, block, ask, mask, context, dry_run, other.

Imported by the tap, which may run on an older python3, so keep it 3.8-compatible.
"""

from __future__ import annotations

import json

VERDICTS = ("allow", "block", "ask", "mask", "context", "dry_run", "other")
_CONTEXT_EVENTS = ("UserPromptSubmit", "SessionStart")


def _str(v):
    return v if isinstance(v, str) and v.strip() else None


def classify(stdout: str, event: str | None = None, dry_run: bool = False) -> dict:
    """Return {"verdict": ..., "reason": str | None, "decision": parsed JSON or None}."""
    text = (stdout or "").strip()
    if dry_run:
        return {"verdict": "dry_run" if text else "allow", "reason": None, "decision": None}
    if not text:
        return {"verdict": "allow", "reason": None, "decision": None}
    try:
        obj = json.loads(text)
    except ValueError:
        verdict = "context" if event in _CONTEXT_EVENTS else "other"
        return {"verdict": verdict, "reason": text[:500], "decision": None}
    if not isinstance(obj, dict):
        return {"verdict": "other", "reason": None, "decision": obj}

    hso = obj.get("hookSpecificOutput") if isinstance(obj.get("hookSpecificOutput"), dict) else {}
    reason = (
        _str(hso.get("permissionDecisionReason"))
        or _str(obj.get("reason"))
        or _str(obj.get("stopReason"))
        or _str(obj.get("user_message"))
        or _str(obj.get("agent_message"))
        or _str(obj.get("systemMessage"))
        or _str(hso.get("additionalContext"))
    )

    permission = hso.get("permissionDecision") or obj.get("permission")
    request = hso.get("decision") if isinstance(hso.get("decision"), dict) else {}
    behavior = request.get("behavior")
    masked = (
        any(k in hso for k in ("updatedInput", "updatedToolOutput", "updatedMCPToolOutput"))
        or "updatedInput" in request
        or any(isinstance(k, str) and k.startswith("updated_") for k in obj)
    )

    if permission == "deny" or behavior == "deny" or obj.get("decision") == "block" or obj.get("continue") is False:
        verdict = "block"
    elif permission == "ask":
        verdict = "ask"
    elif masked:
        verdict = "mask"
    elif _str(hso.get("additionalContext")) or _str(obj.get("systemMessage")) or _str(obj.get("agent_message")):
        verdict = "context"
    elif (
        permission == "allow"
        or behavior == "allow"
        or obj.get("decision") == "approve"
        or obj.get("continue") is True
        or not obj
    ):
        verdict = "allow"
    else:
        verdict = "other"
    return {"verdict": verdict, "reason": reason, "decision": obj}
