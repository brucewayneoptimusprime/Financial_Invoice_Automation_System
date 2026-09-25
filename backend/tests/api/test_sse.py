"""The live run stream (M4 stage 3): order, exactly-once, resume, queued/rejected/end, heartbeat, disconnect, a run in progress."""
import asyncio
import json
import threading
from contextlib import closing

import httpx
import pytest

from app.api import sse
from app.api.worker import open_db
from app.enums import Decision, Outcome
from app.pipeline import persist
from app.pipeline import runner as runner_mod
from tests.api.helpers import SS_10963, ScriptedRuns, api, api_settings, build_app, live_server, run_and_wait, sse_events, upload
from tests.extraction.real import real_pdf
from tests.pipeline.helpers import demo_db


# ------------------------------------------------------------------------------------ the endpoint on finished runs

def db_events(c, rid):
    with closing(open_db(c.db_path, c.settings)) as conn:
        return [r[0] for r in conn.execute("SELECT seq FROM audit_events WHERE run_id = ? ORDER BY seq", (rid,))]


def test_a_finished_run_streams_every_event_once_in_order_then_ends(tmp_path):
    with api(tmp_path) as c:
        rid = run_and_wait(c, SS_10963)
        r = c.get(f"/api/runs/{rid}/events")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        frames = sse_events(r.text)
        seqs = db_events(c, rid)
    assert frames[0] == {"retry": "1000"}
    audit = [f for f in frames if f.get("event") == "audit"]
    assert [int(f["id"]) for f in audit] == seqs == list(range(len(seqs)))
    assert [f["data"]["seq"] for f in audit] == seqs
    assert frames[-1] == {"event": "end", "data": {"status": "completed", "decision": "review"}}
    assert audit[0]["data"]["event_type"] == "run_started" and audit[-1]["data"]["event_type"] == "run_completed"
    assert isinstance(audit[-1]["data"]["detail"], dict)


def test_last_event_id_resumes_after_that_event(tmp_path):
    with api(tmp_path) as c:
        rid = run_and_wait(c, SS_10963)
        total = len(db_events(c, rid))
        by_header = sse_events(c.get(f"/api/runs/{rid}/events", headers={"Last-Event-ID": "10"}).text)
        by_query = sse_events(c.get(f"/api/runs/{rid}/events?after=40").text)
        garbage = sse_events(c.get(f"/api/runs/{rid}/events", headers={"Last-Event-ID": "abc"}).text)
    assert [int(f["id"]) for f in by_header if f.get("event") == "audit"] == list(range(11, total))
    assert [int(f["id"]) for f in by_query if f.get("event") == "audit"] == list(range(41, total))
    assert len([f for f in garbage if f.get("event") == "audit"]) == total                     # unreadable id: from the start
    assert by_header[-1]["event"] == by_query[-1]["event"] == "end"


def test_resuming_after_the_last_event_sends_only_end(tmp_path):
    with api(tmp_path) as c:
        rid = run_and_wait(c, SS_10963)
        last = db_events(c, rid)[-1]
        frames = sse_events(c.get(f"/api/runs/{rid}/events", headers={"Last-Event-ID": str(last)}).text)
    assert [f.get("event") for f in frames] == [None, "end"]


def test_unknown_and_malformed_ids_are_404(tmp_path):
    with api(tmp_path) as c:
        assert c.get("/api/runs/0123456789abcdef0123456789abcdef/events").status_code == 404
        assert c.get("/api/runs/bad!id/events").status_code == 404


def test_a_failed_run_ends_with_status_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod.persist, "save_invoice", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with api(tmp_path) as c:
        rid = run_and_wait(c, SS_10963)
        frames = sse_events(c.get(f"/api/runs/{rid}/events").text)
    assert frames[-1] == {"event": "end", "data": {"status": "failed", "decision": None}}
    assert [f for f in frames if f.get("event") == "audit"][-1]["data"]["event_type"] == "pipeline_error"


def test_a_run_that_could_not_start_streams_rejected(tmp_path):
    with api(tmp_path, run_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))) as c:
        rid = run_and_wait(c, SS_10963)
        frames = sse_events(c.get(f"/api/runs/{rid}/events").text)
    assert frames[-1]["event"] == "rejected" and frames[-1]["data"]["code"] == "internal_error"


# ------------------------------------------------------------------------------------ a run in progress (real worker)

def test_a_run_in_progress_streams_stage_by_stage(tmp_path, monkeypatch):
    """Through a real server: events arrive while the run is held inside the match stage, then the rest follows."""
    gate, reached = threading.Event(), threading.Event()
    real_match = runner_mod.run_match_stage

    def held_match(ctx):
        reached.set()
        assert gate.wait(30)
        return real_match(ctx)
    monkeypatch.setattr(runner_mod, "run_match_stage", held_match)

    app, worker, db, settings = build_app(tmp_path)
    with live_server(app) as base, httpx.Client(base_url=base, timeout=30) as http:
        rid = http.post("/api/runs", files={"file": ("superstore_10963.pdf", real_pdf(SS_10963).read_bytes())}).json()["run_id"]
        assert reached.wait(30)
        seen, released = [], False
        with http.stream("GET", f"/api/runs/{rid}/events") as r:
            buf = ""
            for chunk in r.iter_text():
                buf += chunk
                while "\n\n" in buf:
                    raw, buf = buf.split("\n\n", 1)
                    f = sse_events(raw + "\n\n")
                    if not f:
                        continue
                    seen.append(f[0])
                    d = f[0].get("data") or {}
                    if not released and d.get("event_type") == "stage_started" and d["detail"]["stage"] == "match":
                        # everything up to the held stage has arrived while the run is still running
                        assert not any(x.get("data", {}).get("event_type") == "rule_evaluated" for x in seen)
                        released = True
                        gate.set()
        assert released and seen[-1]["event"] == "end" and seen[-1]["data"]["decision"] == "review"
        with closing(open_db(db, settings)) as conn:
            stored = [r[0] for r in conn.execute("SELECT seq FROM audit_events WHERE run_id = ? ORDER BY seq", (rid,))]
        assert [int(f["id"]) for f in seen if f.get("event") == "audit"] == stored


