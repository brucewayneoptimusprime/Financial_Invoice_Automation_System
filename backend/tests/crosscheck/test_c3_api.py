"""Cross-check stage C3: the two routes. Report only (no database write, proven by table counts and the file's bytes), the access
gate, the caps, empty and unreadable files, model failures, the modes. Scripted model doubles; no live call."""
import hashlib
import re
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.crosscheck.facts import load_po_context, open_readonly
from app.crosscheck.service import OFFLINE_MESSAGE, REPLAY_MISS, REPORT_ONLY_LABEL
from app.llm.budget import CostTracker
from app.llm.errors import LLMTimeoutError, ReplayMiss
from tests.api.helpers import api, api_settings, build_app, run_and_wait
from tests.crosscheck.helpers import client_for, note
from tests.ingest.docs import make_blank_pdf, make_native_pdf, make_png
from tests.review.helpers import SS_10963, item_for, runs_with_scenarios
from tests.review.test_review_actions import approve

APP = Path(__file__).resolve().parents[2] / "app"
PO = {"po_number": "PO-7001", "currency": "USD", "total": "1000.00", "issued_date": "2026-03-01",
      "lines": [{"description": "Widget A", "quantity": "10", "unit_price": "60.00", "amount": "600.00"},
                {"description": "Widget B", "quantity": "5", "unit_price": "80.00", "amount": "400.00"}]}


def pdf(tmp_path, name="note.pdf", **changes) -> tuple[tuple[str, bytes, str], dict]:
    """(the multipart file, the model reply for it): a real one-page PDF whose text layer is the note's lines."""
    text, reply = note(**changes)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = make_native_pdf(tmp_path / f"src-{name}", [text.split("\n")])
    return (name, path.read_bytes(), "application/pdf"), reply


def start(tmp_path, *replies, mode="replay", **kw):
    """A started app whose model is scripted, with PO-7001 saved through the PO form's own route. Returns (client, po id)."""
    kw.setdefault("crosscheck_tmp_dir", tmp_path / "cc")
    tracker = kw.pop("tracker", None)
    app, worker, db, settings = build_app(tmp_path, mode=mode, settings=api_settings(tmp_path, **kw), tracker=tracker,
                                          inner_client=client_for(*replies) if mode != "offline" else None)
    c = TestClient(app)
    c.__enter__()
    c.db_path, c.settings, c.tracker, c.model = db, settings, app.state.api.tracker, worker.client.inner
    token = settings.access_token_value()
    r = c.post("/api/pos", json={"po": PO, "new_vendor": {"name": "Northwind Trading Co"}},
               headers={"Authorization": f"Bearer {token}"} if token else {})
    assert r.status_code == 201, r.text
    return c, r.json()["po_id"]


def post(c, po_id, *files, **kw):
    return c.post(f"/api/pos/{po_id}/crosscheck", files=[("files", f) for f in files], **kw)


def snapshot(c) -> tuple[dict, str]:
    """Row counts of EVERY table, and the SHA-256 of the database file."""
    with closing(sqlite3.connect(c.db_path)) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    return counts, hashlib.sha256(Path(c.db_path).read_bytes()).hexdigest()


def leftovers(tmp_path) -> list:
    folder = tmp_path / "cc"
    return list(folder.iterdir()) if folder.exists() else []


# ---------------------------------------------------------------------------------------------------- the report

def test_info_gives_the_limits_the_label_and_the_cost_figures_without_calling_the_model(tmp_path):
    c, po_id = start(tmp_path, mode="live")
    r = c.get(f"/api/pos/{po_id}/crosscheck")
    assert r.status_code == 200 and r.json() == {
        "enabled": True, "mode": "live", "available": True, "message": None, "label": REPORT_ONLY_LABEL, "max_documents": 5,
        "max_file_mb": 20.0, "accepted": ["PDF", "PNG", "JPG"], "typical_cost_per_document_usd": "0.02",
        "ceiling_per_document_usd": "0.25", "budget_remaining_usd": "5.00"}
    assert c.model.requests == [] and c.get("/api/pos/99999/crosscheck").status_code == 404
    assert REPORT_ONLY_LABEL == "Report only: nothing here changes the PO, its invoices, the ledger or any decision."


