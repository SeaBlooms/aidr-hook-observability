import json

import pytest

from aidr_obs import insights, server, store

BASE_MS = 1_800_000_000_000


@pytest.fixture
def conn(env):
    c = store.connect()
    yield c
    c.close()


def add(conn, n, agent, event, stdin, verdict="dry_run", tool_name=None, latency=50):
    ms = BASE_MS + n * 1000
    conn.execute(
        "INSERT INTO captures (id, ts, ts_ms, agent, event, tool_name, session_id, latency_ms, verdict, stdin_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            f"c{n}",
            f"2027-01-15T08:00:{n:02d}.000",
            ms,
            agent,
            event,
            tool_name,
            stdin.get("session_id") or stdin.get("conversation_id"),
            latency,
            verdict,
            json.dumps(stdin),
        ),
    )
    conn.commit()


def cursor_ev(event, **kw):
    return {
        "hook_event_name": event,
        "conversation_id": "conv-1",
        "generation_id": "gen-1",
        "workspace_roots": ["/work/demo-proj"],
        "model": "fake-model",
        "cursor_version": "9.9",
        **kw,
    }


def seed(conn):
    add(conn, 1, "cursor", "beforeSubmitPrompt", cursor_ev("beforeSubmitPrompt", prompt="fetch the status page"))
    shell = {
        "tool_use_id": "t1",
        "tool_name": "Shell",
        "tool_input": {"command": "curl -s https://status.example.com/x"},
    }
    add(conn, 2, "cursor", "preToolUse", cursor_ev("preToolUse", **shell))
    add(
        conn,
        3,
        "cursor",
        "beforeShellExecution",
        cursor_ev(
            "beforeShellExecution", tool_use_id="t1", command="curl -s https://status.example.com/x", sandbox=False
        ),
        verdict="ask",
    )
    add(conn, 4, "cursor", "postToolUse", cursor_ev("postToolUse", duration=120, **shell))
    write = {"tool_use_id": "t2", "tool_name": "Write", "tool_input": {"file_path": "/work/demo-proj/.env"}}
    add(conn, 5, "cursor", "preToolUse", cursor_ev("preToolUse", **write))
    add(conn, 6, "cursor", "afterFileEdit", cursor_ev("afterFileEdit", file_path="/work/demo-proj/.env"))
    add(
        conn,
        7,
        "cursor",
        "preToolUse",
        cursor_ev(
            "preToolUse",
            tool_use_id="t3",
            tool_name="MCP:lookup",
            tool_input=json.dumps({"q": "x"}),
            mcp_server_name="fake-crm",
        ),
    )
    add(
        conn,
        8,
        "cursor",
        "afterAgentResponse",
        cursor_ev("afterAgentResponse", text="done", input_tokens=1000, output_tokens=200),
    )
    add(
        conn,
        9,
        "claude-code",
        "PreToolUse",
        {
            "session_id": "s-claude",
            "cwd": "/work/other",
            "tool_use_id": "u1",
            "tool_name": "Bash",
            "permission_mode": "bypassPermissions",
            "tool_input": {"command": "ssh deploy@10.0.0.5 uptime"},
        },
        tool_name="Bash",
    )
    add(
        conn,
        10,
        "claude-code",
        "PostToolUse",
        {
            "session_id": "s-claude",
            "cwd": "/work/other",
            "tool_use_id": "u1",
            "tool_name": "Bash",
            "duration_ms": 40,
            "tool_input": {"command": "ssh deploy@10.0.0.5 uptime"},
        },
        tool_name="Bash",
    )


def test_actions_group_by_tool_use_and_merge_file_edits(conn):
    seed(conn)
    actions = insights.load_actions(conn)
    tools = [a for a in actions if a.kind == "tool"]
    assert [a.tool for a in tools] == ["Shell", "Write", "lookup", "Bash"]
    shell = tools[0]
    assert shell.events == ["preToolUse", "beforeShellExecution", "postToolUse"]
    assert shell.verdict == "ask" and shell.sandbox is False and shell.duration_ms == 120
    assert tools[1].events == ["preToolUse", "afterFileEdit"]
    assert tools[2].input == {"q": "x"} and tools[2].mcp_server == "fake-crm"
    assert tools[3].duration_ms == 40 and tools[3].project == "/work/other"


@pytest.mark.parametrize(
    ("text", "hosts"),
    [
        ("curl -s https://status.example.com/x", [("status.example.com", "url")]),
        ("ssh deploy@10.0.0.5 uptime", [("10.0.0.5", "ssh")]),
        ("git clone git@github.com:someone/repo.git", [("github.com", "ssh")]),
        ("wget -q -O out example.org", [("example.org", "command")]),
        ("cat notes.md && python run.py", []),
        ("curl http://127.0.0.1:8770/api", [("127.0.0.1", "url")]),
        # Wrapping punctuation from Markdown, JSON and shell text is not part of the host.
        ("see [Cytoscape.js](https://js.cytoscape.org) docs", [("js.cytoscape.org", "url")]),
        ("(https://example.com)", [("example.com", "url")]),
        ("<https://example.com>", [("example.com", "url")]),
        ("links: https://a.example.com, https://b.example.com;", [("a.example.com", "url"), ("b.example.com", "url")]),
        ('{\\"url\\": \\"https://api.example.com\\"}', [("api.example.com", "url")]),
        ("curl https://example.com&&echo ok", [("example.com", "url")]),
        ("curl http://[::1]:8080/x", [("[::1]", "url")]),
        ("curl https://user@login.example.com/x", [("login.example.com", "url")]),
    ],
)
def test_hosts_in_text(text, hosts):
    assert insights.hosts_in_text(text) == hosts


