"""Local dashboard server: static page + read-only JSON API, bound to 127.0.0.1 only.

Requests whose Host header isn't this loopback address are refused (DNS-rebinding
guard), and the one mutating endpoint (POST /api/ingest) also needs a custom header,
which a cross-site form can't send.
"""

import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import graph, insights, logparse, paths, store

STATIC = Path(__file__).with_name("static")
VENDOR = STATIC / "vendor"
HOST = "127.0.0.1"
VERDICT_ACTIONS = ("block", "ask", "mask", "context", "other")


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _pct(values: list[int], p: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]


def _since(q: dict) -> int:
    try:
        return int(q.get("since", ["0"])[0])
    except ValueError:
        return 0


def _bucket_ms(since: int, now: int) -> int:
    span = now - since if since else 30 * 86400_000
    for size in (60_000, 300_000, 900_000, 3_600_000, 21_600_000, 86_400_000):
        if span / size <= 72:
            return size
    return 86_400_000


def summary(conn: sqlite3.Connection, q: dict) -> dict:
    since = _since(q)
    now = int(time.time() * 1000)
    runs = _rows(
        conn.execute(
            "SELECT event, outcome, start_ms, duration_ms, key_source, agent FROM runs WHERE start_ms >= ?", (since,)
        )
    )
    caps = _rows(
        conn.execute("SELECT event, verdict, ts_ms, latency_ms, run_id FROM captures WHERE ts_ms >= ?", (since,))
    )
    if not since:
        first = min([r["start_ms"] for r in runs] + [c["ts_ms"] for c in caps] or [now])
        since_eff = first
    else:
        since_eff = since
    bucket = _bucket_ms(since_eff, now)
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
    return {
        "now": now,
        "since": since_eff,
        "bucket_ms": bucket,
        "series": [{"t": t, "counts": series.get(t, {})} for t in range(start, now + bucket, bucket)],
        "totals": {
            "runs": len(runs),
            "captures": len(caps),
            "errors": conn.execute(
                "SELECT COUNT(*) FROM log_lines WHERE level = 'ERROR' AND ts_ms >= ?", (since,)
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


def verdicts(conn: sqlite3.Connection, q: dict) -> dict:
    since = _since(q)
    marks = ",".join("?" * len(VERDICT_ACTIONS))
    items = _rows(
        conn.execute(
            "SELECT id, ts, ts_ms, event, tool_name, verdict, reason, session_id, run_id FROM captures"
            " WHERE ts_ms >= ? AND verdict IN (" + marks + ") ORDER BY ts_ms DESC LIMIT 500",
            (since, *VERDICT_ACTIONS),
        )
    )
    by_tool = _rows(
        conn.execute(
            "SELECT COALESCE(tool_name, '(' || COALESCE(event, '?') || ')') AS tool, verdict, COUNT(*) AS n"
            " FROM captures WHERE ts_ms >= ? GROUP BY tool, verdict ORDER BY n DESC",
            (since,),
        )
    )
    return {"items": items, "by_tool": by_tool}


def events(conn: sqlite3.Connection, q: dict) -> dict:
    since = _since(q)
    get = lambda k: (q.get(k) or [""])[0]  # noqa: E731
    limit = min(int(get("limit") or 200), 1000)
    offset = int(get("offset") or 0)
    rows = _rows(
        conn.execute(
            """
        SELECT r.run_id, c.id AS capture_id, r.agent, r.start_ms AS ts_ms, r.start_ts AS ts, r.event,
               c.tool_name, r.outcome, c.verdict, c.reason, COALESCE(c.latency_ms, r.duration_ms) AS latency_ms,
               r.error_count, r.pid, r.post_bytes
        FROM runs r LEFT JOIN captures c ON c.run_id = r.run_id
        WHERE r.start_ms >= ?
        UNION ALL
        SELECT NULL, c.id, c.agent, c.ts_ms, c.ts, c.event, c.tool_name,
               CASE WHEN c.dry_run THEN 'dry_run' ELSE NULL END, c.verdict, c.reason, c.latency_ms,
               NULL, c.child_pid, NULL
        FROM captures c WHERE c.run_id IS NULL AND c.ts_ms >= ?
        ORDER BY ts_ms DESC
        """,
            (since, since),
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
    if capture:
        for key in ("stdin_json", "stdout_text"):
            try:
                capture[key] = json.loads(capture[key]) if capture[key] else capture[key]
            except ValueError:
                pass
    return {"run": run, "capture": capture, "lines": lines}


def errors(conn: sqlite3.Connection, q: dict) -> dict:
    since = _since(q)
    groups: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT agent, ts, ts_ms, location, message FROM log_lines WHERE level = 'ERROR' AND ts_ms >= ? ORDER BY ts_ms",
        (since,),
    ):
        sig = logparse.error_signature(r["message"])
        g = groups.setdefault(
            sig, {"signature": sig, "count": 0, "first": r["ts"], "agents": set(), "locations": set()}
        )
        g["count"] += 1
        g["last"] = r["ts"]
        g["example"] = r["message"]
        g["agents"].add(r["agent"])
        g["locations"].add(r["location"])
    error_groups = sorted(
        ({**g, "agents": sorted(g["agents"]), "locations": sorted(g["locations"])} for g in groups.values()),
        key=lambda g: -g["count"],
    )
    tap_errors = _rows(
        conn.execute(
            "SELECT tap_error, COUNT(*) AS n, MAX(ts) AS last FROM captures WHERE tap_error IS NOT NULL AND ts_ms >= ?"
            " GROUP BY tap_error ORDER BY n DESC",
            (since,),
        )
    )
    health = {}
    for col in ("key_source", "installed_config", "url", "sender", "outcome", "hook_file"):
        health[col] = _rows(
            conn.execute(
                "SELECT COALESCE(" + col + ", '?') AS value, COUNT(*) AS n, MAX(start_ts) AS last FROM runs"
                " WHERE start_ms >= ? GROUP BY value ORDER BY n DESC",
                (since,),
            )
        )
    key_daily = _rows(
        conn.execute(
            "SELECT substr(start_ts, 1, 10) AS day, COALESCE(key_source, '?') AS key_source, COUNT(*) AS n FROM runs"
            " WHERE start_ms >= ? GROUP BY day, key_source ORDER BY day",
            (since,),
        )
    )
    retries = conn.execute(
        "SELECT COALESCE(SUM(retries),0), COALESCE(SUM(fallbacks),0) FROM runs WHERE start_ms >= ?", (since,)
    ).fetchone()
    return {
        "groups": error_groups,
        "tap_errors": tap_errors,
        "health": health,
        "key_daily": key_daily,
        "retries": retries[0],
        "fallbacks": retries[1],
    }


def mcp(conn: sqlite3.Connection, q: dict) -> dict:
    since = _since(q)
    snapshots = []
    latest: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT run_id, agent, start_ts, project_root, mcp_artifacts, discovery_json FROM runs"
        " WHERE mcp_artifacts IS NOT NULL AND start_ms >= ? ORDER BY start_ms",
        (since,),
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
            prev = latest.get(key)
            changed = prev is not None and prev["has_servers"] != d["has_servers"]
            latest[key] = {
                **d,
                "agent": r["agent"],
                "last_seen": r["start_ts"],
                "first_seen": prev["first_seen"] if prev else r["start_ts"],
                "changes": (prev["changes"] if prev else 0) + (1 if changed else 0),
            }
    return {
        "snapshots": list(reversed(snapshots))[:500],
        "sources": sorted(latest.values(), key=lambda d: (d["scope"], d["path"])),
    }


def destinations(conn: sqlite3.Connection, q: dict) -> dict:
    return insights.destinations(conn, _since(q))


def sensitive(conn: sqlite3.Connection, q: dict) -> dict:
    return insights.sensitive(conn, _since(q))


def sessions(conn: sqlite3.Connection, q: dict) -> dict:
    return insights.sessions(conn, _since(q))


def session_detail(conn: sqlite3.Connection, q: dict) -> dict:
    return insights.session_detail(conn, (q.get("key") or [""])[0])


def usage(conn: sqlite3.Connection, q: dict) -> dict:
    return insights.usage(conn, _since(q))


def entity_graph(conn: sqlite3.Connection, q: dict) -> dict:
    raw = (q.get("max_nodes") or [str(graph.DEFAULT_MAX_NODES)])[0]
    try:
        max_nodes = int(raw)
    except ValueError as e:
        raise ValueError("max_nodes must be an integer") from e
    focus = (q.get("focus") or [""])[0] or None
    return graph.graph(conn, _since(q), max(1, min(max_nodes, 2000)), focus)


def vendor_file(name: str) -> Path | None:
    """A vendored script by exact file name, or None. The name is matched against the
    directory listing, never joined into a path, so no traversal is possible."""
    if not VENDOR.is_dir():
        return None
    return next((f for f in VENDOR.iterdir() if f.is_file() and f.suffix == ".js" and f.name == name), None)


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
}


class Handler(BaseHTTPRequestHandler):
    server_version = "aidr-obs"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (HOST + ":" + str(port), "localhost:" + str(port))

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';"
            " connect-src 'self'; img-src 'self' data:",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path.startswith("/static/vendor/"):
            found = vendor_file(url.path.removeprefix("/static/vendor/"))
            if found is None:
                return self._send(404, b"not found", "text/plain")
            return self._send(200, found.read_bytes(), "text/javascript; charset=utf-8")
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
        if not self._host_ok() or self.headers.get("X-Aidr-Obs") != "1":
            return self._send(403, b"forbidden", "text/plain")
        if urlsplit(self.path).path != "/api/ingest":
            return self._send(404, b"not found", "text/plain")
        conn = store.connect()
        try:
            self._json(store.ingest(conn))
        finally:
            conn.close()


def make_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((HOST, port), Handler)


def serve(port: int = 8770, ingest_first: bool = True) -> None:
    if ingest_first:
        conn = store.connect()
        print("ingest:", json.dumps(store.ingest(conn)))
        conn.close()
    httpd = make_server(port)
    print("aidr-obs serving http://" + HOST + ":" + str(httpd.server_address[1]) + "/ (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
