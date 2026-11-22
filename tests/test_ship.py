import json
import os
import plistlib
import threading

import pytest

from aidr_obs import push, server, ship, store

TOKEN = "test-ship-token"


def _line(cid: str) -> str:
    rec = {"id": cid, "ts": "2026-06-29T14:23:01.050", "agent": "claude-code", "event": "UserPromptSubmit"}
    return json.dumps(rec) + "\n"


@pytest.fixture
def mac(tmp_path):
    """The Mac's files, kept apart from the server's spool (AIDR_OBS_HOME / AIDR_OBS_LOG_DIR)."""
    dirs = {"data": tmp_path / "mac-data", "logs": tmp_path / "mac-noma"}
    for d in dirs.values():
        d.mkdir()
    return dirs


@pytest.fixture
def cfg(env, monkeypatch):
    monkeypatch.setenv("AIDR_OBS_PUSH_TOKEN", TOKEN)
    monkeypatch.delenv("AIDR_OBS_REMOTE", raising=False)
    store.connect().close()
    httpd = server.make_server(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "token": TOKEN, "device": "test-mac"}
    httpd.shutdown()
    httpd.server_close()


def _append(path, text):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def _captures():
    return store.connect().execute("SELECT COUNT(*) FROM captures").fetchone()[0]


def _spool(env, *parts):
    return env["home"].joinpath("devices", "test-mac", *parts)


def test_ships_appended_lines_once(cfg, mac, env):
    cap = mac["data"] / "capture.jsonl"
    _append(cap, _line("a") + _line("b"))
    _append(cap, '{"id": "partial')
    state = {}
    sent = ship.ship_once(cfg, state, mac["data"], mac["logs"])
    assert sent["capture.jsonl"] == len(_line("a") + _line("b")) and _captures() == 2
    assert ship.ship_once(cfg, state, mac["data"], mac["logs"])["capture.jsonl"] == 0
    _append(cap, '"}\n' + _line("c"))
    ship.ship_once(cfg, state, mac["data"], mac["logs"])
    assert _spool(env, "capture.jsonl").read_bytes() == cap.read_bytes()
    assert _captures() == 4


def test_ships_noma_logs_but_not_rotated_names(cfg, mac, env):
    log = mac["logs"] / "cursor-guardrails-debug.log"
    _append(log, "2026-06-29 14:23:01,050 DEBUG one\n")
    _append(mac["logs"] / "cursor-guardrails-debug.log.1", "old\n")
    _append(mac["logs"] / "notes.txt", "private\n")
    ship.ship_once(cfg, {}, mac["data"], mac["logs"])
    noma = _spool(env, "noma")
    assert sorted(p.name for p in noma.iterdir()) == ["cursor-guardrails-debug.log"]
    assert (noma / "cursor-guardrails-debug.log").read_bytes() == log.read_bytes()
    assert list(env["logs"].iterdir()) == []


def test_rotation_finishes_old_file_first(cfg, mac, env):
    cap = mac["data"] / "capture.jsonl"
    _append(cap, _line("a"))
    state = {}
    ship.ship_once(cfg, state, mac["data"], mac["logs"])
    _append(cap, _line("b"))
    os.replace(cap, cap.with_name("capture.jsonl.1"))
    _append(cap, _line("c"))
    ship.ship_once(cfg, state, mac["data"], mac["logs"])
    ids = [json.loads(x)["id"] for x in _spool(env, "capture.jsonl").read_text().splitlines()]
    assert ids == ["a", "b", "c"] and _captures() == 3


def test_from_end_skips_existing_lines(cfg, mac):
    cap = mac["data"] / "capture.jsonl"
    _append(cap, _line("old1") + _line("old2"))
    state = ship.baseline({}, mac["data"], mac["logs"])
    _append(cap, _line("new"))
    ship.ship_once(cfg, state, mac["data"], mac["logs"])
    ids = [r[0] for r in store.connect().execute("SELECT id FROM captures")]
    assert ids == ["new"]


def test_lost_state_resyncs_from_409(cfg, mac, env):
    cap = mac["data"] / "capture.jsonl"
    _append(cap, _line("a") + _line("b"))
    state = {}
    ship.ship_once(cfg, state, mac["data"], mac["logs"])
    state["capture.jsonl"]["offset"] = 0
    _append(cap, _line("c"))
    ship.ship_once(cfg, state, mac["data"], mac["logs"])
    assert _spool(env, "capture.jsonl").read_bytes() == cap.read_bytes() and _captures() == 3


def test_state_file_is_private(cfg, mac, env):
    _append(mac["data"] / "capture.jsonl", _line("a"))
    ship.ship_once(cfg, {}, mac["data"], mac["logs"])
    assert oct(ship.state_path().stat().st_mode & 0o777) == "0o600"
    assert ship.load_state()["capture.jsonl"]["offset"] == len(_line("a"))


def test_heartbeat_reaches_server(cfg, env):
    ship.send_heartbeat(cfg)
    beat = push.latest_heartbeat(store.connect())
    assert beat["device"] == "test-mac" and "claude-code" in beat["status"]


def test_wrong_token_is_an_error(cfg, mac):
    _append(mac["data"] / "capture.jsonl", _line("a"))
    with pytest.raises(ship.ShipError, match="HTTP 401"):
        ship.ship_once({**cfg, "token": "wrong"}, {}, mac["data"], mac["logs"])


def test_unreachable_server_is_an_error(mac):
    _append(mac["data"] / "capture.jsonl", _line("a"))
    cfg = {"url": "http://127.0.0.1:9", "token": TOKEN, "device": "m"}
    with pytest.raises(ship.ShipError, match="tunnel"):
        ship.ship_once(cfg, {}, mac["data"], mac["logs"])


def test_config_requires_token_and_loopback_http(env, monkeypatch):
    for var in ("AIDR_OBS_PUSH_TOKEN", "AIDR_OBS_PUSH_URL", "AIDR_OBS_SHIP_DEVICE"):
        monkeypatch.setenv(var, "x")
        monkeypatch.delenv(var)
    with pytest.raises(ship.ShipError, match="AIDR_OBS_PUSH_TOKEN"):
        ship.load_config()
    env["home"].mkdir(parents=True, exist_ok=True)
    ship.env_path().write_text("AIDR_OBS_PUSH_TOKEN='from-file'\nAIDR_OBS_SHIP_DEVICE=mac-9\n", encoding="utf-8")
    cfg = ship.load_config()
    assert cfg["url"] == ship.DEFAULT_URL and cfg["token"] == "from-file" and cfg["device"] == "mac-9"
    monkeypatch.setenv("AIDR_OBS_SHIP_DEVICE", "Other Mac")
    assert ship.load_config()["device"] == "Other-Mac"
    monkeypatch.setenv("AIDR_OBS_PUSH_URL", "http://vanishai.example:8770")
    with pytest.raises(ship.ShipError, match="loopback"):
        ship.load_config()
    monkeypatch.setenv("AIDR_OBS_PUSH_URL", "https://vanishai.example")
    assert ship.load_config()["url"] == "https://vanishai.example"


def test_plist_runs_shipper_from_this_interpreter(env):
    plist = plistlib.loads(ship.plist_bytes())
    assert plist["Label"] == ship.LABEL and plist["KeepAlive"] is True
    assert plist["ProgramArguments"][1:] == ["-m", "aidr_obs.cli", "ship", "run"]
    assert plist["StandardOutPath"] == str(ship.log_path())
