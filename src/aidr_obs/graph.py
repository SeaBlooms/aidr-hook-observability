"""Entity graph over agent actions: who did what, with which tool, reaching where.

Nodes are aggregated entities, not individual hook events:

    agent ─works in→ project ─has session→ session ─called→ tool ─reached→ destination / MCP server
                                                                  └─touched (read/edit/shell)→ sensitive

A session with no known project hangs directly off its agent. Every node and edge carries
an action count, the per-verdict breakdown and its worst verdict, so a path that ended in a
block or ask stands out. Tool nodes are shared across sessions, so destination and
sensitive nodes also record which sessions reached them.

Built from insights.load_actions(), so it inherits the same caveats: redacted, possibly
truncated payloads, and destinations parsed from what the agent asked for, not network traffic.
"""

import sqlite3

from .insights import SEVERITY, Action, _access, _session_key, _summary, destinations_of, load_actions, sensitive_hits

DEFAULT_MAX_NODES = 300
_MAX_EXAMPLES = 5
_MAX_SESSIONS = 20


def worst(verdicts: dict) -> str:
    """The most severe verdict in a {verdict: count} map ("allow" when empty)."""
    return max(verdicts, key=lambda v: SEVERITY.get(v, 2), default="allow")


class _Builder:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}

    def node(self, node_id: str, ntype: str, label: str, a: Action, **attrs) -> str:
        n = self.nodes.get(node_id)
        if n is None:
            n = self.nodes[node_id] = {
                "id": node_id,
                "type": ntype,
                "label": label,
                "count": 0,
                "verdicts": {},
                "first": a.ts,
                "first_ms": a.ts_ms,
                "examples": [],
                "sessions": [],
                **attrs,
            }
        n["count"] += 1
        n["verdicts"][a.verdict] = n["verdicts"].get(a.verdict, 0) + 1
        n["last"], n["last_ms"] = a.ts, a.ts_ms
        if a.kind == "tool" and len(n["examples"]) < _MAX_EXAMPLES:
            n["examples"].append(
                {
                    "ts": a.ts,
                    "tool": a.tool,
                    "verdict": a.verdict,
                    "summary": _summary(a)[:300],
                    "capture_id": a.capture_ids[0] if a.capture_ids else None,
                }
            )
        key = _session_key(a)
        if ntype in ("destination", "mcp", "sensitive", "tool") and key not in n["sessions"]:
            if len(n["sessions"]) < _MAX_SESSIONS:
                n["sessions"].append(key)
        return node_id

    def edge(self, source: str, target: str, rel: str, a: Action) -> None:
        key = (source, target, rel)
        e = self.edges.get(key)
        if e is None:
            e = self.edges[key] = {
                "id": source + " " + rel + " " + target,
                "source": source,
                "target": target,
                "rel": rel,
                "count": 0,
                "verdicts": {},
                "first": a.ts,
            }
        e["count"] += 1
        e["verdicts"][a.verdict] = e["verdicts"].get(a.verdict, 0) + 1
        e["last"] = a.ts


def _short(session: str | None) -> str:
    if not session:
        return "(no session id)"
    return session[:8] + "…" if len(session) > 12 else session


def _project_label(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _plan(a: Action) -> tuple[list[tuple], list[tuple]]:
    """The nodes (id, type, label, attrs) and edges (source, target, rel) one action touches."""
    key = _session_key(a)
    agent, session = "agent:" + a.agent, "session:" + key
    nodes = [
        (agent, "agent", a.agent, {}),
        (session, "session", _short(a.session), {"agent": a.agent, "key": key}),
    ]
    edges = []
    if a.project:
        project = "project:" + a.project
        nodes.append((project, "project", _project_label(a.project), {"path": a.project}))
        edges += [(agent, project, "works in"), (project, session, "has session")]
    else:
        edges.append((agent, session, "has session"))
    if a.kind != "tool":
        return nodes, edges
    tool = "tool:" + (a.tool or "?")
    nodes.append((tool, "tool", a.tool or "?", {}))
    edges.append((session, tool, "called"))
    for d in destinations_of(a):
        if d["kind"] == "mcp":
            target = "mcp:" + d["target"]
            nodes.append((target, "mcp", d["target"], {"class": "mcp"}))
        else:
            target = "dest:" + d["target"]
            nodes.append((target, "destination", d["target"], {"class": d["class"], "via": d["via"]}))
        edges.append((tool, target, "reached"))
    for label in sensitive_hits(a):
        target = "sensitive:" + label
        nodes.append((target, "sensitive", label, {}))
        edges.append((tool, target, _access(a)))
    return nodes, edges


def build(actions: list[Action]) -> _Builder:
    b = _Builder()
    for a in actions:
        nodes, edges = _plan(a)
        seen = set()
        for node_id, ntype, label, attrs in nodes:
            if node_id not in seen:  # an action reaching one host twice still counts once
                seen.add(node_id)
                b.node(node_id, ntype, label, a, **attrs)
        for source, target, rel in dict.fromkeys(edges):
            b.edge(source, target, rel, a)
    return b


def _finish(item: dict) -> dict:
    item["worst"] = worst(item["verdicts"])
    return item


def graph(
    conn: sqlite3.Connection,
    since: int,
    max_nodes: int = DEFAULT_MAX_NODES,
    focus: str | None = None,
    until: int | None = None,
) -> dict:
    """The entity graph for actions since `since` (ms), capped at `max_nodes`.

    With `focus` (a node id such as "dest:github.com"), only the entities and links of actions
    that involve that node are returned: the sessions, tools and agents that led to a host,
    not everything else that shares a tool with them. Counts stay global for the time range,
    so a node reads the same in every view; `in_focus` holds how many of its actions involve
    the focus node.

    When capping, the focus node and agent nodes are always kept, then nodes rank by worst
    verdict and then by action count, so blocked or flagged entities survive before busy but
    benign ones. Edges whose endpoints were dropped are dropped too.
    """
    actions = load_actions(conn, since, until)
    full = build(actions)
    focus_info = None
    if focus:
        involved = [a for a in actions if any(n[0] == focus for n in _plan(a)[0])]
        sub = build(involved)
        nodes = [{**full.nodes[i], "in_focus": n["count"]} for i, n in sub.nodes.items()]
        edges = [{**full.edges[k], "in_focus": e["count"]} for k, e in sub.edges.items()]
        focus_info = {"id": focus, "found": focus in full.nodes, "actions": len(involved)}
        if focus in full.nodes:
            focus_info.update(type=full.nodes[focus]["type"], label=full.nodes[focus]["label"])
    else:
        nodes, edges = list(full.nodes.values()), list(full.edges.values())
    total = len(nodes)
    if total > max_nodes:
        nodes.sort(
            key=lambda n: (
                n["id"] != focus,
                n["type"] != "agent",
                -SEVERITY.get(worst(n["verdicts"]), 2),
                -n["count"],
                n["id"],
            )
        )
        nodes = nodes[:max_nodes]
    kept = {n["id"] for n in nodes}
    edges = [e for e in edges if e["source"] in kept and e["target"] in kept]
    by_type: dict[str, int] = {}
    for n in nodes:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    return {
        "nodes": [_finish(n) for n in nodes],
        "edges": [_finish(e) for e in edges],
        "by_type": by_type,
        "total_nodes": total,
        "truncated": total > len(nodes),
        "focus": focus_info,
    }
