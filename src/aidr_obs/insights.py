"""Derived views over captured hook events: actions, destinations, sensitive access, sessions, usage.

One agent action can fire several hooks (Cursor sends preToolUse, beforeShellExecution and
postToolUse for one shell command), so captures are first grouped into actions by
(agent, tool_use_id). Cursor's afterFileEdit carries no tool_use_id and is merged into the
Write/Edit action for the same file in the same turn. An action's verdict is the most
severe verdict of its events.

Everything here works on the redacted, possibly truncated copies the tap stored, so a host
or path late in a very long command can be missed.
"""

import ipaddress
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import PurePosixPath

SEVERITY = {"block": 6, "ask": 5, "mask": 4, "context": 3, "other": 2, "dry_run": 1, "allow": 0}
PROMPT_EVENTS = ("UserPromptSubmit", "beforeSubmitPrompt")
RESPONSE_EVENTS = ("Stop", "afterAgentResponse")
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "StrReplace", "NotebookEdit", "EditNotebook")
CURSOR_EVENT_TOOLS = {"beforeShellExecution": "Shell", "beforeReadFile": "Read", "afterFileEdit": "Edit"}


@dataclass
class Action:
    agent: str
    session: str | None
    turn: str | None
    ts: str
    ts_ms: int
    tool: str | None
    kind: str  # tool, prompt, response
    events: list = field(default_factory=list)
    capture_ids: list = field(default_factory=list)
    verdict: str = "allow"
    reason: str | None = None
    input: dict = field(default_factory=dict)
    command: str | None = None
    url: str | None = None
    file_path: str | None = None
    mcp_server: str | None = None
    sandbox: bool | None = None
    duration_ms: float | None = None
    project: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    text: str | None = None
    tokens: dict | None = None


def _as_dict(value) -> dict:
    """tool_input arrives as an object, or (Cursor MCP calls) as a JSON string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _project(stdin: dict) -> str | None:
    roots = stdin.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str) and roots[0]:
        return roots[0]
    cwd = stdin.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _tool_name(agent: str, event: str | None, stdin: dict, fallback: str | None) -> str | None:
    name = stdin.get("tool_name") or fallback
    if agent == "cursor" and not stdin.get("tool_name"):
        name = CURSOR_EVENT_TOOLS.get(event or "", name)
    if isinstance(name, str) and name.startswith("MCP:"):
        name = name[4:]
    return name


def _load_captures(conn: sqlite3.Connection, since: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, ts_ms, agent, event, tool_name, session_id, verdict, reason, stdin_json, latency_ms"
        " FROM captures WHERE ts_ms >= ? ORDER BY ts_ms, id",
        (since,),
    ).fetchall()
    out = []
    for r in rows:
        rec = dict(r)
        try:
            stdin = json.loads(rec.pop("stdin_json") or "{}")
        except ValueError:
            stdin = {}
        rec["stdin"] = stdin if isinstance(stdin, dict) else {}
        out.append(rec)
    return out


def _merge(action: Action, rec: dict) -> None:
    stdin = rec["stdin"]
    action.events.append(rec["event"])
    action.capture_ids.append(rec["id"])
    verdict = rec["verdict"] or "allow"
    if SEVERITY.get(verdict, 2) > SEVERITY.get(action.verdict, 0):
        action.verdict, action.reason = verdict, rec["reason"]
    tool_input = _as_dict(stdin.get("tool_input"))
    if tool_input:
        action.input = {**tool_input, **action.input}
    action.command = action.command or stdin.get("command") or tool_input.get("command")
    url = tool_input.get("url") or stdin.get("url")
    action.url = action.url or (url if isinstance(url, str) else None)
    path = stdin.get("file_path") or tool_input.get("file_path") or tool_input.get("path")
    action.file_path = action.file_path or (path if isinstance(path, str) else None)
    action.mcp_server = action.mcp_server or stdin.get("mcp_server_name")
    if isinstance(stdin.get("sandbox"), bool):
        action.sandbox = stdin["sandbox"]
    duration = _num(stdin.get("duration_ms")) if "duration_ms" in stdin else _num(stdin.get("duration"))
    if duration is not None:
        action.duration_ms = duration
    action.project = action.project or _project(stdin)
    action.model = action.model or stdin.get("model_id") or stdin.get("model")
    action.permission_mode = action.permission_mode or stdin.get("permission_mode")


def load_actions(conn: sqlite3.Connection, since: int = 0) -> list[Action]:
    actions: list[Action] = []
    by_tool_use: dict[tuple, Action] = {}
    pending_edits = []
    for rec in _load_captures(conn, since):
        stdin = rec["stdin"]
        agent, event = rec["agent"] or "?", rec["event"]
        session = rec["session_id"] or stdin.get("session_id") or stdin.get("conversation_id")
        turn = stdin.get("prompt_id") or stdin.get("generation_id")
        if event in PROMPT_EVENTS or event in RESPONSE_EVENTS:
            kind = "prompt" if event in PROMPT_EVENTS else "response"
            a = Action(agent, session, turn, rec["ts"], rec["ts_ms"], None, kind)
            _merge(a, rec)
            text = stdin.get("prompt") if kind == "prompt" else stdin.get("last_assistant_message", stdin.get("text"))
            a.text = text if isinstance(text, str) else None
            if kind == "response" and any(k in stdin for k in ("input_tokens", "output_tokens")):
                a.tokens = {
                    k: _num(stdin.get(k))
                    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
                }
            actions.append(a)
            continue
        key = (agent, stdin.get("tool_use_id")) if stdin.get("tool_use_id") else None
        if key and key in by_tool_use:
            _merge(by_tool_use[key], rec)
            continue
        a = Action(
            agent, session, turn, rec["ts"], rec["ts_ms"], _tool_name(agent, event, stdin, rec["tool_name"]), "tool"
        )
        _merge(a, rec)
        if key:
            by_tool_use[key] = a
        if agent == "cursor" and event == "afterFileEdit" and not key:
            pending_edits.append(a)
        else:
            actions.append(a)
    for edit in pending_edits:
        target = next(
            (
                a
                for a in reversed(actions)
                if a.agent == edit.agent
                and a.turn == edit.turn
                and a.file_path == edit.file_path
                and a.tool in EDIT_TOOLS
                and a.ts_ms <= edit.ts_ms + 5000
            ),
            None,
        )
        if target:
            target.events.extend(edit.events)
            target.capture_ids.extend(edit.capture_ids)
            if SEVERITY.get(edit.verdict, 0) > SEVERITY.get(target.verdict, 0):
                target.verdict, target.reason = edit.verdict, edit.reason
        else:
            actions.append(edit)
    actions.sort(key=lambda a: a.ts_ms)
    return actions


# --- destinations ----------------------------------------------------------------------------------

# A URL's host stops at characters no hostname can contain. Besides the URL delimiters this covers the
# brackets, quotes and punctuation that wrap URLs in Markdown, JSON and shell text: "[docs](https://x.org)"
# must yield x.org, not "x.org)".
_URL_RE = re.compile(
    r"\b[a-z][a-z0-9+.-]*://(?:[^/\s'\"`@]+@)?(\[[0-9a-f:]+\]|[^/\s'\"`:?#()<>\[\]{}|\\,;&!*]+)", re.IGNORECASE
)
_USER_HOST_RE = re.compile(
    r"\b(?:ssh|scp|sftp|rsync|git|mosh)\b[^|;&\n]*?\b[\w.-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.IGNORECASE
)
_GIT_SCP_RE = re.compile(r"\bgit@([a-z0-9-]+(?:\.[a-z0-9-]+)+):", re.IGNORECASE)
_BARE_HOST_RE = re.compile(
    r"\b(?:curl|wget|http|https|nc|ncat|telnet|ping|dig|nslookup|host|ssh|openssl\s+s_client\s+-connect)"
    r"\s+(?:-{1,2}[\w-]+(?:[ =](?!-)\S+)?\s+)*([a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}|\d{1,3}(?:\.\d{1,3}){3})\b",
    re.IGNORECASE,
)
_FILE_EXT = re.compile(r"\.(?:py|js|ts|json|md|txt|sh|log|toml|yaml|yml|html|css|csv|lock|cfg|ini)$", re.IGNORECASE)


def host_class(host: str) -> str:
    h = host.strip("[]").lower()
    if h in ("localhost",) or h.endswith(".localhost"):
        return "loopback"
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return "domain"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private or ip.is_link_local:
        return "private-ip"
    return "public-ip"


def hosts_in_text(text: str) -> list[tuple[str, str]]:
    """(host, how) pairs found in a command or free text; how is url, ssh or command."""
    found = []
    for m in _URL_RE.finditer(text):
        found.append((m.group(1), "url"))
    for regex in (_USER_HOST_RE, _GIT_SCP_RE):
        for m in regex.finditer(text):
            found.append((m.group(1), "ssh"))
    for m in _BARE_HOST_RE.finditer(text):
        if not _FILE_EXT.search(m.group(1)):
            found.append((m.group(1), "command"))
    seen, out = set(), []
    for host, how in found:
        host = host.lower().rstrip(".")
        if host and host not in seen and "*" not in host and "redacted" not in host:
            seen.add(host)
            out.append((host, how))
    return out


def destinations_of(action: Action) -> list[dict]:
    out = []
    if action.url:
        for host, _ in hosts_in_text(action.url):
            out.append({"kind": "web", "target": host, "via": "url"})
    if action.command:
        for host, how in hosts_in_text(action.command):
            if not any(d["target"] == host for d in out):
                out.append({"kind": "network", "target": host, "via": how})
    if action.mcp_server:
        out.append({"kind": "mcp", "target": action.mcp_server, "via": "mcp_server_name"})
    for d in out:
        d["class"] = "mcp" if d["kind"] == "mcp" else host_class(d["target"])
    return out


def _summary(a: Action) -> str:
    if a.command:
        return a.command
    if a.url:
        return a.url
    if a.file_path:
        return a.file_path
    if a.input:
        return json.dumps(a.input, ensure_ascii=False)
    return ""


def destinations(conn: sqlite3.Connection, since: int) -> dict:
    groups: dict[tuple, dict] = {}
    for a in load_actions(conn, since):
        if a.kind != "tool":
            continue
        for d in destinations_of(a):
            key = (d["kind"], d["target"])
            g = groups.setdefault(
                key, {**d, "count": 0, "verdicts": {}, "agents": set(), "tools": set(), "first": a.ts, "examples": []}
            )
            g["count"] += 1
            g["verdicts"][a.verdict] = g["verdicts"].get(a.verdict, 0) + 1
            g["agents"].add(a.agent)
            if a.tool:
                g["tools"].add(a.tool)
            g["last"] = a.ts
            if len(g["examples"]) < 5:
                g["examples"].append(
                    {
                        "ts": a.ts,
                        "tool": a.tool,
                        "verdict": a.verdict,
                        "summary": _summary(a)[:300],
                        "capture_id": a.capture_ids[0],
                    }
                )
    items = [{**g, "agents": sorted(g["agents"]), "tools": sorted(g["tools"])} for g in groups.values()]
    items.sort(key=lambda g: (-max((SEVERITY.get(v, 0) for v in g["verdicts"]), default=0), -g["count"]))
    by_class: dict[str, int] = {}
    for g in items:
        by_class[g["class"]] = by_class.get(g["class"], 0) + g["count"]
    return {"items": items, "by_class": by_class}


# --- sensitive access ------------------------------------------------------------------------------

SENSITIVE_PATTERNS = [
    ("ssh key or config", re.compile(r"(?:^|/)\.ssh/|(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)\b")),
    ("env file", re.compile(r"(?:^|/)\.env(?:\.[\w-]+)?$|(?:^|[\s/'\"])\.env(?:\.[\w-]+)?(?=$|[\s'\";|])")),
    ("private key or cert", re.compile(r"\.(?:pem|key|p12|pfx|jks|keystore)\b", re.IGNORECASE)),
    ("cloud credentials", re.compile(r"(?:^|/)\.(?:aws|azure|config/gcloud|kube|docker)/(?:credentials|config)")),
    ("package registry token", re.compile(r"(?:^|/)\.(?:npmrc|pypirc|netrc|git-credentials|yarnrc)\b")),
    ("gpg", re.compile(r"(?:^|/)\.gnupg/")),
    ("credentials file", re.compile(r"(?:credentials|secrets?)(?:\.[\w]+)?(?:$|[\s'\";|])", re.IGNORECASE)),
    ("gh/cli tokens", re.compile(r"(?:^|/)\.config/gh/hosts\.yml|(?:^|/)\.config/hub\b")),
    ("keychain", re.compile(r"Keychains/|\bsecurity\s+(?:find|dump|export)-", re.IGNORECASE)),
    ("browser profile", re.compile(r"(?:Cookies|Login Data|Local State)(?:$|['\"])|/Chrome/Default/", re.IGNORECASE)),
]
EXPOSURE_COMMANDS = re.compile(r"(?:^|[;&|]\s*|\$\()\s*(?:printenv|env)\s*(?:$|[;&|)])")


def sensitive_hits(action: Action) -> list[str]:
    hits = []
    for text in (action.file_path, action.command):
        if not text:
            continue
        for label, regex in SENSITIVE_PATTERNS:
            if regex.search(text) and label not in hits:
                hits.append(label)
    if action.command and EXPOSURE_COMMANDS.search(action.command) and "environment dump" not in hits:
        hits.append("environment dump")
    return hits


def _access(a: Action) -> str:
    if a.tool in EDIT_TOOLS or "afterFileEdit" in a.events:
        return "edit"
    if a.command:
        return "shell"
    if a.file_path:
        return "read"
    return "tool"


def sensitive(conn: sqlite3.Connection, since: int) -> dict:
    hits, unsandboxed, modes = [], [], {}
    for a in load_actions(conn, since):
        if a.permission_mode:
            modes[(a.agent, a.permission_mode)] = modes.get((a.agent, a.permission_mode), 0) + 1
        if a.kind != "tool":
            continue
        labels = sensitive_hits(a)
        row = {
            "ts": a.ts,
            "agent": a.agent,
            "tool": a.tool,
            "access": _access(a),
            "verdict": a.verdict,
            "summary": _summary(a)[:300],
            "file_path": a.file_path,
            "capture_id": a.capture_ids[0],
            "session": a.session,
        }
        if labels:
            hits.append({**row, "labels": labels})
        if a.agent == "cursor" and a.command and a.sandbox is False:
            unsandboxed.append(row)
    by_label: dict[str, int] = {}
    for h in hits:
        for label in h["labels"]:
            by_label[label] = by_label.get(label, 0) + 1
    return {
        "hits": list(reversed(hits))[:500],
        "by_label": dict(sorted(by_label.items(), key=lambda kv: -kv[1])),
        "unsandboxed": list(reversed(unsandboxed))[:500],
        "unsandboxed_count": len(unsandboxed),
        "permission_modes": [
            {"agent": k[0], "mode": k[1], "events": n} for k, n in sorted(modes.items(), key=lambda kv: -kv[1])
        ],
    }


# --- sessions --------------------------------------------------------------------------------------


def _session_key(a: Action) -> str:
    return a.agent + "|" + (a.session or "?")


def sessions(conn: sqlite3.Connection, since: int) -> dict:
    groups: dict[str, dict] = {}
    for a in load_actions(conn, since):
        key = _session_key(a)
        g = groups.setdefault(
            key,
            {
                "key": key,
                "agent": a.agent,
                "session": a.session,
                "first": a.ts,
                "first_ms": a.ts_ms,
                "prompts": 0,
                "tool_calls": 0,
                "responses": 0,
                "verdicts": {},
                "projects": set(),
                "models": set(),
                "sensitive": 0,
                "destinations": set(),
                "output_tokens": 0.0,
                "input_tokens": 0.0,
            },
        )
        g["last"], g["last_ms"] = a.ts, a.ts_ms
        g["prompts" if a.kind == "prompt" else "responses" if a.kind == "response" else "tool_calls"] += 1
        g["verdicts"][a.verdict] = g["verdicts"].get(a.verdict, 0) + 1
        if a.project:
            g["projects"].add(a.project)
        if a.model:
            g["models"].add(a.model)
        if a.kind == "tool":
            g["sensitive"] += 1 if sensitive_hits(a) else 0
            g["destinations"].update(d["target"] for d in destinations_of(a))
        if a.tokens:
            g["output_tokens"] += a.tokens.get("output_tokens") or 0
            g["input_tokens"] += a.tokens.get("input_tokens") or 0
    items = []
    for g in groups.values():
        items.append(
            {
                **g,
                "projects": sorted(g["projects"]),
                "models": sorted(g["models"]),
                "destinations": len(g["destinations"]),
                "duration_ms": g["last_ms"] - g["first_ms"],
            }
        )
    items.sort(key=lambda g: -g["last_ms"])
    return {"items": items}


def session_detail(conn: sqlite3.Connection, key: str, since: int = 0) -> dict:
    turns: list[dict] = []
    by_turn: dict[str, dict] = {}
    for a in load_actions(conn, since):
        if _session_key(a) != key:
            continue
        turn_key = a.turn or (
            "loose-" + str(len(turns)) if a.kind == "prompt" else (turns[-1]["turn"] if turns else "loose")
        )
        t = by_turn.get(turn_key)
        if t is None:
            t = {"turn": turn_key, "ts": a.ts, "prompt": None, "response": None, "tools": [], "tokens": None}
            by_turn[turn_key] = t
            turns.append(t)
        item = {"ts": a.ts, "verdict": a.verdict, "reason": a.reason, "capture_id": a.capture_ids[0]}
        if a.kind == "prompt":
            t["prompt"] = {**item, "text": (a.text or "")[:2000]}
        elif a.kind == "response":
            t["response"] = {**item, "text": (a.text or "")[:2000]}
            t["tokens"] = a.tokens
        else:
            t["tools"].append(
                {
                    **item,
                    "tool": a.tool,
                    "summary": _summary(a)[:400],
                    "events": a.events,
                    "duration_ms": a.duration_ms,
                    "sensitive": sensitive_hits(a),
                    "destinations": [d["target"] for d in destinations_of(a)],
                }
            )
    return {"key": key, "turns": turns}


# --- usage and performance -------------------------------------------------------------------------


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]


def usage(conn: sqlite3.Connection, since: int) -> dict:
    actions = load_actions(conn, since)
    by_model: dict[str, dict] = {}
    token_series: dict[str, dict] = {}
    redacted_responses = 0
    for a in actions:
        if a.kind != "response" or a.agent != "cursor":
            continue
        if not a.tokens or all(v is None for v in a.tokens.values()):
            redacted_responses += 1
            continue
        m = by_model.setdefault(
            a.model or "?",
            {
                "model": a.model or "?",
                "responses": 0,
                "input_tokens": 0.0,
                "output_tokens": 0.0,
                "cache_read_tokens": 0.0,
                "cache_write_tokens": 0.0,
            },
        )
        m["responses"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            m[k] += a.tokens.get(k) or 0
        day = a.ts[:13]
        s = token_series.setdefault(day, {"t": day, "input_tokens": 0.0, "output_tokens": 0.0})
        s["input_tokens"] += a.tokens.get("input_tokens") or 0
        s["output_tokens"] += a.tokens.get("output_tokens") or 0

    tools: dict[tuple, list] = {}
    for a in actions:
        if a.kind == "tool" and a.duration_ms is not None:
            tools.setdefault((a.agent, a.tool or "?"), []).append(a.duration_ms)
    tool_perf = [
        {
            "agent": k[0],
            "tool": k[1],
            "count": len(v),
            "p50": _pct(v, 0.5),
            "p95": _pct(v, 0.95),
            "max": max(v),
            "total": sum(v),
        }
        for k, v in tools.items()
    ]
    tool_perf.sort(key=lambda r: -r["total"])

    hook_lat: dict[tuple, list] = {}
    for r in conn.execute(
        "SELECT agent, event, latency_ms FROM captures WHERE ts_ms >= ? AND latency_ms IS NOT NULL", (since,)
    ):
        hook_lat.setdefault((r["agent"], r["event"]), []).append(r["latency_ms"])
    hook_perf = [
        {"agent": k[0], "event": k[1], "count": len(v), "p50": _pct(v, 0.5), "p95": _pct(v, 0.95), "max": max(v)}
        for k, v in hook_lat.items()
    ]
    hook_perf.sort(key=lambda r: -(r["p95"] or 0))

    projects: dict[str, dict] = {}
    for a in actions:
        p = projects.setdefault(
            a.project or "?",
            {
                "project": a.project or "?",
                "agents": set(),
                "sessions": set(),
                "tool_calls": 0,
                "prompts": 0,
                "flagged": 0,
                "sensitive": 0,
            },
        )
        p["agents"].add(a.agent)
        if a.session:
            p["sessions"].add(a.agent + a.session)
        if a.kind == "tool":
            p["tool_calls"] += 1
            p["sensitive"] += 1 if sensitive_hits(a) else 0
        elif a.kind == "prompt":
            p["prompts"] += 1
        if SEVERITY.get(a.verdict, 0) >= SEVERITY["context"]:
            p["flagged"] += 1
    project_rows = [
        {
            **p,
            "name": PurePosixPath(p["project"]).name or p["project"],
            "agents": sorted(p["agents"]),
            "sessions": len(p["sessions"]),
        }
        for p in projects.values()
    ]
    project_rows.sort(key=lambda p: -(p["tool_calls"] + p["prompts"]))
    return {
        "tokens_by_model": sorted(by_model.values(), key=lambda m: -m["output_tokens"]),
        "token_series": [token_series[k] for k in sorted(token_series)],
        "redacted_token_responses": redacted_responses,
        "tool_perf": tool_perf,
        "hook_perf": hook_perf,
        "projects": project_rows,
    }
