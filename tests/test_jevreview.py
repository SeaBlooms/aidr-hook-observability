"""Jev second-opinion client and /api/event/review (fake loopback Jev, no live gateway)."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aidr_obs import jevreview, server, store


def _capture(env, **fields):
    rec = {
        "id": fields.pop("id", "cap1"),
        "ts": "2026-06-29T14:23:01.050",
        "ts_ms": fields.pop("ts_ms", int(time.time() * 1000)),
        "agent": "claude-code",
        "event": "PreToolUse",
        "tool_name": "Bash",
        "latency_ms": 40,
        "stdout": json.dumps({"decision": "block", "reason": "reads ~/.ssh"}),
        "stdin": {"tool_name": "Bash", "tool_input": {"command": "cat ~/.ssh/id_rsa"}},
        **fields,
    }
    env["home"].mkdir(parents=True, exist_ok=True)
    with open(env["home"] / "capture.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


class _FakeJev(BaseHTTPRequestHandler):
    hits = 0
    reply_text = json.dumps({"stance": "agree", "note": "Blocking the SSH key read is correct."})
    status = 200
    last_body = None
    bodies: list = []
    answer_backend = "echo"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        type(self).last_body = json.loads(raw.decode()) if raw else None
        type(self).bodies.append(type(self).last_body)
        type(self).hits += 1
        auth = self.headers.get("Authorization") or ""
        if auth != "Bearer test-token":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"detail":"Invalid token"}')
            return
        if self.path != "/v1/complete":
            self.send_response(404)
            self.end_headers()
            return
        requested = (type(self).last_body or {}).get("backend")
        answer = type(self).answer_backend
        body = json.dumps(
            {
                "text": type(self).reply_text,
                "backend": (requested or "gateway") if answer == "echo" else answer,
                "model": "fake/model",
                "persona_sha256": "abc123persona",
                "case_id": (type(self).last_body or {}).get("case_id"),
            }
        ).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_jev(monkeypatch):
    _FakeJev.hits = 0
    _FakeJev.reply_text = json.dumps({"stance": "agree", "note": "Blocking the SSH key read is correct."})
    _FakeJev.status = 200
    _FakeJev.last_body = None
    _FakeJev.bodies = []
    _FakeJev.answer_backend = "echo"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeJev)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    monkeypatch.setenv("JEV_API_TOKEN", "test-token")
    monkeypatch.setenv("JEV_BASE_URL", f"http://127.0.0.1:{port}")
    yield {"port": port, "handler": _FakeJev}
    httpd.shutdown()
    httpd.server_close()


def test_parse_stances_and_fence():
    assert jevreview.parse_reply('{"stance":"agree","note":"ok"}')["stance"] == "agree"
    assert jevreview.parse_reply('{"stance":"would_block","note":"stop"}')["stance"] == "would_block"
    assert jevreview.parse_reply('{"stance":"would_allow","note":"fine"}')["stance"] == "would_allow"
    fenced = '```json\n{"stance":"agree","note":"fenced ok"}\n```'
    assert jevreview.parse_reply(fenced)["note"] == "fenced ok"


def test_parse_rejects_bad_shape():
    with pytest.raises(jevreview.ReviewError, match="not valid JSON"):
        jevreview.parse_reply("not json")
    with pytest.raises(jevreview.ReviewError, match="stance"):
        jevreview.parse_reply('{"stance":"maybe","note":"x"}')
    with pytest.raises(jevreview.ReviewError, match="note"):
        jevreview.parse_reply('{"stance":"agree","note":""}')


def test_non_loopback_base_refused(monkeypatch):
    with pytest.raises(jevreview.ReviewError, match="loopback"):
        jevreview.assert_loopback_base("http://example.com:8790")
    monkeypatch.setenv("JEV_API_TOKEN", "t")
    monkeypatch.setenv("JEV_BASE_URL", "http://8.8.8.8:8790")
    with pytest.raises(jevreview.ReviewError, match="loopback"):
        jevreview.ask_jev({"verdict": "block", "reason": "x"})


def test_jev_hosts_allowlist_names_only(monkeypatch):
    monkeypatch.delenv("AIDR_OBS_JEV_HOSTS", raising=False)
    with pytest.raises(jevreview.ReviewError, match="AIDR_OBS_JEV_HOSTS"):
        jevreview.assert_loopback_base("http://jev:8790")
    monkeypatch.setenv("AIDR_OBS_JEV_HOSTS", " Jev , 10.0.0.5,")
    assert jevreview.assert_loopback_base("http://jev:8790") == "http://jev:8790"
    with pytest.raises(jevreview.ReviewError):
        jevreview.assert_loopback_base("http://10.0.0.5:8790")
    with pytest.raises(jevreview.ReviewError):
        jevreview.assert_loopback_base("http://other:8790")


def test_load_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "JEV_API_TOKEN='from-file'\n"
        'export JEV_BASE_URL="http://127.0.0.1:9999"\n'
        "AIDR_OBS_HOME=/should/not/load\n"
        "not a pair\n",
        encoding="utf-8",
    )
    for key in ("JEV_API_TOKEN", "JEV_BASE_URL", "AIDR_OBS_HOME"):
        monkeypatch.setenv(key, "")  # registers the key so teardown restores it
        monkeypatch.delenv(key)
    assert sorted(jevreview.load_env_file(env_file)) == ["JEV_API_TOKEN", "JEV_BASE_URL"]
    cfg = jevreview.config()
    assert cfg["token"] == "from-file" and cfg["base_url"] == "http://127.0.0.1:9999"
    assert "AIDR_OBS_HOME" not in os.environ


def test_load_env_file_keeps_exported_and_missing_ok(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("JEV_API_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("JEV_API_TOKEN", "from-shell")
    assert jevreview.load_env_file(env_file) == []
    assert jevreview.config()["token"] == "from-shell"
    assert jevreview.load_env_file(tmp_path / "missing.env") == []


def test_ask_and_cache(env, fake_jev):
    _capture(env)
    conn = store.connect()
    store.ingest(conn)
    first = jevreview.review_capture(conn, "cap1")
    assert first["stance"] == "agree" and first["cached"] is False
    assert first["model"] == "fake/model"
    assert fake_jev["handler"].hits == 1
    assert fake_jev["handler"].last_body["case_id"] == "verdict-review"

    second = jevreview.review_capture(conn, "cap1")
    assert second["cached"] is True and fake_jev["handler"].hits == 1

    fake_jev["handler"].reply_text = json.dumps({"stance": "would_allow", "note": "overly strict"})
    third = jevreview.review_capture(conn, "cap1", force=True)
    assert third["stance"] == "would_allow" and third["cached"] is False
    assert fake_jev["handler"].hits == 2


def test_default_backend_omits_field_and_stores_answer(env, fake_jev):
    _capture(env)
    conn = store.connect()
    store.ingest(conn)
    review = jevreview.review_capture(conn, "cap1")
    assert "backend" not in fake_jev["handler"].last_body
    assert review["backend"] == "gateway"


def test_reviews_cached_per_backend(env, fake_jev):
    _capture(env)
    conn = store.connect()
    store.ingest(conn)
    gw = jevreview.review_capture(conn, "cap1", backend="gateway")
    assert gw["backend"] == "gateway" and fake_jev["handler"].last_body["backend"] == "gateway"

    fake_jev["handler"].reply_text = json.dumps({"stance": "would_allow", "note": "local model disagrees"})
    local = jevreview.review_capture(conn, "cap1", backend="ollama")
    assert local["backend"] == "ollama" and local["cached"] is False
    assert fake_jev["handler"].hits == 2

    again = jevreview.review_capture(conn, "cap1", backend="gateway")
    assert again["cached"] is True and again["stance"] == "agree"
    assert fake_jev["handler"].hits == 2
    assert [(r["backend"], r["stance"]) for r in jevreview.list_reviews(conn, "cap1")] == [
        ("gateway", "agree"),
        ("ollama", "would_allow"),
    ]


def test_backend_mismatch_rejected(env, fake_jev):
    _capture(env)
    conn = store.connect()
    store.ingest(conn)
    fake_jev["handler"].answer_backend = None
    with pytest.raises(jevreview.ReviewError, match="backend selection"):
        jevreview.review_capture(conn, "cap1", backend="ollama")
    assert jevreview.list_reviews(conn, "cap1") == []


def test_unknown_backend_refused(env):
    with pytest.raises(jevreview.ReviewError, match="backend must be"):
        jevreview.ask_jev({"verdict": "block"}, backend="cloud")


def test_migrates_pre_backend_reviews(env):
    import sqlite3

    from aidr_obs import paths

    db = paths.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(db)
    old.executescript(
        "CREATE TABLE jev_reviews (capture_id TEXT PRIMARY KEY, stance TEXT NOT NULL, note TEXT NOT NULL,"
        " model TEXT, persona_sha256 TEXT, asked_at INTEGER NOT NULL);"
        "CREATE INDEX jev_reviews_asked ON jev_reviews(asked_at);"
        "INSERT INTO jev_reviews VALUES ('cap1', 'agree', 'old note', 'anthropic/x', 'p', 1);"
    )
    old.commit()
    old.close()

    conn = store.connect()
    assert jevreview.list_reviews(conn, "cap1") == [
        {
            "capture_id": "cap1",
            "backend": "gateway",
            "stance": "agree",
            "note": "old note",
            "model": "anthropic/x",
            "persona_sha256": "p",
            "asked_at": 1,
        }
    ]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "jev_reviews_v1" not in tables
    conn.close()
    assert len(jevreview.list_reviews(store.connect(), "cap1")) == 1


def test_bad_reply_not_cached(env, fake_jev):
    _capture(env)
    conn = store.connect()
    store.ingest(conn)
    fake_jev["handler"].reply_text = "sorry, not json"
    with pytest.raises(jevreview.ReviewError, match="not valid JSON"):
        jevreview.review_capture(conn, "cap1")
    assert jevreview.get_review(conn, "cap1") is None


def test_prune_removes_review(env, fake_jev):
    now = int(time.time() * 1000)
    _capture(env, id="old", ts_ms=now - 10 * 86400_000)
    _capture(env, id="new", ts_ms=now)
    conn = store.connect()
    store.ingest(conn)
    jevreview.review_capture(conn, "old")
    jevreview.review_capture(conn, "new")
    out = store.prune(conn, days=3)
    assert out["captures_deleted"] == 1
    assert out["jev_reviews_deleted"] == 1
    assert [r[0] for r in conn.execute("SELECT capture_id FROM jev_reviews")] == ["new"]


def _post_review(port, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    raw = json.dumps(body).encode()
    conn.request(
        "POST",
        "/api/event/review",
        body=raw,
        headers={"Content-Type": "application/json", "X-Aidr-Obs": "1", "Content-Length": str(len(raw))},
    )
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, json.loads(data)


@pytest.fixture
def dash(env, fake_jev):
    _capture(env)
    store.ingest(store.connect())
    httpd = server.make_server(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_api_review_and_event_detail(dash, fake_jev):
    status, data = _post_review(dash, {"capture_id": "cap1"})
    assert status == 200 and data["stance"] == "agree" and data["cached"] is False
    assert fake_jev["handler"].hits == 1

    status, data = _post_review(dash, {"capture_id": "cap1"})
    assert status == 200 and data["cached"] is True and fake_jev["handler"].hits == 1

    conn = http.client.HTTPConnection("127.0.0.1", dash, timeout=5)
    conn.request("GET", "/api/event?capture_id=cap1")
    res = conn.getresponse()
    detail = json.loads(res.read())
    conn.close()
    assert res.status == 200
    assert detail["jev_review"]["stance"] == "agree"
    assert detail["jev"]["configured"] is True

    conn = http.client.HTTPConnection("127.0.0.1", dash, timeout=5)
    conn.request("GET", "/api/verdicts?since=0")
    res = conn.getresponse()
    verdicts = json.loads(res.read())
    conn.close()
    match = next(i for i in verdicts["items"] if i["id"] == "cap1")
    assert match["jev_stance"] == "agree"
    assert match["jev_stances"] == {"gateway": "agree"}


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    res = conn.getresponse()
    data = json.loads(res.read())
    conn.close()
    return res.status, data


def test_api_both_backends_one_verdict_row(dash, fake_jev):
    assert _post_review(dash, {"capture_id": "cap1", "backend": "gateway"})[0] == 200
    fake_jev["handler"].reply_text = json.dumps({"stance": "would_allow", "note": "too strict"})
    status, data = _post_review(dash, {"capture_id": "cap1", "backend": "ollama"})
    assert status == 200 and data["backend"] == "ollama"

    _, detail = _get(dash, "/api/event?capture_id=cap1")
    assert [r["backend"] for r in detail["jev_reviews"]] == ["gateway", "ollama"]
    assert detail["jev"]["backends"] == ["gateway", "ollama"]

    _, verdicts = _get(dash, "/api/verdicts?since=0")
    rows = [i for i in verdicts["items"] if i["id"] == "cap1"]
    assert len(rows) == 1
    assert rows[0]["jev_stances"] == {"gateway": "agree", "ollama": "would_allow"}


def test_api_rejects_unknown_backend(dash, fake_jev):
    status, data = _post_review(dash, {"capture_id": "cap1", "backend": "cloud"})
    assert status == 400 and "backend" in data["error"]
    assert fake_jev["handler"].hits == 0