def test_host_class():
    assert insights.host_class("127.0.0.1") == "loopback"
    assert insights.host_class("localhost") == "loopback"
    assert insights.host_class("10.0.0.5") == "private-ip"
    assert insights.host_class("8.8.8.8") == "public-ip"
    assert insights.host_class("example.com") == "domain"


def test_destinations(conn):
    seed(conn)
    d = insights.destinations(conn, 0)
    targets = {(g["kind"], g["target"]): g for g in d["items"]}
    assert set(targets) == {("network", "status.example.com"), ("network", "10.0.0.5"), ("mcp", "fake-crm")}
    assert d["items"][0]["target"] == "status.example.com"  # most severe verdict (ask) sorts first
    assert targets[("network", "10.0.0.5")]["class"] == "private-ip"
    assert d["by_class"] == {"domain": 1, "private-ip": 1, "mcp": 1}


def test_sensitive(conn):
    seed(conn)
    s = insights.sensitive(conn, 0)
    assert [(h["tool"], h["access"], h["labels"]) for h in s["hits"]] == [("Write", "edit", ["env file"])]
    assert s["unsandboxed_count"] == 1 and "status.example.com" in s["unsandboxed"][0]["summary"]
    assert {"agent": "claude-code", "mode": "bypassPermissions", "events": 1} in s["permission_modes"]


def test_sensitive_patterns():
    a = insights.Action("cursor", None, None, "", 0, "Shell", "tool", command="cat ~/.ssh/id_ed25519; printenv")
    assert insights.sensitive_hits(a) == ["ssh key or config", "environment dump"]
    assert (
        insights.sensitive_hits(
            insights.Action("x", None, None, "", 0, "Read", "tool", file_path="/work/app/src/main.py")
        )
        == []
    )


def test_sessions_and_timeline(conn):
    seed(conn)
    items = {s["key"]: s for s in insights.sessions(conn, 0)["items"]}
    cur = items["cursor|conv-1"]
    assert (cur["prompts"], cur["tool_calls"], cur["responses"]) == (1, 3, 1)
    assert cur["sensitive"] == 1 and cur["destinations"] == 2 and cur["projects"] == ["/work/demo-proj"]
    assert items["claude-code|s-claude"]["tool_calls"] == 1

    detail = insights.session_detail(conn, "cursor|conv-1")
    [turn] = detail["turns"]
    assert turn["prompt"]["text"] == "fetch the status page"
    assert [t["tool"] for t in turn["tools"]] == ["Shell", "Write", "lookup"]
    assert turn["response"]["text"] == "done" and turn["tokens"]["output_tokens"] == 200


def test_usage(conn):
    seed(conn)
    add(
        conn,
        11,
        "cursor",
        "afterAgentResponse",
        cursor_ev("afterAgentResponse", generation_id="gen-2", input_tokens="***REDACTED***"),
    )
    u = insights.usage(conn, 0)
    assert u["tokens_by_model"] == [
        {
            "model": "fake-model",
            "responses": 1,
            "input_tokens": 1000.0,
            "output_tokens": 200.0,
            "cache_read_tokens": 0.0,
            "cache_write_tokens": 0.0,
        }
    ]
    assert u["redacted_token_responses"] == 1
    assert u["token_series"] == [{"t": "2027-01-15T08", "input_tokens": 1000.0, "output_tokens": 200.0}]
    perf = {(t["agent"], t["tool"]): t for t in u["tool_perf"]}
    assert perf[("cursor", "Shell")]["p50"] == 120 and perf[("claude-code", "Bash")]["total"] == 40
    projects = {p["name"]: p for p in u["projects"]}
    assert projects["demo-proj"]["tool_calls"] == 3 and projects["demo-proj"]["flagged"] == 1
    assert projects["demo-proj"]["sensitive"] == 1
    assert {h["event"] for h in u["hook_perf"] if h["agent"] == "cursor"} >= {"preToolUse", "afterAgentResponse"}


def test_insight_routes_are_json_serializable(conn):
    seed(conn)
    for path in ("/api/destinations", "/api/sensitive", "/api/sessions", "/api/usage"):
        json.dumps(server.ROUTES[path](conn, {"since": ["0"]}))
    json.dumps(server.ROUTES["/api/session"](conn, {"key": ["cursor|conv-1"]}))
