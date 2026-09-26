"""The HTTP API (M4 stage 2): upload checks, the worker queue, the run view, the runs list, page images, health.

Offline: the app's client is an OfflineClient; extraction replies are recorded fixtures chosen per upload (tests/api/helpers.py).
"""
import json
import sqlite3
import threading

import pytest

from app.api import views
from tests.api.helpers import IQ, SS_10963, SS_24429, ScriptedRuns, api, api_settings, run_and_wait, upload
from tests.extraction.real import real_reply
from tests.extraction.wire_convert import set_field
from tests.pipeline.helpers import controlled_variant_reply


# -------------------------------------------------------------------------------------------- health

def test_health_reports_the_mode_and_limits(tmp_path):
    with api(tmp_path) as c:
        h = c.get("/api/health").json()
    assert h["mode"] == "offline" and h["model"] == "claude-sonnet-5" and h["session_spent_usd"] == "0"
    assert h["session_ceiling_usd"] == "5.00" and h["queue_length"] == 0 and h["max_file_bytes"] == 20 * 1024 * 1024 and h["max_files_per_upload"] == 20


# -------------------------------------------------------------------------------------------- upload

def test_an_accepted_upload_returns_202_and_a_run_id_at_once(tmp_path):
    with api(tmp_path) as c:
        r = upload(c, SS_10963)
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "queued" and len(body["run_id"]) == 32 and body["source_file"] == "superstore_10963.pdf"
        assert body["media_type"] == "application/pdf" and body["events"] == f"/api/runs/{body['run_id']}/events"
        assert c.worker.wait_idle(60)


