import http.client
import json
import threading

import pytest

from aidr_obs import server, store


@pytest.fixture
def base(fixture_log):
    store.ingest(store.connect())
    httpd = server.make_server(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _req(port, path, method="GET", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers=headers or {})
    res = conn.getresponse()
    body = res.read()
    conn.close()
    return res.status, body


def test_index_and_api(base):
    status, body = _req(base, "/")
    assert status == 200 and b"AIDR Hook Observability" in body
    status, body = _req(base, "/api/summary?since=0")
    data = json.loads(body)
    assert status == 200 and data["totals"]["runs"] == 5 and data["by_outcome"]["dry_run"] == 2
    status, body = _req(base, "/api/events?since=0&outcome=failed")
    items = json.loads(body)["items"]
    assert len(items) == 1 and items[0]["pid"] == 1003
    status, body = _req(base, "/api/event?run_id=" + items[0]["run_id"])
    assert len(json.loads(body)["lines"]) == 6
    assert json.loads(_req(base, "/api/errors?since=0")[1])["groups"][0]["count"] >= 1
    sources = {s["scope"]: s for s in json.loads(_req(base, "/api/mcp?since=0")[1])["sources"]}
    assert sources["user"]["has_servers"] is True and sources["project"]["has_servers"] is False


def test_foreign_host_is_refused(base):
    assert _req(base, "/api/summary", headers={"Host": "attacker.example:" + str(base)})[0] == 403


def test_ingest_needs_custom_header(base):
    assert _req(base, "/api/ingest", "POST")[0] == 403
    status, body = _req(base, "/api/ingest", "POST", {"X-Aidr-Obs": "1"})
    assert status == 200 and json.loads(body)["runs"] == 5


def test_binds_loopback_only(base):
    assert server.HOST == "127.0.0.1"


def test_vendor_scripts_served_without_traversal(base):
    status, body = _req(base, "/static/vendor/cytoscape.min.js")
    assert status == 200 and b"Cytoscape Consortium" in body[:400]
    for path in (
        "/static/vendor/README.md",
        "/static/vendor/../index.html",
        "/static/vendor/%2e%2e/server.py",
        "/static/vendor/nope.js",
        "/static/index.html",
    ):
        assert _req(base, path)[0] == 404, path


def test_graph_api(base):
    status, body = _req(base, "/api/graph?since=0")
    data = json.loads(body)
    assert status == 200 and set(data) >= {"nodes", "edges", "by_type", "truncated"}
    assert _req(base, "/api/graph?max_nodes=x")[0] == 400
