import base64
import http.client
import json
import threading
from urllib.parse import urlencode

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


def _req(port, path, method="GET", headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    payload = res.read()
    headers_out = {"status": res.status, "cookie": res.getheader("Set-Cookie"), "location": res.getheader("Location")}
    conn.close()
    return res.status, payload, headers_out


def _form(port, fields, cookie=None):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    return _req(port, "/login", "POST", headers, urlencode(fields).encode())


def _session(set_cookie):
    assert set_cookie and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
    assert "Secure" not in set_cookie
    return set_cookie.split(";", 1)[0]


def test_index_and_api(base):
    status, body, _ = _req(base, "/")
    assert status == 200 and b"AIDR Hook Observability" in body
    status, body, _ = _req(base, "/api/summary?since=0")
    data = json.loads(body)
    assert status == 200 and data["totals"]["runs"] == 5 and data["by_outcome"]["dry_run"] == 2
    status, body, _ = _req(base, "/api/events?since=0&outcome=failed")
    items = json.loads(body)["items"]
    assert len(items) == 1 and items[0]["pid"] == 1003
    status, body, _ = _req(base, "/api/event?run_id=" + items[0]["run_id"])
    assert len(json.loads(body)["lines"]) == 6
    assert json.loads(_req(base, "/api/errors?since=0")[1])["groups"][0]["count"] >= 1
    sources = {s["scope"]: s for s in json.loads(_req(base, "/api/mcp?since=0")[1])["sources"]}
    assert sources["user"]["has_servers"] is True and sources["project"]["has_servers"] is False


def test_foreign_host_is_refused(base):
    assert _req(base, "/api/summary", headers={"Host": "attacker.example:" + str(base)})[0] == 403


def test_allowed_host_serves_the_dashboard(base, monkeypatch):
    monkeypatch.setenv("AIDR_OBS_ALLOWED_HOSTS", "VanishAI.home.lab, other.example:9999")
    host = "vanishai.home.lab:" + str(base)
    status, body, _ = _req(base, "/", headers={"Host": host})
    assert status == 200 and b"AIDR Hook Observability" in body
    assert _req(base, "/api/summary?since=0", headers={"Host": host})[0] == 200
    assert _req(base, "/", headers={"Host": "attacker.example:" + str(base)})[0] == 403
    assert _req(base, "/", headers={"Host": "vanishai.home.lab:9"})[0] == 403


def test_password_off_still_serves(base, monkeypatch):
    monkeypatch.delenv("AIDR_OBS_UI_PASSWORD", raising=False)
    status, body, _ = _req(base, "/")
    assert status == 200 and b"/static/js/main.js" in body
    status, body, _ = _req(base, "/health")
    assert status == 200 and json.loads(body) == {"ok": True}


def test_password_hides_the_dashboard(base, monkeypatch):
    server._LOGIN_FAILS.clear()
    monkeypatch.setenv("AIDR_OBS_UI_PASSWORD", "ui-secret")
    status, body, _ = _req(base, "/")
    assert status == 200 and b'type="password"' in body and b"/static/js/main.js" not in body
    assert _req(base, "/static/js/main.js")[0] == 401
    status, body, _ = _req(base, "/api/summary?since=0")
    assert status == 401 and json.loads(body)["error"] == "sign in required"
    status, body, _ = _req(base, "/health")
    assert status == 200 and json.loads(body) == {"ok": True}


def test_login_sets_a_cookie_and_logout_clears_it(base, monkeypatch):
    server._LOGIN_FAILS.clear()
    monkeypatch.setenv("AIDR_OBS_UI_PASSWORD", "ui-secret")
    status, body, headers = _form(base, {"password": "nope", "next": "/#/activity?range=24h"})
    assert status == 401 and b"Wrong password" in body and headers["cookie"] is None
    status, _, headers = _form(base, {"password": "ui-secret", "next": "/#/activity?range=24h"})
    assert status == 302 and headers["location"] == "/#/activity?range=24h"
    cookie = _session(headers["cookie"])
    status, body, _ = _req(base, "/", headers={"Cookie": cookie})
    assert status == 200 and b"/static/js/main.js" in body
    status, body, _ = _req(base, "/api/summary?since=0", headers={"Cookie": cookie})
    assert status == 200 and json.loads(body)["totals"]["runs"] == 5
    status, _, headers = _req(base, "/logout", "POST", {"Cookie": cookie})
    assert status == 302 and "Max-Age=0" in headers["cookie"]
    status, body, _ = _req(base, "/")
    assert b'type="password"' in body


def test_bad_next_falls_back_to_root(base, monkeypatch):
    server._LOGIN_FAILS.clear()
    monkeypatch.setenv("AIDR_OBS_UI_PASSWORD", "ui-secret")
    status, _, headers = _form(base, {"password": "ui-secret", "next": "https://evil.example"})
    assert status == 302 and headers["location"] == "/"


def test_login_locks_after_repeated_failures(base, monkeypatch):
    server._LOGIN_FAILS.clear()
    monkeypatch.setenv("AIDR_OBS_UI_PASSWORD", "ui-secret")
    for _ in range(server.LOGIN_LOCK_AFTER - 1):
        assert _form(base, {"password": "nope"})[0] == 401
    status, body, _ = _form(base, {"password": "nope"})
    assert status == 429 and b"minute" in body
    assert _form(base, {"password": "ui-secret"})[0] == 429
    server._LOGIN_FAILS.clear()


def test_push_does_not_need_the_ui_cookie(base, monkeypatch):
    monkeypatch.setenv("AIDR_OBS_UI_PASSWORD", "ui-secret")
    monkeypatch.setenv("AIDR_OBS_PUSH_TOKEN", "push-secret")
    raw = b'{"id":"p1","ts":"2026-06-29T14:23:01.050","agent":"claude-code","event":"UserPromptSubmit"}\n'
    payload = json.dumps(
        {"stream": "capture.jsonl", "file_key": "k", "offset": 0, "data_b64": base64.b64encode(raw).decode()}
    ).encode()
    status, body, _ = _req(
        base,
        "/api/push",
        "POST",
        {"X-Aidr-Obs": "1", "Authorization": "Bearer push-secret", "Content-Type": "application/json"},
        payload,
    )
    assert status == 200 and json.loads(body)["accepted_bytes"] == len(raw)


def test_lan_hosts_require_a_password(monkeypatch):
    monkeypatch.setenv("AIDR_OBS_ALLOWED_HOSTS", "vanishai.home.lab")
    monkeypatch.delenv("AIDR_OBS_UI_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="AIDR_OBS_UI_PASSWORD"):
        server.check_auth_config()
    monkeypatch.setenv("AIDR_OBS_UI_PASSWORD", "ui-secret")
    server.check_auth_config()


def test_ingest_needs_custom_header(base):
    assert _req(base, "/api/ingest", "POST")[0] == 403
    status, body, _ = _req(base, "/api/ingest", "POST", {"X-Aidr-Obs": "1"})
    assert status == 200 and json.loads(body)["runs"] == 5


def test_binds_loopback_only(base):
    assert server.HOST == "127.0.0.1"


def test_vendor_scripts_served_without_traversal(base):
    status, body, _ = _req(base, "/static/vendor/cytoscape.min.js")
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
    status, body, _ = _req(base, "/api/graph?since=0")
    data = json.loads(body)
    assert status == 200 and set(data) >= {"nodes", "edges", "by_type", "truncated"}
    assert _req(base, "/api/graph?max_nodes=x")[0] == 400