def test_a_matching_document_is_related_with_no_differences_and_the_cost_is_reported(tmp_path):
    f, reply = pdf(tmp_path / "src")
    c, po_id = start(tmp_path, reply)
    r = post(c, po_id, f)
    assert r.status_code == 200, r.text
    body = r.json()
    (doc,) = body["documents"]
    assert doc["status"] == "analysed" and doc["file_name"] == "note.pdf" and doc["pages"] == 1 and doc["failure"] is None
    assert doc["relevance"]["related"] and doc["differences"] == [] and doc["unconfirmed"] == [] and doc["notices"] == []
    assert {s["signal"]: s["holds"] for s in doc["relevance"]["signals"]} == {"po_number": True, "invoice_number": False,
                                                                              "vendor": True, "lines": True}
    assert body["label"] == REPORT_ONLY_LABEL
    a = body["analysis"]
    assert (a["po_number"], a["documents"], a["analysed"], a["mode"], a["prompt_version"]) == ("PO-7001", 1, 1, "replay", "crosscheck-v1")
    assert (a["tokens_in"], a["tokens_out"], a["cost_usd"], doc["cost_usd"], a["estimated_before_usd"]) == (3500, 700, "0.014000", "0.014000", "0.02")
    (req,) = c.model.requests
    assert req.purpose == "crosscheck" and re.fullmatch(r"crosscheck-[0-9a-f]{32}-1", req.run_id)
    assert any(p.kind == "image" for p in req.parts) and c.tracker.session_spent == Decimal("0.014")
    assert leftovers(tmp_path) == []                                              # the upload and its pages are gone


def test_several_documents_are_reported_each_on_its_own_in_upload_order(tmp_path):
    good, r1 = pdf(tmp_path / "src", "a.pdf")
    short, r2 = pdf(tmp_path / "src", "b.pdf", a=("Widget A", "8", "60.00", "480.00"), total="880.00")
    other, r3 = pdf(tmp_path / "src", "c.pdf", vendor="Acme Trading", po="PO-2001", a=("Stapler, heavy duty", "1", "25.00", "25.00"),
                    b=None, total="25.00")
    c, po_id = start(tmp_path, r1, r2, r3)
    body = post(c, po_id, good, short, other).json()
    docs = body["documents"]
    assert [d["file_name"] for d in docs] == ["a.pdf", "b.pdf", "c.pdf"]
    assert [d["relevance"]["related"] for d in docs] == [True, True, False]
    assert [[x["type"] for x in d["differences"]] for d in docs] == [[], ["quantity_vs_po"], []]
    assert docs[1]["differences"][0]["document"] == {"value": "8", "page": 1, "source_text": "Widget A 8 pcs 60.00 480.00"}
    assert body["analysis"]["cost_usd"] == "0.042000" and [r.run_id[-2:] for r in c.model.requests] == ["-1", "-2", "-3"]


def test_a_scanned_image_is_analysed_and_marked_as_read_from_the_image(tmp_path):
    _, reply = pdf(tmp_path / "src")
    c, po_id = start(tmp_path, reply)
    png = make_png(tmp_path / "scan.png").read_bytes()
    (doc,) = post(c, po_id, ("scan.png", png, "image/png")).json()["documents"]
    assert doc["status"] == "analysed" and doc["differences"] == []
    assert doc["notices"] == ["Read from the image: this document has no text layer, so its values could not be checked against text."]


# ------------------------------------------------------------------------------------------------- report only