def test_a_queued_run_announces_its_state_before_the_run_row_exists(tmp_path, monkeypatch):
    gate = threading.Event()
    inner = ScriptedRuns()

    def slow(path, conn, **kw):
        assert gate.wait(30)
        return inner(path, conn, **kw)

    with api(tmp_path, run_fn=slow) as c:
        rid = upload(c, SS_10963).json()["run_id"]
        threading.Timer(0.3, gate.set).start()
        frames = sse_events(c.get(f"/api/runs/{rid}/events").text)
    kinds = [f.get("event") for f in frames]
    assert kinds[1] == "queued" and frames[1]["data"]["state"] in ("queued", "running")
    assert kinds[-1] == "end" and "audit" in kinds and kinds.index("queued") < kinds.index("audit")


# ------------------------------------------------------------------------------------ the generator itself

class FakeWorker:
    def __init__(self, states):
        self.states = list(states)

    def state(self, run_id):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def rejection(self, run_id):
        return {"code": "x", "message": "y"}


async def collect(gen, limit=10_000, timeout=10):
    out = []

    async def go():
        async for item in gen:
            out.append(item)
            if len(out) >= limit:
                break
    await asyncio.wait_for(go(), timeout)
    return out


@pytest.fixture
def dbfile(tmp_path):
    conn = demo_db(tmp_path)
    conn.close()
    return tmp_path / "app.db"


def opener(path, settings, counter=None):
    def open_conn():
        if counter is not None:
            counter[0] += 1
        return open_db(path, settings)
    return open_conn


def test_more_than_one_batch_is_delivered_completely_and_in_order(dbfile, tmp_path):
    settings = api_settings(tmp_path)
    conn = open_db(dbfile, settings)
    with persist.transaction(conn):
        persist.start_run(conn, "big", "big.pdf")
        w = persist.AuditWriter(conn, "big")
        for i in range(sse.BATCH * 2 + 7):
            w.emit("test", "tick", Outcome.INFO, f"tick {i}")
        persist.finish_run(conn, "big", status="completed", decision=Decision.REVIEW, tokens_in=0, tokens_out=0, cost_usd=0, model=None)
    conn.close()
    frames = sse_events("".join(asyncio.run(collect(sse.run_events("big", open_conn=opener(dbfile, settings), worker=FakeWorker([None]),
                                                                     poll_s=0.01)))))
    audit = [int(f["id"]) for f in frames if f.get("event") == "audit"]
    assert audit == list(range(sse.BATCH * 2 + 7)) and frames[-1]["event"] == "end"


def test_a_heartbeat_is_sent_while_nothing_happens_and_a_disconnect_stops_polling(dbfile, tmp_path):
    settings = api_settings(tmp_path)
    conn = open_db(dbfile, settings)
    with persist.transaction(conn):
        persist.start_run(conn, "idle", "idle.pdf")                      # stays running, no events
    conn.close()
    polls, checks = [0], [0]

    async def disconnected():
        checks[0] += 1
        return checks[0] > 12

    out = asyncio.run(collect(sse.run_events("idle", open_conn=opener(dbfile, settings, polls), worker=FakeWorker([None]),
                                             poll_s=0.01, heartbeat_s=0.03, is_disconnected=disconnected)))
    assert ": ping\n\n" in out and not any("event: end" in x for x in out)
    assert polls[0] == 12                                                  # stopped at the first disconnected check


def test_a_run_that_disappears_ends_with_unknown(dbfile, tmp_path):
    settings = api_settings(tmp_path)
    frames = sse_events("".join(asyncio.run(collect(sse.run_events("ghost", open_conn=opener(dbfile, settings),
                                                                     worker=FakeWorker(["queued", None]), poll_s=0.01)))))
    assert [f.get("event") for f in frames] == [None, "queued", "end"] and frames[-1]["data"]["status"] == "unknown"


def test_the_run_finishing_between_the_two_reads_is_still_streamed(dbfile, tmp_path):
    settings = api_settings(tmp_path)

    class FinishesDuringCheck(FakeWorker):
        def state(self, run_id):                                         # the worker forgets the run: it was just stored
            conn = open_db(dbfile, settings)
            with persist.transaction(conn):
                persist.start_run(conn, "late", "late.pdf")
                persist.AuditWriter(conn, "late").emit("test", "tick", Outcome.INFO, "tick")
                persist.finish_run(conn, "late", status="completed", decision=Decision.REVIEW, tokens_in=0, tokens_out=0,
                                   cost_usd=0, model=None)
            conn.close()
            return None

    frames = sse_events("".join(asyncio.run(collect(sse.run_events("late", open_conn=opener(dbfile, settings),
                                                                     worker=FinishesDuringCheck([None]), poll_s=0.01)))))
    assert [f.get("event") for f in frames] == [None, "audit", "end"] and frames[-1]["data"]["status"] == "completed"


def test_frames_are_well_formed():
    assert sse.frame("audit", {"a": "é"}, id=3) == 'id: 3\nevent: audit\ndata: {"a":"é"}\n\n'
    assert sse.parse_last_event_id(None) == -1 and sse.parse_last_event_id(" 7 ") == 7 and sse.parse_last_event_id("-5") == -1
    assert json.loads(sse.frame(data={"x": 1}).split("data: ")[1]) == {"x": 1}
