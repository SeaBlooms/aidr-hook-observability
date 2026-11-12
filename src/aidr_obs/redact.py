"""Mask secret-looking values in captured hook data before it is stored.

Standalone: no code is shared with the Noma plugin. Over-redaction is acceptable,
leaking is not. Imported by the tap, so keep it stdlib-only and 3.8-compatible.
"""

from __future__ import annotations

import re

REDACTED = "***REDACTED***"

_SENSITIVE_NAME = (
    r"[A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|credential|auth|session[_-]?key|x-noma-key)[A-Za-z0-9_.-]*"
)
_SENSITIVE_KEY_RE = re.compile(r"^" + _SENSITIVE_NAME + r"$", re.IGNORECASE)

_PATTERNS = [
    # PEM private key blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL), REDACTED),
    # before name=value, which would otherwise eat "Bearer" as the value and leave the token
    (re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), lambda m: m.group(1) + " " + REDACTED),
    # name=value / name: value where the name looks sensitive
    (
        re.compile(r"(" + _SENSITIVE_NAME + r")([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)", re.IGNORECASE),
        lambda m: m.group(1) + m.group(2) + REDACTED,
    ),
    (re.compile(r"\b(?:github_pat_|gh[pousr]_|glpat-)[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), REDACTED),
    # user:password@ in a URL
    (re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@"), lambda m: m.group(1) + REDACTED + "@"),
]


def redact_str(s: str) -> str:
    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s


def truncate_str(s: str, limit: int) -> str:
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:limit] + "…[truncated " + str(len(s) - limit) + " chars]"


def redact(value, limit: int = 0):
    """Deep-copy value with secrets masked and (if limit > 0) long strings truncated.

    Under a sensitive key name only string values are masked: secrets are strings, while
    numbers there are counts (Cursor's input_tokens, output_tokens, ...).
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            is_count = isinstance(k, str) and k.lower().endswith("_tokens") and isinstance(v, str) and v.isdigit()
            if isinstance(k, str) and _SENSITIVE_KEY_RE.match(k) and isinstance(v, str) and not is_count:
                out[k] = REDACTED
            else:
                out[k] = redact(v, limit)
        return out
    if isinstance(value, list):
        return [redact(v, limit) for v in value]
    if isinstance(value, str):
        return truncate_str(redact_str(value), limit)
    return value
