"""On-demand Jev second opinion for a captured hook verdict.

Called only from the dashboard (never from the tap). Stdlib urllib only: the
serve process may talk to the local Jev sandbox; the tap stays offline.

Env (on `aidr-obs serve`, or JEV_* lines in this repo's .env):
  JEV_API_TOKEN  — required to ask; same value as jevai-dev-lab/.env
  JEV_BASE_URL   — default http://127.0.0.1:8790; host must be loopback
  AIDR_OBS_JEV_HOSTS — extra host names allowed in JEV_BASE_URL, comma-separated (for
                   example the compose service name `jev`). IP literals here are ignored.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

STANCES = ("agree", "would_block", "would_allow")
BACKENDS = ("gateway", "ollama")
CASE_ID = "verdict-review"
DEFAULT_BASE = "http://127.0.0.1:8790"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
# Jev allows up to 300 s for a cold model load on the homelab Ollama box.
TIMEOUT_SECONDS = 330.0
PAYLOAD_LIMIT = 4000
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)


class ReviewError(Exception):
    """User-facing failure; message is safe to show in the UI (no secrets)."""


def load_env_file(path: Path | None = None) -> list[str]:
    """Copy JEV_* keys from a .env file into os.environ without overriding exported values.

    Returns the keys it set. A missing file is not an error.
    """
    env_path = path or ENV_FILE
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    loaded = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not key.startswith("JEV_") or key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def config() -> dict:
    """Return {token, base_url, configured}. Never includes the token in API responses."""
    token = (os.environ.get("JEV_API_TOKEN") or "").strip()
    base = (os.environ.get("JEV_BASE_URL") or DEFAULT_BASE).strip().rstrip("/") or DEFAULT_BASE
    return {"token": token, "base_url": base, "configured": bool(token)}


def extra_jev_hosts() -> frozenset[str]:
    """Host names from AIDR_OBS_JEV_HOSTS. IP literals are dropped so only names can widen the check."""
    names = set()
    for raw in (os.environ.get("AIDR_OBS_JEV_HOSTS") or "").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        try:
            ipaddress.ip_address(name)
        except ValueError:
            names.add(name)
    return frozenset(names)


def assert_loopback_base(base_url: str) -> str:
    """Raise ReviewError unless the URL host is loopback or allowlisted. Returns the normalized base."""
    base = (base_url or DEFAULT_BASE).strip().rstrip("/") or DEFAULT_BASE
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https"):
        raise ReviewError("JEV_BASE_URL must be http or https on loopback")
    host = (parts.hostname or "").lower()
    if host not in _LOOPBACK and host not in extra_jev_hosts():
        raise ReviewError(
            "JEV_BASE_URL host must be loopback (127.0.0.1, localhost, or ::1) or listed in AIDR_OBS_JEV_HOSTS"
        )
    if parts.path not in ("", "/"):
        raise ReviewError("JEV_BASE_URL must not include a path")
    return base


def build_prompt(capture: dict) -> str:
    """User prompt for POST /v1/complete: redacted capture summary + JSON schema."""
    stdin = _truncate(_as_text(capture.get("stdin_json")), PAYLOAD_LIMIT)
    stdout = _truncate(_as_text(capture.get("stdout_text")), PAYLOAD_LIMIT)
    reason = capture.get("reason") or "(none)"
    return (
        "You are reviewing one AI-agent guardrail (Noma) hook decision. "
        "Noma already made the real decision the agent saw; your reply is a second opinion only.\n\n"
        "Return one JSON object and nothing else, with exactly these keys:\n"
        '  "stance": one of "agree", "would_block", "would_allow"\n'
        '  "note": one short paragraph\n\n'
        "Stance meanings:\n"
        '- "agree": Noma\'s verdict is the right call (including when both sides would ask).\n'
        '- "would_block": you would have blocked or asked, but Noma allowed, masked, or only added context.\n'
        '- "would_allow": you would have allowed, but Noma blocked or asked.\n\n'
        f"agent: {capture.get('agent') or '?'}\n"
        f"event: {capture.get('event') or '?'}\n"
        f"tool: {capture.get('tool_name') or '(none)'}\n"
        f"noma_verdict: {capture.get('verdict') or '?'}\n"
        f"noma_reason: {reason}\n\n"
        f"hook stdout (redacted):\n{stdout}\n\n"
        f"event payload from the agent (redacted):\n{stdin}\n"
    )


def parse_reply(text: str) -> dict:
    """Parse stance + note from Jev's reply. Raises ReviewError on bad shape."""
    raw = (text or "").strip()
    if not raw:
        raise ReviewError("Jev returned an empty reply")
    m = _FENCE_RE.match(raw)
    if m:
        raw = m.group(1).strip()
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise ReviewError("Jev reply was not valid JSON") from exc
    if not isinstance(obj, dict):
        raise ReviewError("Jev reply was not a JSON object")
    stance = obj.get("stance")
    note = obj.get("note")
    if stance not in STANCES:
        raise ReviewError('Jev stance must be "agree", "would_block", or "would_allow"')
    if not isinstance(note, str) or not note.strip():
        raise ReviewError("Jev note must be a non-empty string")
    return {"stance": stance, "note": note.strip()}


