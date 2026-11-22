"""Dashboard server: static page + read-only JSON API.

The process binds 127.0.0.1 unless AIDR_OBS_CONTAINER=1, in which case it may bind
0.0.0.0 so Docker can publish the port. Publish that port on the host's loopback, or on
the LAN (AIDR_OBS_BIND=0.0.0.0) the way the Kaizen hub is published.

Requests are refused unless the Host header is 127.0.0.1:<port>, localhost:<port>, or a
name listed in AIDR_OBS_ALLOWED_HOSTS (DNS-rebinding guard). When AIDR_OBS_UI_PASSWORD is
set, pages and /api/* need the session cookie from POST /login. POST /api/push* does not:
it keeps the bearer token. GET /health stays open and returns only {"ok": true}.

The mutating endpoints (POST /api/ingest, POST /api/noise, POST /api/push*) also need a
custom header, which a cross-site form can't send. POST /login and POST /logout do not.

Static files under vendor/, js/ and css/ are served by exact relative name. The name is
checked as path segments and then resolved back under that directory, so a request
cannot climb out of it. Scripts are only loaded from this origin (script-src 'self').
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import graph, insights, jevreview, logparse, noise, paths, push, store, tapinstall

STATIC = Path(__file__).with_name("static")
HOST = "127.0.0.1"
CONTAINER_BIND = "0.0.0.0"
VERDICT_ACTIONS = ("block", "ask", "mask", "context", "other")
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:"
)
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_NEXT = re.compile(r"^/(#[A-Za-z0-9_?=&.%\-/]*)?\Z")
SESSION_COOKIE = "aidr_obs_session"
SESSION_SECONDS = 7 * 24 * 3600
LOGIN_LOCK_AFTER = 8
LOGIN_LOCK_SECONDS = 60
_LOGIN_FAILS: dict[str, tuple[int, float]] = {}
_LOGIN_LOCK = threading.Lock()
_STATIC_KINDS = {
    "vendor": (STATIC / "vendor", {".js"}),
    "js": (STATIC / "js", {".js"}),
    "css": (STATIC / "css", {".css"}),
}
_HEALTH_COLUMNS = ("key_source", "installed_config", "url", "sender", "outcome", "hook_file")
_FAILURES = ("failed", "budget_exhausted")


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _pct(values: list[int], p: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]


def _since(q: dict) -> int:
    try:
        return int((q.get("since") or ["0"])[0])
    except ValueError:
        return 0


def _bounds(q: dict) -> tuple[int, int | None]:
    since = _since(q)
    raw = (q.get("until") or [""])[0]
    if raw == "":
        return since, None
    try:
        return since, int(raw)
    except ValueError:
        return since, None


def _opt_int(q: dict, key: str) -> int:
    raw = (q.get(key) or [""])[0]
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _span(column: str, since: int, until: int | None) -> tuple[str, tuple]:
    if until is None:
        return f"{column} >= ?", (since,)
    return f"{column} >= ? AND {column} <= ?", (since, until)


def _bucket_ms(since: int, now: int) -> int:
    span = now - since if since else 30 * 86400_000
    for size in (60_000, 300_000, 900_000, 3_600_000, 21_600_000, 86_400_000):
        if span / size <= 72:
            return size
    return 86_400_000


def summary(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    now = int(time.time() * 1000)
    run_sql, run_args = _span("start_ms", since, until)
    cap_sql, cap_args = _span("ts_ms", since, until)
    runs = _rows(
        conn.execute(
            "SELECT event, outcome, start_ms, duration_ms, key_source, agent FROM runs WHERE " + run_sql, run_args
        )
    )
    caps = _rows(
        conn.execute("SELECT event, verdict, ts_ms, latency_ms, run_id FROM captures WHERE " + cap_sql, cap_args)
    )
    if not since:
        first = min([r["start_ms"] for r in runs] + [c["ts_ms"] for c in caps] or [now])
        since_eff = first
    else:
        since_eff = since
    end = until if until is not None else now
    bucket = _bucket_ms(since_eff, end)
    start = since_eff - since_eff % bucket

    series: dict[int, dict[str, int]] = {}
    for r in runs:
        b = r["start_ms"] - r["start_ms"] % bucket
        series.setdefault(b, {})
        series[b][r["event"] or "?"] = series[b].get(r["event"] or "?", 0) + 1
    # Captures with no debug-log run (NOMA_DEBUG off) still count as hook invocations.
    for c in caps:
        if c["run_id"]:
            continue
        b = c["ts_ms"] - c["ts_ms"] % bucket
        series.setdefault(b, {})
        series[b][c["event"] or "?"] = series[b].get(c["event"] or "?", 0) + 1

    def count(items, key):
        out: dict[str, int] = {}
        for it in items:
            out[it[key] or "?"] = out.get(it[key] or "?", 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    cap_lat = [c["latency_ms"] for c in caps if c["latency_ms"] is not None]
    run_lat = [r["duration_ms"] for r in runs]
    latency_src = "tap" if cap_lat else "debug log"
    lat = cap_lat or run_lat
    err_sql, err_args = _span("ts_ms", since, until)
    return {
        "now": now,
        "since": since_eff,
        "until": until,
        "bucket_ms": bucket,
        "series": [{"t": t, "counts": series.get(t, {})} for t in range(start, end + bucket, bucket)],
        "totals": {
            "runs": len(runs),
            "captures": len(caps),
            "errors": conn.execute(
                "SELECT COUNT(*) FROM log_lines WHERE level = 'ERROR' AND " + err_sql, err_args
            ).fetchone()[0],
        },
        "by_event": count(runs, "event") if runs else count(caps, "event"),
        "by_outcome": count(runs, "outcome"),
        "by_verdict": count(caps, "verdict"),
        "by_agent": count(runs, "agent"),
        "latency": {"source": latency_src, "p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "max": max(lat or [0])},
        "db": str(paths.db_path()),
        "log_dir": str(paths.noma_log_dir()),
    }


def _norm_tool(event: str | None, tool_name: str | None) -> tuple[str, str | None, bool]:
    if event in insights.PROMPT_EVENTS:
        return "prompt", None, False
    if event in insights.RESPONSE_EVENTS:
        return "response", None, False
    name = tool_name or ""
    mcp = name.startswith("MCP:")
    if mcp:
        name = name[4:]
    return "tool", name or (event or "?"), mcp


def verdicts(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    now = int(time.time() * 1000)
    cap_sql, cap_args = _span("ts_ms", since, until)
    marks = ",".join("?" * len(VERDICT_ACTIONS))
    item_sql, item_args = _span("c.ts_ms", since, until)
    items = _rows(
        conn.execute(
            "SELECT c.id, c.ts, c.ts_ms, c.event, c.tool_name, c.verdict, c.reason, c.session_id, c.run_id,"
            " (SELECT j.stance FROM jev_reviews j WHERE j.capture_id = c.id"
            "  ORDER BY j.asked_at DESC LIMIT 1) AS jev_stance,"
            " (SELECT group_concat(j.backend || ':' || j.stance, ',') FROM jev_reviews j"
            "  WHERE j.capture_id = c.id) AS jev_stances"
            " FROM captures c"
            " WHERE " + item_sql + " AND c.verdict IN (" + marks + ")"
            " ORDER BY c.ts_ms DESC LIMIT 500",
            (*item_args, *VERDICT_ACTIONS),
        )
    )
    for item in items:
        pairs = (part.partition(":") for part in (item["jev_stances"] or "").split(",") if part)
        item["jev_stances"] = {backend: stance for backend, _, stance in pairs}
    by_tool = _rows(
        conn.execute(
            "SELECT COALESCE(tool_name, '(' || COALESCE(event, '?') || ')') AS tool, verdict, COUNT(*) AS n"
            " FROM captures WHERE " + cap_sql + " GROUP BY tool, verdict ORDER BY n DESC",
            cap_args,
        )
    )
    rows = _rows(conn.execute("SELECT event, tool_name, verdict, ts_ms FROM captures WHERE " + cap_sql, cap_args))
    tools: dict[str, dict] = {}
    conversations: dict[tuple, dict] = {}
    for row in rows:
        kind, name, mcp = _norm_tool(row["event"], row["tool_name"])
        verdict = row["verdict"] or "allow"
        if kind == "tool":
            slot = tools.setdefault(name, {"tool": name, "mcp": mcp, "verdicts": {}, "n": 0})
            slot["mcp"] = slot["mcp"] or mcp
        else:
            slot = conversations.setdefault(
                (kind, row["event"]), {"kind": kind, "event": row["event"], "verdicts": {}, "n": 0}
            )
        slot["verdicts"][verdict] = slot["verdicts"].get(verdict, 0) + 1
        slot["n"] += 1
    ranked = sorted(tools.values(), key=lambda item: -item["n"])
    top = {item["tool"] for item in ranked[:8]}
    end = until if until is not None else now
    first = min((row["ts_ms"] for row in rows), default=now)
    since_eff = since or first
    bucket = _bucket_ms(since_eff, end)
    mix: dict[str, dict[int, dict[str, int]]] = {}
    for row in rows:
        kind, name, _mcp = _norm_tool(row["event"], row["tool_name"])
        if kind != "tool" or name not in top:
            continue
        slot = row["ts_ms"] - row["ts_ms"] % bucket
        verdicts_at = mix.setdefault(name, {}).setdefault(slot, {})
        verdict = row["verdict"] or "allow"
        verdicts_at[verdict] = verdicts_at.get(verdict, 0) + 1
    tool_series = [
        {"tool": name, "points": [{"t": t, "verdicts": counts} for t, counts in sorted(points.items())]}
        for name, points in mix.items()
    ]
    return {
        "items": items,
        "by_tool": by_tool,
        "tools": ranked,
        "conversations": sorted(conversations.values(), key=lambda item: -item["n"]),
        "tool_series": tool_series,
        "bucket_ms": bucket,
    }


def events(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    get = lambda k: (q.get(k) or [""])[0]  # noqa: E731
    limit = min(int(get("limit") or 200), 1000)
    offset = int(get("offset") or 0)
    run_sql, run_args = _span("r.start_ms", since, until)
    cap_sql, cap_args = _span("c.ts_ms", since, until)
    rows = _rows(
        conn.execute(
            "SELECT r.run_id, c.id AS capture_id, r.agent, r.start_ms AS ts_ms, r.start_ts AS ts, r.event,"
            " c.tool_name, r.outcome, c.verdict, c.reason, COALESCE(c.latency_ms, r.duration_ms) AS latency_ms,"
            " r.error_count, r.pid, r.post_bytes"
            " FROM runs r LEFT JOIN captures c ON c.run_id = r.run_id"
            " WHERE " + run_sql + " UNION ALL "
            "SELECT NULL, c.id, c.agent, c.ts_ms, c.ts, c.event, c.tool_name,"
            " CASE WHEN c.dry_run THEN 'dry_run' ELSE NULL END, c.verdict, c.reason, c.latency_ms,"
            " NULL, c.child_pid, NULL"
            " FROM captures c WHERE c.run_id IS NULL AND " + cap_sql + " ORDER BY ts_ms DESC",
            (*run_args, *cap_args),
        )
    )

    def keep(r):
        for key in ("agent", "event", "tool_name", "verdict", "outcome"):
            want = get(key)
            if want and (r.get(key) or "") != want:
                return False
        text = get("q").lower()
        if text and text not in json.dumps(r).lower():
            return False
        return True

    filtered = [r for r in rows if keep(r)]
    facets = {
        k: sorted({r[k] for r in rows if r.get(k)}) for k in ("agent", "event", "tool_name", "verdict", "outcome")
    }
    return {"total": len(filtered), "items": filtered[offset : offset + limit], "facets": facets}


def event_detail(conn: sqlite3.Connection, q: dict) -> dict:
    run_id = (q.get("run_id") or [""])[0]
    capture_id = (q.get("capture_id") or [""])[0]
    run = capture = None
    if capture_id:
        capture = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
        capture = dict(capture) if capture else None
        if capture and not run_id:
            run_id = capture.get("run_id") or ""
    if run_id:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        run = dict(run) if run else None
        if run and capture is None:
            c = conn.execute("SELECT * FROM captures WHERE run_id = ?", (run_id,)).fetchone()
            capture = dict(c) if c else None
    lines = []
    if run:
        run["discovery"] = json.loads(run.pop("discovery_json") or "[]")
        lines = _rows(
            conn.execute(
                "SELECT ts, level, location, message FROM log_lines"
                " WHERE agent = ? AND pid = ? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms, rowid",
                (run["agent"], run["pid"], run["start_ms"], run["end_ms"]),
            )
        )
    jev_review = None
    jev_reviews: list[dict] = []
    if capture:
        for key in ("stdin_json", "stdout_text"):
            try:
                capture[key] = json.loads(capture[key]) if capture[key] else capture[key]
            except ValueError:
                pass
        jev_reviews = jevreview.list_reviews(conn, capture["id"])
        jev_review = max(jev_reviews, key=lambda r: r["asked_at"], default=None)
    cfg = jevreview.config()
    return {
        "run": run,
        "capture": capture,
        "lines": lines,
        "jev_review": jev_review,
        "jev_reviews": jev_reviews,
        "jev": {"configured": cfg["configured"], "base_url": cfg["base_url"], "backends": list(jevreview.BACKENDS)},
    }


def _health_level(conn: sqlite3.Connection, since: int, until: int | None) -> dict:
    rules = noise.load_rules()
    err_sql, err_args = _span("ts_ms", since, until)
    real = harmless = 0
    for row in conn.execute("SELECT message FROM log_lines WHERE level = 'ERROR' AND " + err_sql, err_args):
        if noise.match_rule(row["message"], rules):
            harmless += 1
        else:
            real += 1
    run_sql, run_args = _span("start_ms", since, until)
    outcomes = {
        (row["outcome"] or "?"): row["n"]
        for row in conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM runs WHERE " + run_sql + " GROUP BY outcome", run_args
        )
    }
    cap_sql, cap_args = _span("ts_ms", since, until)
    timeouts = conn.execute("SELECT COUNT(*) FROM captures WHERE timed_out = 1 AND " + cap_sql, cap_args).fetchone()[0]
    tap_errors = conn.execute(
        "SELECT COUNT(*) FROM captures WHERE tap_error IS NOT NULL AND " + cap_sql, cap_args
    ).fetchone()[0]
    failures = sum(outcomes.get(name, 0) for name in _FAILURES)
    if failures or tap_errors or real:
        level = "red"
    elif timeouts or outcomes.get("incomplete") or outcomes.get("no_key"):
        level = "amber"
    else:
        level = "green"
    return {
        "level": level,
        "failures": failures,
        "timeouts": timeouts,
        "tap_errors": tap_errors,
        "errors": real,
        "noise": harmless,
    }


def _mode(conn: sqlite3.Connection, since: int, until: int | None) -> str:
    cap_sql, cap_args = _span("ts_ms", since, until)
    verdicts = {
        (row[0] or "allow") for row in conn.execute("SELECT DISTINCT verdict FROM captures WHERE " + cap_sql, cap_args)
    }
    if verdicts:
        if verdicts <= {"dry_run"}:
            return "dry_run"
        if "dry_run" in verdicts:
            return "mixed"
        return "live"
    run_sql, run_args = _span("start_ms", since, until)
    outcomes = {(row[0] or "") for row in conn.execute("SELECT DISTINCT outcome FROM runs WHERE " + run_sql, run_args)}
    outcomes.discard("")
    if not outcomes:
        return "unknown"
    if outcomes <= {"dry_run"}:
        return "dry_run"
    if "dry_run" in outcomes:
        return "mixed"
    return "live"


def errors(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    rules = noise.load_rules()
    err_sql, err_args = _span("ts_ms", since, until)
    groups: dict[tuple, dict] = {}
    for r in conn.execute(
        "SELECT agent, ts, ts_ms, location, message FROM log_lines WHERE level = 'ERROR' AND "
        + err_sql
        + " ORDER BY ts_ms",
        err_args,
    ):
        sig = logparse.error_signature(r["message"])
        rule = noise.match_rule(r["message"], rules)
        key = (sig, rule["id"] if rule else "")
        g = groups.setdefault(
            key,
            {
                "signature": sig,
                "count": 0,
                "first": r["ts"],
                "agents": set(),
                "locations": set(),
                "noise": rule is not None,
                "explanation": rule["explanation"] if rule else None,
            },
        )
        g["count"] += 1
        g["last"] = r["ts"]
        g["example"] = r["message"]
        g["agents"].add(r["agent"])
        g["locations"].add(r["location"])
    error_groups = sorted(
        ({**g, "agents": sorted(g["agents"]), "locations": sorted(g["locations"])} for g in groups.values()),
        key=lambda g: (g["noise"], -g["count"]),
    )
    cap_sql, cap_args = _span("ts_ms", since, until)
    tap_errors = _rows(
        conn.execute(
            "SELECT tap_error, COUNT(*) AS n, MAX(ts) AS last FROM captures"
            " WHERE tap_error IS NOT NULL AND " + cap_sql + " GROUP BY tap_error ORDER BY n DESC",
            cap_args,
        )
    )
    run_sql, run_args = _span("start_ms", since, until)
    health = {}
    for col in _HEALTH_COLUMNS:
        health[col] = _rows(
            conn.execute(
                "SELECT COALESCE(" + col + ", '?') AS value, COUNT(*) AS n, MAX(start_ts) AS last FROM runs"
                " WHERE " + run_sql + " GROUP BY value ORDER BY n DESC",
                run_args,
            )
        )
    key_daily = _rows(
        conn.execute(
            "SELECT substr(start_ts, 1, 10) AS day, COALESCE(key_source, '?') AS key_source, COUNT(*) AS n FROM runs"
            " WHERE " + run_sql + " GROUP BY day, key_source ORDER BY day",
            run_args,
        )
    )
    retries = conn.execute(
        "SELECT COALESCE(SUM(retries),0), COALESCE(SUM(fallbacks),0) FROM runs WHERE " + run_sql, run_args
    ).fetchone()
    versions = _rows(
        conn.execute(
            "SELECT agent, COALESCE(hook_version, '') AS hook_version, COUNT(*) AS n, MAX(ts) AS last"
            " FROM captures WHERE " + cap_sql + " GROUP BY agent, hook_version ORDER BY n DESC",
            cap_args,
        )
    )
    level = _health_level(conn, since, until)
    return {
        "groups": error_groups,
        "tap_errors": tap_errors,
        "health": health,
        "key_daily": key_daily,
        "retries": retries[0],
        "fallbacks": retries[1],
        "versions": versions,
        "status": level,
    }


def mcp(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    run_sql, run_args = _span("start_ms", since, until)
    snapshots = []
    latest: dict[str, dict] = {}
    changes = []
    prev_has: dict[str, bool] = {}
    for r in conn.execute(
        "SELECT run_id, agent, start_ts, project_root, mcp_artifacts, discovery_json FROM runs"
        " WHERE mcp_artifacts IS NOT NULL AND " + run_sql + " ORDER BY start_ms",
        run_args,
    ):
        disc = json.loads(r["discovery_json"] or "[]")
        snapshots.append(
            {
                "run_id": r["run_id"],
                "agent": r["agent"],
                "ts": r["start_ts"],
                "project_root": r["project_root"],
                "artifacts": r["mcp_artifacts"],
                "sources": len(disc),
                "with_servers": sum(1 for d in disc if d["has_servers"]),
            }
        )
        for d in disc:
            key = d["scope"] + "|" + d["kind"] + "|" + d["path"]
            current = bool(d["has_servers"])
            if key in prev_has and prev_has[key] != current:
                changes.append(
                    {
                        "ts": r["start_ts"],
                        "run_id": r["run_id"],
                        "scope": d["scope"],
                        "kind": d["kind"],
                        "path": d["path"],
                        "before": prev_has[key],
                        "after": current,
                    }
                )
            prev_has[key] = current
            prev = latest.get(key)
            changed = prev is not None and prev["has_servers"] != d["has_servers"]
            latest[key] = {
                **d,
                "agent": r["agent"],
                "last_seen": r["start_ts"],
                "first_seen": prev["first_seen"] if prev else r["start_ts"],
                "changes": (prev["changes"] if prev else 0) + (1 if changed else 0),
            }
    collapsed = []
    for snap in snapshots:
        sig = (snap["agent"], snap["project_root"], snap["artifacts"], snap["sources"], snap["with_servers"])
        if collapsed and collapsed[-1]["sig"] == sig:
            collapsed[-1]["repeats"] += 1
            collapsed[-1]["until"] = snap["ts"]
        else:
            collapsed.append({**snap, "repeats": 1, "until": snap["ts"], "sig": sig})
    for row in collapsed:
        row.pop("sig", None)
    called: dict[str, dict] = {}
    for action in insights.load_actions(conn, since, until):
        if not action.mcp_server:
            continue
        group = called.setdefault(
            action.mcp_server,
            {"server": action.mcp_server, "count": 0, "agents": set(), "verdicts": {}, "last": action.ts},
        )
        group["count"] += 1
        group["agents"].add(action.agent)
        group["verdicts"][action.verdict] = group["verdicts"].get(action.verdict, 0) + 1
        group["last"] = action.ts
    called_rows = [{**group, "agents": sorted(group["agents"])} for group in called.values()]
    called_rows.sort(key=lambda group: -group["count"])
    return {
        "snapshots": list(reversed(snapshots))[:500],
        "sources": sorted(latest.values(), key=lambda d: (d["scope"], d["path"])),
        "collapsed": list(reversed(collapsed))[:200],
        "changes": list(reversed(changes))[:200],
        "called": called_rows,
    }


def destinations(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    return insights.destinations(conn, since, until)


def sensitive(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    return insights.sensitive(conn, since, until)


def sessions(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    return insights.sessions(conn, since, until)


def session_detail(conn: sqlite3.Connection, q: dict) -> dict:
    return insights.session_detail(conn, (q.get("key") or [""])[0])


def usage(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    return insights.usage(conn, since, until)


def entity_graph(conn: sqlite3.Connection, q: dict) -> dict:
    raw = (q.get("max_nodes") or [str(graph.DEFAULT_MAX_NODES)])[0]
    try:
        max_nodes = int(raw)
    except ValueError as e:
        raise ValueError("max_nodes must be an integer") from e
    focus = (q.get("focus") or [""])[0] or None
    since, until = _bounds(q)
    return graph.graph(conn, since, max(1, min(max_nodes, 2000)), focus, until)


def actions(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    return insights.list_actions(conn, since, until, q)


def search(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    return insights.search(conn, since, until, (q.get("q") or [""])[0])


def overview(conn: sqlite3.Connection, q: dict) -> dict:
    since, until = _bounds(q)
    now = int(time.time() * 1000)
    body = insights.attention(conn, since, until, now, _opt_int(q, "visit"))
    body["health"] = _health_level(conn, since, until)
    body["mode"] = _mode(conn, since, until)
    body["now"] = now
    return body


def noise_list(conn: sqlite3.Connection, q: dict) -> dict:
    del conn, q
    return {"rules": noise.load_rules(), "source": "default" if noise.using_defaults() else "user"}


def _age_ms(ts_ms: int | None) -> int | None:
    if not ts_ms:
        return None
    return max(0, int(time.time() * 1000) - int(ts_ms))


def _shipper_check(beat: dict, check_id: str) -> dict:
    age = _age_ms(beat["received_ms"])
    fresh = age is not None and age < 120_000
    return {
        "id": check_id,
        "ok": fresh,
        "label": "Mac shipper",
        "detail": ("reporting from " if fresh else "no recent heartbeat from ") + beat["device"],
        "age_ms": age,
    }


def _remote_status(conn: sqlite3.Connection) -> tuple[dict, list[dict]]:
    """Tap status last reported by the shippers, plus one freshness check per Mac.

    One Mac keeps the check id ``shipper``. Several Macs each get ``shipper:<device>``.
    """
    beats = push.heartbeats(conn)
    if not beats:
        status = {"error": "no heartbeat from the Mac shipper yet", "claude-code": {}, "cursor": {}}
        return status, [
            {
                "id": "shipper",
                "ok": False,
                "label": "Mac shipper",
                "detail": "Run aidr-obs ship install on the Mac",
            }
        ]
    if len(beats) == 1:
        return beats[0]["status"], [_shipper_check(beats[0], "shipper")]
    merged: dict = {"claude-code": {}, "cursor": {}}
    for beat in reversed(beats):
        reported = beat["status"] or {}
        for agent in ("claude-code", "cursor"):
            info = reported.get(agent) or {}
            if info:
                merged[agent] = info
    return merged, [_shipper_check(beat, "shipper:" + beat["device"]) for beat in beats]


def setup_status(conn: sqlite3.Connection, q: dict) -> dict:
    del q
    checks = []
    if push.remote_mode():
        status, shipper_checks = _remote_status(conn)
        checks.extend(shipper_checks)
    else:
        try:
            status = tapinstall.status()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            status = {"error": str(exc), "claude-code": {}, "cursor": {}}
    for agent, label in (("claude-code", "Claude Code"), ("cursor", "Cursor")):
        info = status.get(agent) or {}
        events = info.get("tap_events") or []
        checks.append(
            {
                "id": "tap-" + agent,
                "ok": bool(events),
                "label": "Tap installed for " + label,
                "detail": f"{len(events)} hook events" if events else "Run aidr-obs tap install",
            }
        )
    cursor_env = set((status.get("cursor") or {}).get("env") or [])
    has_logs = conn.execute("SELECT 1 FROM log_lines LIMIT 1").fetchone() is not None
    debug_set = "NOMA_DEBUG" in cursor_env
    checks.append(
        {
            "id": "debug",
            "ok": debug_set or has_logs,
            "label": "NOMA_DEBUG",
            "detail": "set on the Cursor tap"
            if debug_set
            else ("debug logs are present" if has_logs else "not set, so hook runs are not logged"),
        }
    )
    dry = "NOMA_DRYRUN" in cursor_env
    checks.append(
        {
            "id": "mode",
            "ok": True,
            "label": "Dry-run" if dry else "Live",
            "detail": "Decisions are recorded and not enforced."
            if dry
            else "The hook can enforce what Noma decides. Set NOMA_DRYRUN to record without enforcing.",
        }
    )
    last = conn.execute("SELECT MAX(ts_ms) FROM captures").fetchone()[0]
    age = _age_ms(last)
    checks.append(
        {
            "id": "capture",
            "ok": last is not None,
            "label": "Last capture",
            "detail": "no captures yet" if last is None else "most recent capture",
            "age_ms": age,
        }
    )
    versions = _rows(
        conn.execute(
            "SELECT agent, COALESCE(hook_version, '') AS hook_version, COUNT(*) AS n, MAX(ts) AS last"
            " FROM captures GROUP BY agent, hook_version ORDER BY n DESC"
        )
    )
    return {"checks": checks, "versions": versions, "show": any(not item["ok"] for item in checks)}


def static_asset(kind: str, rel: str) -> Path | None:
    """A file under static/<kind>/ by its relative name, or None.

    Each segment must be a plain filename. The resolved path has to stay inside that
    directory, so ``..`` and encoded traversal never match a file.
    """
    spec = _STATIC_KINDS.get(kind)
    if spec is None or not rel or rel.startswith(("/", "\\")) or "\\" in rel or "\x00" in rel:
        return None
    parts = rel.split("/")
    if not parts or any(not _PART.fullmatch(part) for part in parts):
        return None
    root, suffixes = spec
    candidate = root.joinpath(*parts)
    if candidate.suffix not in suffixes or not candidate.is_file():
        return None
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def vendor_file(name: str) -> Path | None:
    """A vendored script by exact file name, or None."""
    return static_asset("vendor", name)


def _watch_snapshot() -> tuple:
    points = []

    def stat(path: Path) -> tuple:
        try:
            info = path.stat()
        except OSError:
            return (str(path), 0, 0)
        return (str(path), info.st_mtime_ns, info.st_size)

    points.append(stat(paths.capture_path()))
    log_dir = paths.noma_log_dir()
    if log_dir.is_dir():
        files = sorted(log_dir.glob("*.log")) + sorted(log_dir.glob("*.log.*"))
        points.extend(stat(path) for path in files)
    devices = paths.data_dir() / "devices"
    if devices.is_dir():
        for child in sorted(devices.iterdir()):
            if not child.is_dir():
                continue
            points.append(stat(child / "capture.jsonl"))
            noma = child / "noma"
            if noma.is_dir():
                files = sorted(noma.glob("*.log")) + sorted(noma.glob("*.log.*"))
                points.extend(stat(path) for path in files)
    return tuple(points)


ROUTES = {
    "/api/summary": summary,
    "/api/verdicts": verdicts,
    "/api/events": events,
    "/api/event": event_detail,
    "/api/errors": errors,
    "/api/mcp": mcp,
    "/api/destinations": destinations,
    "/api/sensitive": sensitive,
    "/api/sessions": sessions,
    "/api/session": session_detail,
    "/api/usage": usage,
    "/api/graph": entity_graph,
    "/api/actions": actions,
    "/api/search": search,
    "/api/overview": overview,
    "/api/noise": noise_list,
    "/api/setup": setup_status,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "aidr-obs"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self) -> bool:
        return host_allowed(self.headers.get("Host", ""), self.server.server_address[1])

    def _base_headers(self, code: int, ctype: str, csp: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp or CSP)

    def _send(self, code: int, body: bytes, ctype: str, csp: str | None = None, set_cookie: str | None = None) -> None:
        self._base_headers(code, ctype, csp)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, set_cookie: str | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def _session_ok(self) -> bool:
        return session_valid(self.headers.get("Cookie"))

    def _login_page(self, message: str = "", nxt: str = "/", code: int = 200) -> None:
        nonce = secrets.token_urlsafe(16)
        csp = CSP.replace("script-src 'self'", "script-src 'nonce-" + nonce + "'")
        self._send(code, login_html(message, safe_next(nxt), nonce), "text/html; charset=utf-8", csp=csp)

    def _read_form(self, limit: int = 8_000) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length < 0 or length > limit:
            return {}
        raw = self.rfile.read(length) if length else b""
        try:
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except UnicodeError:
            return {}
        return {key: values[0] if values else "" for key, values in parsed.items()}

    def _post_login(self) -> None:
        if not auth_required():
            return self._redirect("/")
        addr = self.client_address[0]
        form = self._read_form()
        nxt = safe_next(form.get("next", "/"))
        if login_blocked(addr):
            return self._login_page("Try again in a minute.", nxt, 429)
        if not password_matches(form.get("password", "")):
            note_login_failure(addr)
            code = 429 if login_blocked(addr) else 401
            message = "Try again in a minute." if code == 429 else "Wrong password."
            return self._login_page(message, nxt, code)
        note_login_success(addr)
        expiry = int(time.time()) + SESSION_SECONDS
        return self._redirect(nxt, cookie_header(session_token(expiry)))

    def _post_logout(self) -> None:
        return self._redirect("/", cookie_header(None))

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _sse(self, event: str, payload: dict) -> None:
        self.wfile.write(f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def _stream(self) -> None:
        self._base_headers(200, "text/event-stream; charset=utf-8")
        self.end_headers()
        try:
            self._sse("hello", {"ok": True})
            last = _watch_snapshot()
            while True:
                time.sleep(2)
                current = _watch_snapshot()
                if current == last:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                last = current
                conn = store.connect()
                try:
                    info = store.ingest(conn)
                except Exception as exc:  # keep the stream up if one ingest fails
                    self._sse("error", {"error": str(exc)})
                    continue
                finally:
                    conn.close()
                self._sse(
                    "ingest",
                    {
                        "added": (info.get("captures_added") or 0) + (info.get("log_lines_added") or 0),
                        "captures": info.get("captures_added") or 0,
                        "log_lines": info.get("log_lines_added") or 0,
                    },
                )
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    def _static(self, kind: str, rel: str) -> None:
        found = static_asset(kind, rel)
        if found is None:
            return self._send(404, b"not found", "text/plain")
        ctype = "text/css; charset=utf-8" if kind == "css" else "text/javascript; charset=utf-8"
        return self._send(200, found.read_bytes(), ctype)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        url = urlsplit(self.path)
        if url.path == "/health":
            return self._json({"ok": True})
        if url.path == "/login":
            if self._session_ok():
                return self._redirect("/")
            return self._login_page()
        if not self._session_ok():
            if url.path.startswith("/api/"):
                return self._json({"error": "sign in required"}, 401)
            if url.path in ("/", "/index.html"):
                return self._login_page()
            return self._send(401, b"sign in required", "text/plain")
        if url.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/stream":
            return self._stream()
        for kind, prefix in (("vendor", "/static/vendor/"), ("js", "/static/js/"), ("css", "/static/css/")):
            if url.path.startswith(prefix):
                return self._static(kind, url.path.removeprefix(prefix))
        route = ROUTES.get(url.path)
        if route is None:
            return self._send(404, b"not found", "text/plain")
        conn = store.connect()
        try:
            self._json(route(conn, parse_qs(url.query)))
        except (ValueError, KeyError) as e:
            self._json({"error": str(e)}, 400)
        finally:
            conn.close()

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        path = urlsplit(self.path).path
        if path == "/login":
            return self._post_login()
        if path == "/logout":
            return self._post_logout()
        if path in ("/api/push", "/api/push/heartbeat"):
            if self.headers.get("X-Aidr-Obs") != "1":
                return self._send(403, b"forbidden", "text/plain")
            return self._post_push(path)
        if not self._session_ok():
            return self._json({"error": "sign in required"}, 401)
        if self.headers.get("X-Aidr-Obs") != "1":
            return self._send(403, b"forbidden", "text/plain")
        if path == "/api/ingest":
            conn = store.connect()
            try:
                self._json(store.ingest(conn))
            finally:
                conn.close()
            return None
        if path == "/api/event/review":
            return self._post_review()
        if path in ("/api/push", "/api/push/heartbeat"):
            return self._post_push(path)
        if path == "/api/noise":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, b"bad length", "text/plain")
            if length < 0 or length > 64_000:
                return self._send(413, b"too large", "text/plain")
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode() or "{}")
                saved = noise.save_rules(body.get("rules"))
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"rules": saved, "source": "user"})
        return self._send(404, b"not found", "text/plain")

    def _post_review(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "bad Content-Length"}, 400)
        if length < 0 or length > 64_000:
            return self._json({"error": "body too large"}, 400)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") if length else b"{}")
        except (UnicodeDecodeError, ValueError):
            return self._json({"error": "body must be JSON"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "body must be a JSON object"}, 400)
        capture_id = body.get("capture_id")
        if not isinstance(capture_id, str) or not capture_id.strip():
            return self._json({"error": "capture_id is required"}, 400)
        force = bool(body.get("force"))
        backend = body.get("backend")
        if backend is not None and backend not in jevreview.BACKENDS:
            return self._json({"error": "backend must be one of " + ", ".join(jevreview.BACKENDS)}, 400)
        conn = store.connect()
        try:
            review = jevreview.review_capture(conn, capture_id.strip(), force=force, backend=backend)
            self._json(review)
        except KeyError as e:
            self._json({"error": str(e)}, 404)
        except jevreview.ReviewError as e:
            self._json({"error": str(e)}, 502)
        finally:
            conn.close()

    def _post_push(self, path: str) -> None:
        try:
            push.check_auth(self.headers.get("Authorization"))
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise push.PushError("bad Content-Length") from exc
            if length < 0 or length > push.MAX_BODY_BYTES:
                raise push.PushError("body too large", 413)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
            except (UnicodeDecodeError, ValueError) as exc:
                raise push.PushError("body must be JSON") from exc
            if not isinstance(body, dict):
                raise push.PushError("body must be a JSON object")
            conn = store.connect()
            try:
                if path == "/api/push/heartbeat":
                    return self._json(push.heartbeat(conn, body))
                out = push.accept(conn, body)
                if out["accepted_bytes"]:
                    out["ingest"] = store.ingest(conn)
                return self._json(out)
            finally:
                conn.close()
        except push.PushError as e:
            return self._json({"error": str(e), **e.extra}, e.status)
        except sqlite3.OperationalError:
            return self._json({"error": "database is busy, retry"}, 503)


def allowed_host_names() -> frozenset[str]:
    """Host names from AIDR_OBS_ALLOWED_HOSTS. A trailing port is stripped; the listening port is what is checked."""
    names = set()
    for raw in (os.environ.get("AIDR_OBS_ALLOWED_HOSTS") or "").split(","):
        name = raw.strip().lower().rstrip(".")
        if not name or "/" in name or " " in name:
            continue
        host, sep, port = name.rpartition(":")
        if sep and port.isdigit():
            name = host
        if name:
            names.add(name)
    return frozenset(names)


def host_allowed(host_header: str, port: int) -> bool:
    """True for loopback, or for a name in AIDR_OBS_ALLOWED_HOSTS, on this server's port."""
    given = (host_header or "").strip().lower()
    port_s = str(port)
    if given in (HOST + ":" + port_s, "localhost:" + port_s):
        return True
    return any(given == name + ":" + port_s for name in allowed_host_names())