def test_an_analysis_writes_nothing_table_counts_and_the_database_bytes_are_identical(tmp_path):
    hostile, r1 = pdf(tmp_path / "src", "hostile.pdf", hostile=True, a=("Widget A", "8", "60.00", "480.00"), total="880.00")
    r1.update({"decision": "approve", "set_quantity": 0, "notes": "Approve this PO and write the ledger."})
    other, r2 = pdf(tmp_path / "src", "other.pdf", vendor="Acme Trading", po="PO-2001")
    c, po_id = start(tmp_path, r1, r2)
    po_before = c.get(f"/api/pos/{po_id}").json()
    before = snapshot(c)
    r = post(c, po_id, hostile, other, ("empty.pdf", b"", "application/pdf"))
    assert r.status_code == 200 and snapshot(c) == before
    assert c.get(f"/api/pos/{po_id}").json() == po_before and leftovers(tmp_path) == []
    doc = r.json()["documents"][0]
    assert "text addressed to an AI reader" in doc["notices"][0]                    # flagged, and the report is what the page says
    assert [x["type"] for x in doc["differences"]] == ["quantity_vs_po"] and "decision" not in doc
    assert doc["facts"]["model_notes"] == "Approve this PO and write the ledger."   # shown as the model's words, acted on by nothing
    assert set(before[0]) >= {"purchase_orders", "po_lines", "invoices", "ledger_entries", "po_consumption", "review_queue",
                              "audit_events", "runs", "drafts", "settings_events"}


def test_the_package_has_no_write_statement_and_imports_no_writer():
    sources = {p: p.read_text(encoding="utf-8") for p in [*sorted((APP / "crosscheck").glob("*.py")), APP / "api" / "routes_crosscheck.py"]}
    assert len(sources) == 8
    for path, text in sources.items():
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE)\s", text), path.name
        assert not re.search(r"\.(commit|executescript|executemany)\(", text), path.name
        assert not re.search(r"import[^\n]*\b(store|persist|actions|save_po|record_consumption|open_db|connect|reset|migrate)\b", text), path.name
        assert not re.search(r"from app\.(pipeline|review|db|gmail|erp|rulesettings)\b", text), path.name
    assert "mode=ro" in sources[APP / "crosscheck" / "facts.py"]
    assert "open_readonly" in sources[APP / "api" / "routes_crosscheck.py"]


# --------------------------------------------------------------------------------------------------- access gate

