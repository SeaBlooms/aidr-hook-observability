import json

import pytest

from aidr_obs import graph, server, store
from tests.test_insights import seed


@pytest.fixture
def conn(env):
    c = store.connect()
    yield c
    c.close()


def _index(g):
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = {(e["source"], e["rel"], e["target"]): e for e in g["edges"]}
    return nodes, edges


def test_graph_entities_and_relations(conn):
    seed(conn)
    g = graph.graph(conn, 0)
    nodes, edges = _index(g)
    assert set(nodes) == {
        "agent:cursor",
        "agent:claude-code",
        "project:/work/demo-proj",
        "project:/work/other",
        "session:cursor|conv-1",
        "session:claude-code|s-claude",
        "tool:Shell",
        "tool:Write",
        "tool:lookup",
        "tool:Bash",
        "dest:status.example.com",
        "dest:10.0.0.5",
        "mcp:fake-crm",
        "sensitive:env file",
    }
    assert set(edges) == {
        ("agent:cursor", "works in", "project:/work/demo-proj"),
        ("project:/work/demo-proj", "has session", "session:cursor|conv-1"),
        ("agent:claude-code", "works in", "project:/work/other"),
        ("project:/work/other", "has session", "session:claude-code|s-claude"),
        ("session:cursor|conv-1", "called", "tool:Shell"),
        ("session:cursor|conv-1", "called", "tool:Write"),
        ("session:cursor|conv-1", "called", "tool:lookup"),
        ("session:claude-code|s-claude", "called", "tool:Bash"),
        ("tool:Shell", "reached", "dest:status.example.com"),
        ("tool:Bash", "reached", "dest:10.0.0.5"),
        ("tool:lookup", "reached", "mcp:fake-crm"),
        ("tool:Write", "edit", "sensitive:env file"),
    }
    assert g["by_type"] == {
        "agent": 2,
        "project": 2,
        "session": 2,
        "tool": 4,
        "destination": 2,
        "mcp": 1,
        "sensitive": 1,
    }
    assert g["truncated"] is False and g["total_nodes"] == 14


def test_graph_counts_and_worst_verdict(conn):
    seed(conn)
    nodes, edges = _index(graph.graph(conn, 0))
    # The shell action's three hook events are one action, and its worst event verdict was ask.
    shell_edge = edges[("tool:Shell", "reached", "dest:status.example.com")]
    assert shell_edge["count"] == 1 and shell_edge["worst"] == "ask"
    assert nodes["dest:status.example.com"]["class"] == "domain"
    assert nodes["dest:10.0.0.5"]["class"] == "private-ip"
    # The cursor session holds the prompt, three tool calls and the response.
    session = nodes["session:cursor|conv-1"]
    assert session["count"] == 5 and session["worst"] == "ask" and session["key"] == "cursor|conv-1"
    assert nodes["sensitive:env file"]["sessions"] == ["cursor|conv-1"]
    assert nodes["sensitive:env file"]["examples"][0]["summary"] == "/work/demo-proj/.env"
    assert edges[("session:claude-code|s-claude", "called", "tool:Bash")]["worst"] == "dry_run"


def test_graph_truncation_keeps_agents_and_severe_nodes(conn):
    seed(conn)
    g = graph.graph(conn, 0, max_nodes=4)
    ids = [n["id"] for n in g["nodes"]]
    assert g["truncated"] is True and g["total_nodes"] == 14 and len(ids) == 4
    assert {"agent:cursor", "agent:claude-code"} <= set(ids)
    assert all(n["worst"] == "ask" for n in g["nodes"] if n["type"] != "agent")
    kept = set(ids)
    assert all(e["source"] in kept and e["target"] in kept for e in g["edges"])


def test_graph_since_filters_and_empty(conn):
    assert graph.graph(conn, 0) == {
        "nodes": [],
        "edges": [],
        "by_type": {},
        "total_nodes": 0,
        "truncated": False,
        "focus": None,
    }
    seed(conn)
    assert graph.graph(conn, 10**15)["nodes"] == []


def test_graph_route(conn):
    seed(conn)
    data = server.ROUTES["/api/graph"](conn, {"since": ["0"], "max_nodes": ["3"]})
    json.dumps(data)
    assert len(data["nodes"]) == 3
    with pytest.raises(ValueError):
        server.ROUTES["/api/graph"](conn, {"max_nodes": ["lots"]})


def test_worst():
    assert graph.worst({}) == "allow"
    assert graph.worst({"allow": 9, "mask": 1, "ask": 1}) == "ask"


def test_graph_focus_keeps_only_actions_involving_the_node(conn):
    seed(conn)
    g = graph.graph(conn, 0, focus="dest:status.example.com")
    nodes, edges = _index(g)
    assert set(nodes) == {
        "agent:cursor",
        "project:/work/demo-proj",
        "session:cursor|conv-1",
        "tool:Shell",
        "dest:status.example.com",
    }
    assert set(edges) == {
        ("agent:cursor", "works in", "project:/work/demo-proj"),
        ("project:/work/demo-proj", "has session", "session:cursor|conv-1"),
        ("session:cursor|conv-1", "called", "tool:Shell"),
        ("tool:Shell", "reached", "dest:status.example.com"),
    }
    # Counts stay global; in_focus says how many of them involve the focus node.
    session = nodes["session:cursor|conv-1"]
    assert session["count"] == 5 and session["in_focus"] == 1
    assert g["focus"] == {
        "id": "dest:status.example.com",
        "found": True,
        "actions": 1,
        "type": "destination",
        "label": "status.example.com",
    }


def test_graph_focus_on_session_and_missing_node(conn):
    seed(conn)
    nodes, _ = _index(graph.graph(conn, 0, focus="session:claude-code|s-claude"))
    assert set(nodes) == {
        "agent:claude-code",
        "project:/work/other",
        "session:claude-code|s-claude",
        "tool:Bash",
        "dest:10.0.0.5",
    }
    g = graph.graph(conn, 0, focus="dest:nowhere.invalid")
    assert g["nodes"] == [] and g["focus"] == {"id": "dest:nowhere.invalid", "found": False, "actions": 0}


def test_graph_focus_node_survives_cap(conn):
    seed(conn)
    g = graph.graph(conn, 0, max_nodes=1, focus="dest:10.0.0.5")
    assert [n["id"] for n in g["nodes"]] == ["dest:10.0.0.5"] and g["truncated"] is True


def test_graph_route_focus(conn):
    seed(conn)
    data = server.ROUTES["/api/graph"](conn, {"since": ["0"], "focus": ["mcp:fake-crm"]})
    assert data["focus"]["found"] is True and {n["id"] for n in data["nodes"]} >= {"mcp:fake-crm", "tool:lookup"}