def ask_jev(
    capture: dict,
    *,
    backend: str | None = None,
    token: str | None = None,
    base_url: str | None = None,
) -> dict:
    """Call local Jev and return {backend, stance, note, model, persona_sha256, asked_at}.

    backend=None lets Jev use its default (JEV_BACKEND). Does not write to the DB.
    Raises ReviewError on config, network, or parse failures.
    """
    if backend is not None and backend not in BACKENDS:
        raise ReviewError(f"backend must be one of {', '.join(BACKENDS)}")
    cfg = config()
    tok = (token if token is not None else cfg["token"]).strip()
    if not tok:
        raise ReviewError("JEV_API_TOKEN is not set (add it to .env and restart aidr-obs serve)")
    base = assert_loopback_base(base_url if base_url is not None else cfg["base_url"])
    prompt = build_prompt(capture)
    payload = {"prompt": prompt, "case_id": CASE_ID}
    if backend is not None:
        payload["backend"] = backend
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base + "/v1/complete",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + tok,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        raise ReviewError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise ReviewError(_url_error_message(exc)) from exc
    except TimeoutError as exc:
        raise ReviewError("Jev request timed out") from exc
    except OSError as exc:
        raise ReviewError("Could not reach Jev: " + type(exc).__name__) from exc

    if status >= 400:
        raise ReviewError(f"Jev HTTP {status}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReviewError("Jev returned non-JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise ReviewError("Jev response missing text")

    answered = data.get("backend")
    if backend is not None and answered != backend:
        raise ReviewError(
            f"Asked Jev for backend {backend!r} but it answered with {answered or 'no backend'}; "
            "update jevai-dev-lab to a version with backend selection"
        )
    parsed = parse_reply(data["text"])
    model = data.get("model") if isinstance(data.get("model"), str) else None
    persona = data.get("persona_sha256") if isinstance(data.get("persona_sha256"), str) else None
    return {
        "backend": answered if answered in BACKENDS else "gateway",
        "stance": parsed["stance"],
        "note": parsed["note"],
        "model": model,
        "persona_sha256": persona,
        "asked_at": int(time.time() * 1000),
    }


_REVIEW_COLS = "capture_id, backend, stance, note, model, persona_sha256, asked_at"


def get_review(conn, capture_id: str, backend: str | None = None) -> dict | None:
    """The review from one backend, or the most recent one from any backend when backend is None."""
    if backend is None:
        row = conn.execute(
            f"SELECT {_REVIEW_COLS} FROM jev_reviews WHERE capture_id = ? ORDER BY asked_at DESC LIMIT 1",
            (capture_id,),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {_REVIEW_COLS} FROM jev_reviews WHERE capture_id = ? AND backend = ?",
            (capture_id, backend),
        ).fetchone()
    return dict(row) if row else None


def list_reviews(conn, capture_id: str) -> list[dict]:
    rows = conn.execute(
        f"SELECT {_REVIEW_COLS} FROM jev_reviews WHERE capture_id = ? ORDER BY backend",
        (capture_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_review(conn, capture_id: str, review: dict) -> dict:
    asked_at = int(review.get("asked_at") or time.time() * 1000)
    backend = review.get("backend") or "gateway"
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO jev_reviews ({_REVIEW_COLS}) VALUES (?,?,?,?,?,?,?)",
            (
                capture_id,
                backend,
                review["stance"],
                review["note"],
                review.get("model"),
                review.get("persona_sha256"),
                asked_at,
            ),
        )
    return get_review(conn, capture_id, backend) or {
        "capture_id": capture_id,
        "backend": backend,
        "stance": review["stance"],
        "note": review["note"],
        "model": review.get("model"),
        "persona_sha256": review.get("persona_sha256"),
        "asked_at": asked_at,
    }


def review_capture(conn, capture_id: str, *, force: bool = False, backend: str | None = None) -> dict:
    """Return a cached review, or ask Jev and cache it. Raises ReviewError / KeyError.

    With backend=None, any cached review counts and a new one goes to Jev's default backend.
    """
    if backend is not None and backend not in BACKENDS:
        raise ReviewError(f"backend must be one of {', '.join(BACKENDS)}")
    if not force:
        cached = get_review(conn, capture_id, backend)
        if cached:
            return {**cached, "cached": True}

    row = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown capture_id: {capture_id}")
    capture = dict(row)
    for key in ("stdin_json", "stdout_text"):
        try:
            capture[key] = json.loads(capture[key]) if capture[key] else capture[key]
        except ValueError:
            pass

    result = ask_jev(capture, backend=backend)
    saved = save_review(conn, capture_id, result)
    return {**saved, "cached": False}


def _as_text(value) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, str):
        return value if value.strip() else "(empty)"
    try:
        return json.dumps(value, indent=2, default=str)
    except TypeError:
        return str(value)


def _truncate(s: str, limit: int) -> str:
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:limit] + f"…[truncated {len(s) - limit} chars]"


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    code = exc.code
    if code == 401:
        return "Jev rejected the token (check JEV_API_TOKEN matches jevai-dev-lab/.env)"
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            detail = body["detail"][:200]
    except Exception:
        pass
    if code == 503:
        return "Jev backend is not configured" + (f": {detail}" if detail else " (check jevai-dev-lab/.env)")
    if code in (502, 504):
        return "Jev could not reach its model backend" + (f": {detail}" if detail else "")
    return f"Jev HTTP {code}" + (f": {detail}" if detail else "")


def _url_error_message(exc: urllib.error.URLError) -> str:
    reason = exc.reason
    if isinstance(reason, ConnectionRefusedError) or (
        isinstance(reason, OSError) and getattr(reason, "errno", None) in (61, 111)
    ):
        return "Cannot reach Jev. Is `uv run jev serve` running on 127.0.0.1:8790?"
    if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
        return "Jev request timed out"
    return "Could not reach Jev. Is `uv run jev serve` running?"