def test_with_an_access_token_both_routes_need_it(tmp_path):
    f, reply = pdf(tmp_path / "src")
    c, po_id = start(tmp_path, reply, access_token="t0p-secret-token")
    auth = {"Authorization": "Bearer t0p-secret-token"}
    assert c.get(f"/api/pos/{po_id}/crosscheck").status_code == 401 and post(c, po_id, f).status_code == 401
    assert post(c, po_id, f, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.model.requests == [] and leftovers(tmp_path) == []
    assert c.get(f"/api/pos/{po_id}/crosscheck", headers=auth).status_code == 200
    r = post(c, po_id, f, headers=auth)
    assert r.status_code == 200 and r.json()["documents"][0]["status"] == "analysed" and "t0p-secret" not in r.text


# ---------------------------------------------------------------------------------------------------------- caps

def test_more_than_five_documents_or_none_are_refused_and_nothing_is_sent(tmp_path):
    f, reply = pdf(tmp_path / "src")
    c, po_id = start(tmp_path, *[reply] * 6)
    r = post(c, po_id, *[(f"n{i}.pdf", f[1], f[2]) for i in range(6)])
    assert (r.status_code, r.json()["error"]) == (422, "too_many") and "At most 5" in r.json()["message"]
    assert post(c, po_id, *[(f"n{i}.pdf", f[1], f[2]) for i in range(40)]).status_code == 400      # the form parser's own cap
    none = c.post(f"/api/pos/{po_id}/crosscheck", files=[("other", f)])
    assert (none.status_code, none.json()["error"]) == (422, "no_file")
    assert c.post(f"/api/pos/{po_id}/crosscheck", json={}).status_code == 422
    assert c.model.requests == [] and leftovers(tmp_path) == [] and c.tracker.session_spent == 0
    assert len(post(c, po_id, *[(f"n{i}.pdf", f[1], f[2]) for i in range(5)]).json()["documents"]) == 5


def test_the_cap_is_a_setting_and_an_oversize_file_fails_alone(tmp_path):
    f, reply = pdf(tmp_path / "src")
    c, po_id = start(tmp_path, reply, crosscheck_max_documents=2, max_file_bytes=20_000)
    assert post(c, po_id, f, f, f).json()["error"] == "too_many"
    big = ("big.pdf", b"%PDF-1.4\n" + b"0" * 30_000, "application/pdf")
    docs = post(c, po_id, big, f).json()["documents"]
    assert [d["status"] for d in docs] == ["failed", "analysed"]
    assert docs[0]["failure"] == {"code": "too_large", "message": "big.pdf is larger than the 0.0 MB limit."}
    huge = c.post(f"/api/pos/{po_id}/crosscheck", content=b"x", headers={"content-type": "multipart/form-data; boundary=x",
                                                                         "content-length": "999999999"})
    assert huge.status_code == 413


def test_the_budget_pre_check_refuses_the_whole_analysis_before_any_call(tmp_path):
    f, reply = pdf(tmp_path / "src")
    c, po_id = start(tmp_path, reply, reply, tracker=CostTracker(Decimal("0.25"), Decimal("0.05")))
    r = post(c, po_id, f, ("again.pdf", f[1], f[2]))
    assert (r.status_code, r.json()["error"]) == (409, "budget") and "Nothing was sent" in r.json()["message"]
    assert r.json()["remaining_usd"] == "0.05" and c.model.requests == [] and leftovers(tmp_path) == []
    assert post(c, po_id, f).status_code == 200                                    # one document fits


def test_a_per_document_ceiling_hit_fails_only_that_document(tmp_path):
    small, r1 = pdf(tmp_path / "src", "small.pdf")
    c, po_id = start(tmp_path, r1, r1, tracker=CostTracker(Decimal("0.045"), Decimal("5")))
    text, _ = note()
    long_pdf = make_native_pdf(tmp_path / "long.pdf", [text.split("\n") + ["Filler line with many words " * 4] * 40] * 3)
    docs = post(c, po_id, ("long.pdf", long_pdf.read_bytes(), "application/pdf"), small).json()["documents"]
    assert [d["status"] for d in docs] == ["failed", "analysed"] and docs[0]["failure"]["code"] == "cost_ceiling"
    assert "The call was not made" in docs[0]["failure"]["message"] and len(c.model.requests) == 1


# ------------------------------------------------------------------------------------- empty and unreadable files

def test_empty_and_unreadable_files_each_get_a_clear_message_and_never_a_500(tmp_path):
    good, reply = pdf(tmp_path / "src", "good.pdf")
    c, po_id = start(tmp_path, reply)
    locked = make_native_pdf(tmp_path / "locked.pdf", [["secret"]], password="pw").read_bytes()
    blank = make_blank_pdf(tmp_path / "blank.pdf").read_bytes()
    r = post(c, po_id, ("empty.pdf", b"", "application/pdf"), ("setup.pdf", b"MZ\x90\x00" + b"\x00" * 64, "application/pdf"),
             ("locked.pdf", locked, "application/pdf"), ("blank.pdf", blank, "application/pdf"), good)
    assert r.status_code == 200, r.text
    docs = r.json()["documents"]
    assert [d["status"] for d in docs] == ["failed"] * 4 + ["analysed"]
    assert [d["failure"]["code"] for d in docs[:4]] == ["empty_file", "unsupported_type", "password_protected", "blank_document"]
    assert docs[0]["failure"]["message"] == "empty.pdf is empty (0 bytes)."
    assert "a Windows executable" in docs[1]["failure"]["message"] and "only PDF, PNG and JPEG" in docs[1]["failure"]["message"]
    assert all(d["failure"]["message"] and d["cost_usd"] == "0.000000" for d in docs[:4])
    assert len(c.model.requests) == 1 and r.json()["analysis"]["analysed"] == 1 and leftovers(tmp_path) == []


def test_an_office_document_is_refused_because_only_pdf_png_and_jpg_are_accepted(tmp_path):
    from tests.po.test_po_readers import docx_bytes

    c, po_id = start(tmp_path)
    (doc,) = post(c, po_id, ("note.docx", docx_bytes(["Purchase Order: PO-7001"]), "application/octet-stream")).json()["documents"]
    assert doc["status"] == "failed" and doc["failure"]["code"] == "unsupported_type" and c.model.requests == []


# ------------------------------------------------------------------------------------------------ model failures

def test_a_model_failure_fails_only_its_document(tmp_path):
    f, reply = pdf(tmp_path / "src")
    c, po_id = start(tmp_path, LLMTimeoutError("The LLM request timed out (after retries)."), "not json", "still not json", reply)
    r = post(c, po_id, ("one.pdf", f[1], f[2]), ("two.pdf", f[1], f[2]), ("three.pdf", f[1], f[2]))
    assert r.status_code == 200
    docs = r.json()["documents"]
    assert [d["status"] for d in docs] == ["failed", "failed", "analysed"]
    assert [d["failure"]["code"] for d in docs[:2]] == ["timeout", "schema_invalid"]
    assert docs[1]["cost_usd"] == "0.028000" and r.json()["analysis"]["cost_usd"] == "0.042000"      # failed attempts are still paid for


# --------------------------------------------------------------------------------------------------------- modes

def test_offline_refuses_before_any_work_and_says_so(tmp_path):
    f, _ = pdf(tmp_path / "src")
    c, po_id = start(tmp_path, mode="offline")
    info = c.get(f"/api/pos/{po_id}/crosscheck").json()
    assert (info["available"], info["message"], info["mode"]) == (False, OFFLINE_MESSAGE, "offline")
    r = post(c, po_id, f)
    assert (r.status_code, r.json()) == (503, {"error": "offline", "message": "Offline mode: no model is available to read documents."})
    assert leftovers(tmp_path) == [] and c.tracker.session_spent == 0


def test_replay_without_a_recording_gives_a_clear_message_for_that_document(tmp_path):
    f, reply = pdf(tmp_path / "src")
    c, po_id = start(tmp_path, ReplayMiss("No recorded response for this request (key 0123456789ab...) in recordings."), reply)
    assert "Replay mode" in c.get(f"/api/pos/{po_id}/crosscheck").json()["message"]
    docs = post(c, po_id, f, ("two.pdf", f[1], f[2])).json()["documents"]
    assert docs[0]["failure"] == {"code": "replay_miss", "message": REPLAY_MISS} and docs[1]["status"] == "analysed"
    assert REPLAY_MISS == "Replay mode: no recorded answer exists for this document."


def test_switched_off_both_routes_answer_404(tmp_path):
    f, reply = pdf(tmp_path / "src")
    c, po_id = start(tmp_path, reply, crosscheck_enabled=False)
    assert c.get(f"/api/pos/{po_id}/crosscheck").status_code == 404 and post(c, po_id, f).status_code == 404
    assert post(c, 99999, f).status_code == 404 and c.model.requests == []


# ------------------------------------------------------------------------------------ invoiced quantity, from real runs

def test_invoiced_quantity_counts_an_invoice_in_review_by_its_line_match_and_an_approved_one_by_its_allocation(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        rid = run_and_wait(c, SS_10963)                                           # real invoice 10963, held for review
        with closing(open_readonly(c.db_path)) as conn:
            in_review = load_po_context(conn, 1)
        assert approve(c, item_for(c, rid)).status_code == 200
        with closing(open_readonly(c.db_path)) as conn:
            approved = load_po_context(conn, 1)
            other = load_po_context(conn, 2)
    (line,) = in_review.lines
    assert in_review.invoices == (("10963", "in_review"),) and approved.invoices == (("10963", "approved"),)
    assert [(e.invoice_number, e.status, e.quantity, e.basis) for e in in_review.invoiced[line.id]] == [("10963", "in_review", Decimal("4"), "line_match")]
    assert [(e.invoice_number, e.status, e.quantity, e.basis) for e in approved.invoiced[line.id]] == [("10963", "approved", Decimal("4"), "allocated")]
    assert other.invoices == () and other.invoiced == {}