def ui_password() -> str:
    return (os.environ.get("AIDR_OBS_UI_PASSWORD") or "").strip()


def auth_required() -> bool:
    return bool(ui_password())


def check_auth_config() -> None:
    """A LAN host allowlist with no password would publish the captures to the network."""
    if allowed_host_names() and not ui_password():
        raise ValueError("AIDR_OBS_ALLOWED_HOSTS is set but AIDR_OBS_UI_PASSWORD is empty")


def password_matches(given: str) -> bool:
    expected = ui_password()
    return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(expected.encode()).digest())


def session_token(expiry: int) -> str:
    raw = str(expiry).encode()
    mac = hmac.new(ui_password().encode(), raw, hashlib.sha256).hexdigest()
    return str(expiry) + "." + mac


def session_valid(cookie_header: str | None) -> bool:
    if not auth_required():
        return True
    jar = SimpleCookie()
    try:
        jar.load(cookie_header or "")
    except CookieError:
        return False
    morsel = jar.get(SESSION_COOKIE)
    if morsel is None or "." not in morsel.value:
        return False
    expiry_s, _, mac = morsel.value.partition(".")
    if not expiry_s.isdigit():
        return False
    if int(expiry_s) < int(time.time()):
        return False
    expected = hmac.new(ui_password().encode(), expiry_s.encode(), hashlib.sha256).hexdigest()
    if len(mac) != len(expected):
        return False
    return hmac.compare_digest(mac, expected)