@pytest.mark.parametrize("data, name, status, code", [
    (b"", "empty.pdf", 400, "empty_file"),
    (b"GIF89a" + b"\0" * 100, "fake.pdf", 415, "unsupported_type"),
    (b"PK\x03\x04" + b"\0" * 100, "doc.docx", 415, "unsupported_type"),
    (b"just text", "notes.txt", 415, "unsupported_type"),
])
def test_rejected_uploads_create_no_run_and_leave_no_upload_folder(tmp_path, data, name, status, code):
    with api(tmp_path) as c:
        r = upload(c, name, data=data)
        assert (r.status_code, r.json()["error"]) == (status, code) and r.json()["message"]
        assert c.worker.wait_idle(10)
        with sqlite3.connect(c.db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        up = c.settings.api_upload_dir
        assert not up.exists() or not any(up.iterdir())


def test_an_oversize_upload_is_refused_from_its_declared_length_without_reading_it(tmp_path):
    settings = api_settings(tmp_path, max_file_bytes=1000)
    with api(tmp_path, settings=settings) as c:
        r = c.post("/api/runs", content=b"x" * 10, headers={"content-type": "multipart/form-data; boundary=b",
                                                             "content-length": str(1000 + 64 * 1024 + 1)})
        assert (r.status_code, r.json()["error"]) == (413, "too_large")


def test_an_oversize_file_inside_a_small_enough_request_is_refused_while_copying(tmp_path):
    settings = api_settings(tmp_path, max_file_bytes=1000)
    with api(tmp_path, settings=settings) as c:
        r = upload(c, "big.pdf", data=b"%PDF-1.4\n" + b"0" * 5000)
        assert (r.status_code, r.json()["error"]) == (413, "too_large")
        assert not any(c.settings.api_upload_dir.iterdir())


def test_a_request_without_a_file_is_422(tmp_path):
    with api(tmp_path) as c:
        assert c.post("/api/runs", data={"x": "1"}).status_code == 422
        assert c.post("/api/runs", content=b"{}", headers={"content-type": "application/json"}).status_code == 422


@pytest.mark.parametrize("hostile", ["../../evil.pdf", "..\\..\\evil.pdf", "CON.pdf", "nul", "..", "   ", "a\x00b.pdf",
                                     "ü–invoice №1.pdf", "x" * 400 + ".pdf"])
def test_hostile_file_names_stay_inside_the_upload_folder(tmp_path, hostile):
    seen = []

    class Spy(ScriptedRuns):
        def __call__(self, path, conn, **kw):
            seen.append(path)
            return super().__call__(path, conn, **kw)

    with api(tmp_path, run_fn=Spy()) as c:
        r = upload(c, SS_10963, filename=hostile)
        assert r.status_code == 202, r.text
        assert c.worker.wait_idle(60)
        up = c.settings.api_upload_dir.resolve()
        assert seen and seen[0].resolve().parent.parent == up
        assert not (tmp_path / "evil.pdf").exists() and not (tmp_path.parent / "evil.pdf").exists()
        assert not any(up.iterdir())                                        # removed after the run
        view = c.get(f"/api/runs/{r.json()['run_id']}").json()
        assert "/" not in view["run"]["source_file"] and "\\" not in view["run"]["source_file"]


# -------------------------------------------------------------------------------------------- worker

def test_runs_execute_one_at_a_time_in_upload_order(tmp_path):
    active, peak, order = [0], [0], []
    lock = threading.Lock()
    inner = ScriptedRuns()

    def run_fn(path, conn, **kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            order.append(kw["source_name"])
            return inner(path, conn, **kw)
        finally:
            with lock:
                active[0] -= 1

    with api(tmp_path, run_fn=run_fn) as c:
        ids = [upload(c, n).json()["run_id"] for n in (SS_10963, SS_24429, IQ)]
        assert c.worker.wait_idle(120)
        assert peak[0] == 1 and order == ["superstore_10963.pdf", "superstore_24429.pdf", "iq_electronics.jpg"]
        assert [c.get(f"/api/runs/{i}").json()["run"]["status"] for i in ids] == ["completed"] * 3


def test_a_run_that_cannot_start_is_reported_as_rejected(tmp_path):
    def broken(path, conn, **kw):
        raise RuntimeError("boom before any run row")

    with api(tmp_path, run_fn=broken) as c:
        rid = run_and_wait(c, SS_10963)
        v = c.get(f"/api/runs/{rid}").json()
        assert v["run"] == {"id": rid, "status": "rejected"} and v["rejection"]["code"] == "internal_error"
        assert "boom" not in json.dumps(v)                                 # internal text is not echoed


def test_a_pipeline_failure_is_a_failed_run_with_no_decision(tmp_path, monkeypatch):
    from app.pipeline import runner
    monkeypatch.setattr(runner.persist, "save_invoice", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret detail")))
    with api(tmp_path) as c:
        rid = run_and_wait(c, SS_10963)
        v = c.get(f"/api/runs/{rid}").json()
    assert v["run"]["status"] == "failed" and v["decision"] is None
    assert v["error"] == {"message": "The run failed: RuntimeError.", "error_type": "RuntimeError"} and "secret detail" not in json.dumps(v)
    assert {s["stage"]: s["status"] for s in v["stages"]}["act"] == "running"


# -------------------------------------------------------------------------------------------- run view

def test_the_run_view_of_a_review(tmp_path):
    with api(tmp_path) as c:
        rid = run_and_wait(c, SS_10963)
        v = c.get(f"/api/runs/{rid}").json()
    assert v["run"]["status"] == "completed" and v["decision"] == "review" and v["run"]["source_file"] == "superstore_10963.pdf"
    assert [s["stage"] for s in v["stages"]] == ["ingest", "extract", "match", "validate", "decide", "explain", "act"]
    assert all(s["status"] in ("ok", "flagged") and s["duration_ms"] is not None for s in v["stages"])
    assert v["invoice"]["total"] == "5338.08" and v["invoice"]["subtotal"] == "5141.76" and v["invoice"]["status"] == "in_review"
    assert v["lines"][0]["amount"] == "5141.76" and v["extracted"]["invoice_number"]["value"] == "10963"
    assert v["extracted"]["total"]["source_text"] and v["extracted"]["total"]["page"] == 1
    assert v["vendor"]["record"]["name"] == "SuperStore" and v["vendor"]["method"] == "exact_name"
    assert v["match"]["status"] == "matched" and v["match"]["matched_po"] == "PO-SS-001" and len(v["match"]["candidates"]) == 5
    rules = {r["rule_id"]: r for r in v["rules"]}
    assert len(rules) == 16 and rules["r_po_found"]["outcome"] == "flag" and rules["r_po_found"]["severity"] == 1
    assert rules["r_po_found"]["outcome_key"] == "matched_without_reference" and rules["engine_floor"]["kind"] == "floor"
    assert rules["r_tolerance_pct"]["detail"]["balance"] == "6000.00"
    assert v["explanation"]["text"].startswith("Decision: REVIEW.") and v["explanation"]["source"] == "template"
    assert v["actions"]["review"][0]["status"] == "open" and v["actions"]["drafts"] == [] and v["actions"]["ledger"] is None
    assert v["pages"] == [1] and v["error"] is None and v["aggregate"]["final_severity"] == 1


def test_the_run_view_of_the_synthetic_approve_variant_shows_the_ledger(tmp_path):
    runs = ScriptedRuns({SS_24429: controlled_variant_reply(SS_24429, "PO-SS-002")})   # SYNTHETIC controlled variant
    with api(tmp_path, run_fn=runs) as c:
        rid = run_and_wait(c, SS_24429)
        v = c.get(f"/api/runs/{rid}").json()
    assert v["decision"] == "approve" and v["actions"]["ready_for_payment"] is True
    led = v["actions"]["ledger"]
    assert (led["po_number"], led["amount"], led["balance_before"], led["balance_after"]) == ("PO-SS-002", "1770.61", "2500.00", "729.39")
    assert "SYNTHETIC CONTROLLED VARIANT" in v["extracted"]["extraction_notes"]


def test_the_run_view_of_a_request_info_carries_the_draft(tmp_path):
    reply = real_reply(SS_10963)
    set_field(reply, "invoice_number", found=False, value="", page=0, source_text="", confidence=0.0)
    with api(tmp_path, run_fn=ScriptedRuns({SS_10963: reply})) as c:
        rid = run_and_wait(c, SS_10963)
        v = c.get(f"/api/runs/{rid}").json()
    assert v["decision"] == "request_info" and v["invoice"]["status"] == "awaiting_info"
    d = v["actions"]["drafts"][0]
    assert d["kind"] == "vendor_email" and d["status"] == "draft" and d["to"] is None and "invoice number" in d["body"].lower()


def test_an_unknown_or_malformed_run_id_is_404(tmp_path):
    with api(tmp_path) as c:
        assert c.get("/api/runs/0123456789abcdef0123456789abcdef").status_code == 404
        assert c.get("/api/runs/..%2F..%2Fetc").status_code == 404
        assert c.get("/api/runs/bad!id").status_code == 404


def test_money_is_never_a_float(tmp_path):
    with api(tmp_path) as c:
        v = c.get(f"/api/runs/{run_and_wait(c, SS_10963)}").json()
    for key in ("subtotal", "tax", "total"):
        assert v["invoice"][key] is None or isinstance(v["invoice"][key], str)
    assert all(isinstance(line["amount"], str) for line in v["lines"])


# -------------------------------------------------------------------------------------------- runs list

def test_the_runs_list_is_newest_first_and_capped(tmp_path):
    with api(tmp_path) as c:
        a = run_and_wait(c, SS_10963)
        b = run_and_wait(c, SS_24429)
        runs = c.get("/api/runs?limit=1").json()["runs"]
        assert [r["id"] for r in runs] == [b]
        runs = c.get("/api/runs").json()["runs"]
        assert [r["id"] for r in runs] == [b, a] and runs[0]["final_decision"] == "review"
        assert c.get("/api/runs?limit=0").status_code == 422


# -------------------------------------------------------------------------------------------- pages

def test_a_rendered_page_is_served_and_nothing_else_is(tmp_path):
    with api(tmp_path) as c:
        rid = run_and_wait(c, SS_10963)
        r = c.get(f"/api/runs/{rid}/pages/1")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:8] == b"\x89PNG\r\n\x1a\n"
        for bad in (f"/api/runs/{rid}/pages/2", f"/api/runs/{rid}/pages/0", f"/api/runs/{rid}/pages/-1",
                    f"/api/runs/{rid}/pages/..%2F..%2Foriginal", "/api/runs/0123456789abcdef0123456789abcdef/pages/1",
                    f"/api/runs/{rid}/pages/1.png"):
            assert c.get(bad).status_code in (404, 422), bad


def test_page_path_refuses_anything_outside_the_pages_folder(tmp_path):
    runs = tmp_path / "runs"
    (runs / "abc" / "pages").mkdir(parents=True)
    (runs / "abc" / "pages" / "page-1.png").write_bytes(b"x")
    (runs / "abc" / "original.pdf").write_bytes(b"x")
    assert views.page_path(runs, "abc", 1) is not None and views.page_numbers(runs, "abc") == [1]
    assert views.page_path(runs, "abc", 2) is None and views.page_numbers(runs, "nope") == []


# -------------------------------------------------------------------------------------------- CORS

def test_cors_allows_only_the_configured_origin(tmp_path):
    with api(tmp_path) as c:
        ok = c.get("/api/health", headers={"Origin": "http://localhost:5173"})
        bad = c.get("/api/health", headers={"Origin": "http://evil.example"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers
