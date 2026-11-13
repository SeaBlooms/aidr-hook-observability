"""Phase 2: actions, search, attention, noise list, and the module UI."""

import http.client
import json
import threading

import pytest

from aidr_obs import insights, noise, server, store
from tests.test_insights import BASE_MS, seed


@pytest.fixture
def conn(env):
    connection = store.connect()
    yield connection
    connection.close()


@pytest.fixture
def base(fixture_log):
    store.ingest(store.connect())
    httpd = server.make_server(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_remote_settings_is_known_noise():
    message = (
        "read_json open/read /home/alice/.claude/remote-settings.json: FileNotFoundError: "
        "[Errno 2] No such file or directory: '/home/alice/.claude/remote-settings.json'"
    )
    rule = noise.match_rule(message)
    assert rule is not None and rule["id"] == "remote-settings-missing"
    other = "read_json open/read /tmp/other.json: FileNotFoundError: missing"
    assert noise.match_rule(other) is None


def test_noise_file_replaces_the_defaults(env):
    saved = noise.save_rules([{"id": "custom", "pattern": "boom", "contains": "nope", "explanation": "ignored"}])
    assert saved[0]["id"] == "custom"
    assert noise.load_rules()[0]["explanation"] == "ignored"
    assert noise.using_defaults() is False


def test_actions_are_filtered_and_paged(conn):
    seed(conn)
    flagged = insights.list_actions(conn, 0, None, {"tag": ["flagged"]})
    assert flagged["total"] == 1
    assert flagged["items"][0]["action_type"] == "Shell"
    assert flagged["items"][0]["unsandboxed"] is True
    sensitive = insights.list_actions(conn, 0, None, {"tag": ["sensitive"]})
    assert sensitive["total"] == 1 and sensitive["items"][0]["action_type"] == "File edit"
    page = insights.list_actions(conn, 0, None, {"limit": ["1"], "offset": ["0"]})
    assert page["total"] > 1 and len(page["items"]) == 1


def test_search_groups_session_host_and_file(conn):
    seed(conn)
    hosts = {group["type"] for group in insights.search(conn, 0, None, "status.example")["groups"]}
    assert "destination" in hosts
    sessions = insights.search(conn, 0, None, "conv-1")["groups"]
    assert any(group["type"] == "session" and group["items"][0]["id"] == "cursor|conv-1" for group in sessions)
    files = insights.search(conn, 0, None, ".env")["groups"]
    assert any(group["type"] == "file" for group in files)
    assert insights.search(conn, 0, None, "x")["groups"] == []


def test_attention_leads_with_what_needs_a_look(conn):
    seed(conn)
    body = insights.attention(conn, 0, None, BASE_MS + 100_000, visit_ms=BASE_MS + 4_000)
    assert body["flagged"] >= 1
    assert body["sensitive"] >= 1
    assert body["unsandboxed"] >= 1
    assert body["bypass"] >= 1
    assert body["destinations"]["domain"] >= 1
    assert body["destinations"]["private_ip"] >= 1
    assert body["sensitive_since_visit"] >= 1
    assert body["feed"][0]["ts_ms"] >= body["feed"][-1]["ts_ms"]
    assert {item["action_type"] for item in body["feed"]} >= {"Shell", "Prompt", "Response"}


def test_attention_chart_covers_actions_the_clock_has_not_reached(conn):
    seed(conn)
    early = insights.attention(conn, 0, None, BASE_MS - 86_400_000)
    counted = sum(sum(bucket["counts"].values()) for bucket in early["series"])
    assert counted > 0
    far = insights.attention(conn, 0, None, BASE_MS + 400 * 86_400_000)
    assert any(bucket["t"] <= BASE_MS + 1000 and bucket["counts"] for bucket in far["series"])


def test_until_drops_later_actions(conn):
    seed(conn)
    early = insights.list_actions(conn, 0, BASE_MS + 2_500, {})
    everything = insights.list_actions(conn, 0, None, {})
    assert 0 < early["total"] < everything["total"]


def test_routes_and_static_modules(base):
    status, body, headers = _req(base, "/")
    assert status == 200 and b"AIDR Hook Observability" in body
    policy = headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy and "script-src 'self' 'unsafe-inline'" not in policy
    status, script, _ = _req(base, "/static/js/main.js")
    assert status == 200 and b"boot" in script
    status, css, _ = _req(base, "/static/css/app.css")
    assert status == 200 and b"--accent" in css
    for path in (
        "/static/js/../index.html",
        "/static/css/../index.html",
        "/static/js/%2e%2e/main.js",
        "/static/vendor/README.md",
    ):
        assert _req(base, path)[0] == 404, path
    overview = json.loads(_req(base, "/api/overview?since=0")[1])
    assert overview["health"]["level"] == "red"
    assert overview["health"]["noise"] >= 1
    actions = json.loads(_req(base, "/api/actions?since=0")[1])
    assert actions["total"] == 0 and "facets" in actions
    search = json.loads(_req(base, "/api/search?q=ab&since=0")[1])
    assert search["groups"] == []
    groups = json.loads(_req(base, "/api/errors?since=0")[1])["groups"]
    assert any(group["noise"] and "remote-settings" in group["example"] for group in groups)


def test_stream_says_hello(base):
    conn = http.client.HTTPConnection("127.0.0.1", base, timeout=5)
    conn.request("GET", "/api/stream")
    response = conn.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type").startswith("text/event-stream")
    chunk = b""
    while b"\n\n" not in chunk and len(chunk) < 2000:
        piece = response.read(1)
        if not piece:
            break
        chunk += piece
    conn.close()
    assert b"event: hello" in chunk


def test_noise_post_needs_the_header_and_saves(base):
    assert _req(base, "/api/noise", "POST", body=b"{}")[0] == 403
    payload = json.dumps({"rules": [{"id": "custom", "pattern": "boom", "explanation": "test"}]}).encode()
    status, body, _ = _req(base, "/api/noise", "POST", {"X-Aidr-Obs": "1"}, payload)
    assert status == 200 and json.loads(body)["source"] == "user"
    listed = json.loads(_req(base, "/api/noise")[1])
    assert listed["rules"][0]["id"] == "custom"


def _req(port, path, method="GET", headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    payload = response.read()
    info = {key: value for key, value in response.getheaders()}
    conn.close()
    return response.status, payload, info