def cookie_header(token: str | None) -> str:
    if not token:
        return SESSION_COOKIE + "=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
    return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"


def safe_next(raw: str) -> str:
    value = (raw or "").strip()
    if _NEXT.fullmatch(value) and "\\" not in value and not value.startswith("//"):
        return value
    return "/"


def login_blocked(addr: str) -> bool:
    now = time.time()
    with _LOGIN_LOCK:
        count, until = _LOGIN_FAILS.get(addr, (0, 0.0))
        if count >= LOGIN_LOCK_AFTER and now < until:
            return True
        if count >= LOGIN_LOCK_AFTER and now >= until:
            _LOGIN_FAILS.pop(addr, None)
        return False


def note_login_failure(addr: str) -> None:
    now = time.time()
    with _LOGIN_LOCK:
        count, until = _LOGIN_FAILS.get(addr, (0, 0.0))
        if until and now >= until:
            count = 0
        count += 1
        lock_until = now + LOGIN_LOCK_SECONDS if count >= LOGIN_LOCK_AFTER else 0.0
        _LOGIN_FAILS[addr] = (count, lock_until)


def note_login_success(addr: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(addr, None)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def login_html(message: str, nxt: str, nonce: str) -> bytes:
    page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in</title>
<style>
  body { font: 16px/1.4 system-ui, sans-serif; background: #111; color: #eee;
    display: grid; place-items: center; min-height: 100vh; margin: 0; }
  form { width: 18rem; display: grid; gap: 0.75rem; }
  h1 { font-size: 1.1rem; font-weight: 600; margin: 0; }
  input, button { font: inherit; padding: 0.5rem 0.6rem; }
  .err { color: #f88; min-height: 1.2em; margin: 0; }
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>AIDR Hook Observability</h1>
  <p class="err">@@MESSAGE@@</p>
  <label>Password <input type="password" name="password" autofocus required></label>
  <input type="hidden" name="next" id="next" value="@@NEXT@@">
  <button type="submit">Sign in</button>
</form>
<script nonce="@@NONCE@@">document.getElementById("next").value = "/" + location.hash</script>
</body>
</html>
"""
    return (
        page.replace("@@MESSAGE@@", _esc(message)).replace("@@NEXT@@", _esc(nxt)).replace("@@NONCE@@", _esc(nonce))
    ).encode()


def bind_host() -> str:
    """AIDR_OBS_HOST, default 127.0.0.1. 0.0.0.0 only with AIDR_OBS_CONTAINER=1; nothing else."""
    host = (os.environ.get("AIDR_OBS_HOST") or HOST).strip() or HOST
    if host in (HOST, "localhost"):
        return host
    if host == CONTAINER_BIND and os.environ.get("AIDR_OBS_CONTAINER", "").strip() == "1":
        return host
    raise ValueError(
        f"AIDR_OBS_HOST={host!r} is not allowed: use 127.0.0.1 ({CONTAINER_BIND} needs AIDR_OBS_CONTAINER=1)"
    )


def make_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((bind_host(), port), Handler)


def serve(port: int = 8770, ingest_first: bool = True) -> None:
    check_auth_config()
    httpd = make_server(port)
    if ingest_first:
        conn = store.connect()
        print("ingest:", json.dumps(store.ingest(conn)))
        conn.close()
    where = "http://" + HOST + ":" + str(httpd.server_address[1]) + "/"
    if httpd.server_address[0] != HOST:
        where += " (bound to " + httpd.server_address[0] + " in a container)"
    print("aidr-obs serving " + where + " (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
