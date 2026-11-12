import json
import os
import time

from aidr_obs import store

NEW_RUN = (
    "2026-06-29 16:00:00,000 INFO pid=2001 [hook.py:102] event PreToolUse; no inventory\n"
    "2026-06-29 16:00:00,001 INFO pid=2001 [transport.py:88] DRY-RUN would POST to https://api.noma.security/claude/v1/hooks (10 bytes)\n"  # noqa: E501
)


def _count(conn, table):
    return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def test_ingest_is_idempotent(fixture_log):
    conn = store.connect()
    first = store.ingest(conn)
    assert first["log_lines_added"] == 28 and first["runs"] == 5
    again = store.ingest(conn)
    assert again["log_lines_added"] == 0 and again["runs"] == 5
    assert _count(conn, "log_lines") == 28


def test_db_is_private(fixture_log, env):
    store.connect()
    assert oct(os.stat(env["home"] / "aidr.db").st_mode & 0o777) == "0o600"
    assert oct(os.stat(env["home"]).st_mode & 0o777) == "0o700"


def test_incremental_append_reads_only_new_lines(fixture_log):
    conn = store.connect()
    store.ingest(conn)
    with open(fixture_log, "a") as f:
        f.write(NEW_RUN)
        f.write("2026-06-29 16:00:01,000 INFO pid=2002 [hook.py:76] partial line without newline")
    out = store.ingest(conn)
    assert out["log_lines_added"] == 2 and out["runs"] == 6
    with open(fixture_log, "a") as f:
        f.write("\n")
    assert store.ingest(conn)["log_lines_added"] == 1


def test_rotation_does_not_duplicate(fixture_log):
    conn = store.connect()
    store.ingest(conn)
    os.replace(fixture_log, fixture_log.with_name(fixture_log.name + ".1"))
    fixture_log.write_text(NEW_RUN)
    out = store.ingest(conn)
    assert out["log_lines_added"] == 2
    assert _count(conn, "log_lines") == 30


def test_rebuilt_state_rereads_without_duplicates(fixture_log):
    conn = store.connect()
    store.ingest(conn)
    conn.execute("DELETE FROM ingest_state")
    conn.commit()
    assert store.ingest(conn)["log_lines_added"] == 0


def _capture(env, **fields):
    rec = {
        "id": fields.pop("id", os.urandom(8).hex()),
        "ts": "2026-06-29T14:23:01.050",
        "agent": "claude-code",
        "event": "UserPromptSubmit",
        "latency_ms": 400,
        "stdout": "",
        **fields,
    }
    env["home"].mkdir(parents=True, exist_ok=True)
    with open(env["home"] / "capture.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def test_captures_ingest_and_link_to_run(fixture_log, env):
    conn = store.connect()
    store.ingest(conn)
    run_start = conn.execute("SELECT start_ms FROM runs WHERE pid = 1001").fetchone()[0]
    _capture(
        env, id="c1", ts_ms=run_start - 50, child_pid=1001, stdout=json.dumps({"decision": "block", "reason": "PII"})
    )
    _capture(env, id="c2", ts_ms=run_start, child_pid=9999)
    out = store.ingest(conn)
    assert out["captures_added"] == 2
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT id, run_id, verdict, reason FROM captures")}
    assert rows["c1"]["run_id"] == "claude-code:1001:" + str(run_start)
    assert rows["c1"]["verdict"] == "block" and rows["c1"]["reason"] == "PII"
    assert rows["c2"]["run_id"] is None
    assert store.ingest(conn)["captures_added"] == 0


def test_prune_drops_old_captures_only_from_our_files(fixture_log, env):
    conn = store.connect()
    now = int(time.time() * 1000)
    _capture(env, id="old", ts_ms=now - 10 * 86400_000)
    _capture(env, id="new", ts_ms=now)
    store.ingest(conn)
    before = fixture_log.read_bytes()
    out = store.prune(conn, days=3)
    assert out["captures_deleted"] == 1 and out["capture_records_dropped"] == 1
    assert [r[0] for r in conn.execute("SELECT id FROM captures")] == ["new"]
    assert [json.loads(x)["id"] for x in (env["home"] / "capture.jsonl").read_text().splitlines()] == ["new"]
    assert oct(os.stat(env["home"] / "capture.jsonl").st_mode & 0o777) == "0o600"
    assert fixture_log.read_bytes() == before
