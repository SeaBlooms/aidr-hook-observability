import base64
import http.client
import json
import threading
import time

import pytest

from aidr_obs import push, server, store

TOKEN = "test-push-token"


def _line(cid: str) -> bytes:
    rec = {"id": cid, "ts": "2026-06-29T14:23:01.050", "agent": "claude-code", "event": "UserPromptSubmit"}
    return (json.dumps(rec) + "\n").encode()


@pytest.fixture
def dash(env, monkeypatch):
    monkeypatch.setenv("AIDR_OBS_PUSH_TOKEN", TOKEN)
    for var in ("AIDR_OBS_REMOTE", "AIDR_OBS_HOST", "AIDR_OBS_CONTAINER"):
        monkeypatch.delenv(var, raising=False)
    store.connect().close()
    httpd = server.make_server(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _post(port, path, body, token=TOKEN, headers=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    h = {"X-Aidr-Obs": "1", "Content-Type": "application/json"}
    if token is not None:
        h["Authorization"] = "Bearer " + token
    h.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=raw, headers=h)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    try:
        return res.status, json.loads(data)
    except ValueError:
        return res.status, {"raw": data.decode(errors="replace")}


def _chunk(stream, key, offset, raw):
    return {"stream": stream, "file_key": key, "offset": offset, "data_b64": base64.b64encode(raw).decode()}


def _captures():
    return store.connect().execute("SELECT COUNT(*) FROM captures").fetchone()[0]


# --- bind ----------------------------------------------------------------------------------------


def test_bind_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("AIDR_OBS_HOST", raising=False)
    assert server.bind_host() == "127.0.0.1"


def test_bind_all_interfaces_needs_container_opt_in(monkeypatch):
    monkeypatch.setenv("AIDR_OBS_HOST", "0.0.0.0")
    monkeypatch.delenv("AIDR_OBS_CONTAINER", raising=False)
    with pytest.raises(ValueError, match="AIDR_OBS_CONTAINER"):
        server.bind_host()
    monkeypatch.setenv("AIDR_OBS_CONTAINER", "1")
    assert server.bind_host() == "0.0.0.0"


def test_container_flag_does_not_open_other_hosts(monkeypatch):
    monkeypatch.setenv("AIDR_OBS_CONTAINER", "1")
    for host in ("192.168.1.10", "example.com", "::"):
        monkeypatch.setenv("AIDR_OBS_HOST", host)
        with pytest.raises(ValueError):
            server.bind_host()


# --- push ----------------------------------------------------------------------------------------


def test_push_needs_token_and_header(dash, monkeypatch):
    body = _chunk("capture.jsonl", "k", 0, _line("a"))
    assert _post(dash, "/api/push", body, token=None)[0] == 401
    assert _post(dash, "/api/push", body, token="wrong")[0] == 401
    assert _post(dash, "/api/push", body, headers={"X-Aidr-Obs": "0"})[0] == 403
    assert _post(dash, "/api/push", body, headers={"Host": "attacker.example:" + str(dash)})[0] == 403
    monkeypatch.delenv("AIDR_OBS_PUSH_TOKEN")
    status, data = _post(dash, "/api/push", body)
    assert status == 403 and "AIDR_OBS_PUSH_TOKEN" in data["error"]
    assert _captures() == 0


def test_push_spools_and_ingests(dash, env):
    first = _line("a") + _line("b")
    status, data = _post(dash, "/api/push", _chunk("capture.jsonl", "k1", 0, first))
    assert status == 200 and data["offset"] == len(first) and data["accepted_bytes"] == len(first)
    assert _captures() == 2
    status, data = _post(dash, "/api/push", _chunk("capture.jsonl", "k1", len(first), _line("c")))
    assert status == 200 and _captures() == 3
    spool = env["home"] / "capture.jsonl"
    assert spool.read_bytes() == first + _line("c")
    assert oct(spool.stat().st_mode & 0o777) == "0o600"


def test_retry_with_old_offset_is_409_not_duplicate(dash, env):
    raw = _line("a")
    assert _post(dash, "/api/push", _chunk("capture.jsonl", "k1", 0, raw))[0] == 200
    status, data = _post(dash, "/api/push", _chunk("capture.jsonl", "k1", 0, raw))
    assert status == 409 and data["expected"] == len(raw)
    status, data = _post(dash, "/api/push", _chunk("capture.jsonl", "k1", 999, _line("z")))
    assert status == 409 and data["expected"] == len(raw)
    assert (env["home"] / "capture.jsonl").read_bytes() == raw and _captures() == 1


def test_new_file_key_starts_a_new_baseline(dash):
    assert _post(dash, "/api/push", _chunk("capture.jsonl", "k1", 0, _line("a")))[0] == 200
    status, data = _post(dash, "/api/push", _chunk("capture.jsonl", "k2", 0, _line("b")))
    assert status == 200 and data["offset"] == len(_line("b")) and _captures() == 2


def test_noma_log_stream_lands_in_log_dir(dash, env):
    raw = b"2026-06-29 14:23:01,050 DEBUG hello\n"
    status, _ = _post(dash, "/api/push", _chunk("cursor-guardrails-debug.log", "L", 0, raw))
    assert status == 200 and (env["logs"] / "cursor-guardrails-debug.log").read_bytes() == raw


@pytest.mark.parametrize(
    "stream", ["../capture.jsonl", "aidr.db", "cursor-guardrails-debug.log.1", "x/claude-guardrails-debug.log", ""]
)
def test_bad_stream_refused(dash, stream):
    status, _ = _post(dash, "/api/push", _chunk(stream, "k", 0, _line("a")))
    assert status == 400


def test_partial_line_and_bad_body_refused(dash):
    assert _post(dash, "/api/push", _chunk("capture.jsonl", "k", 0, b'{"id": "half'))[0] == 400
    assert (
        _post(dash, "/api/push", {"stream": "capture.jsonl", "file_key": "k", "offset": 0, "data_b64": "@@"})[0] == 400
    )
    assert (
        _post(dash, "/api/push", {"stream": "capture.jsonl", "file_key": "k", "offset": -1, "data_b64": ""})[0] == 400
    )
    assert _post(dash, "/api/push", b"not json")[0] == 400
    assert _post(dash, "/api/push", b"[1]")[0] == 400
    assert _captures() == 0


def test_oversized_body_refused(dash, monkeypatch):
    monkeypatch.setattr(push, "MAX_BODY_BYTES", 100)
    status, _ = _post(dash, "/api/push", _chunk("capture.jsonl", "k", 0, _line("a") * 5))
    assert status == 413


def test_spool_rotates(env, monkeypatch):
    monkeypatch.setattr(push, "SPOOL_ROTATE_BYTES", 120)
    conn = store.connect()
    a, b = _line("a"), _line("b")
    push.accept(conn, _chunk("capture.jsonl", "k", 0, a))
    push.accept(conn, _chunk("capture.jsonl", "k", len(a), b))
    assert (env["home"] / "capture.jsonl.1").read_bytes() == a
    assert (env["home"] / "capture.jsonl").read_bytes() == b
    store.ingest(conn)
    assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 2


# --- heartbeat and remote mode ------------------------------------------------------------------


def _checks(conn):
    return {c["id"]: c for c in server.setup_status(conn, {})["checks"]}


def test_heartbeat_feeds_setup_in_remote_mode(dash, monkeypatch):
    monkeypatch.setenv("AIDR_OBS_REMOTE", "1")
    conn = store.connect()
    checks = _checks(conn)
    assert checks["shipper"]["ok"] is False and checks["tap-claude-code"]["ok"] is False
    status = {"claude-code": {"tap_events": ["PreToolUse", "PostToolUse"]}, "cursor": {"env": ["NOMA_DEBUG"]}}
    code, data = _post(dash, "/api/push/heartbeat", {"device": "mac-1", "status": status})
    assert code == 200 and data["device"] == "mac-1"
    checks = _checks(conn)
    assert checks["shipper"]["ok"] is True and "mac-1" in checks["shipper"]["detail"]
    assert checks["tap-claude-code"]["ok"] is True and checks["tap-cursor"]["ok"] is False
    assert checks["debug"]["ok"] is True


def test_stale_heartbeat_reported(env, monkeypatch):
    monkeypatch.setenv("AIDR_OBS_REMOTE", "1")
    conn = store.connect()
    push.heartbeat(conn, {"device": "mac-1", "status": {}})
    with conn:
        conn.execute("UPDATE push_heartbeat SET received_ms = ?", (int(time.time() * 1000) - 600_000,))
    shipper = _checks(conn)["shipper"]
    assert shipper["ok"] is False and "no recent heartbeat" in shipper["detail"]


def test_local_mode_ignores_heartbeat(env, monkeypatch):
    monkeypatch.delenv("AIDR_OBS_REMOTE", raising=False)
    conn = store.connect()
    push.heartbeat(conn, {"device": "mac-1", "status": {"claude-code": {"tap_events": ["PreToolUse"]}}})
    checks = _checks(conn)
    assert "shipper" not in checks and checks["tap-claude-code"]["ok"] is False


def test_heartbeat_validation(dash):
    assert _post(dash, "/api/push/heartbeat", {"device": "", "status": {}})[0] == 400
    assert _post(dash, "/api/push/heartbeat", {"device": "m", "status": []})[0] == 400
    assert _post(dash, "/api/push/heartbeat", {"device": "m", "status": {"x": "y" * 70_000}})[0] == 413
    assert _post(dash, "/api/push/heartbeat", {"device": "m", "status": {}}, token="nope")[0] == 401
