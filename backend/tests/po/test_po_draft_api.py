"""The PO draft endpoints (PO integration stage 4): the live rule, no database writes, the confirmation hand-off, documents."""
import io
import json
import zipfile
from contextlib import closing

from fastapi.testclient import TestClient

from app.api.worker import open_db
from app.llm.errors import ReplayMiss
from tests.api.helpers import api, build_app
from tests.ingest.docs import make_native_pdf
from tests.po.helpers import TYPED, client_for, entry, po_reply
from tests.po.test_po_readers import docx_bytes

TABLES = ("vendors", "purchase_orders", "po_lines", "runs", "invoices", "invoice_lines", "ledger_entries", "audit_events",
          "review_queue", "drafts", "rules", "settings")
CANARY = "sk-ant-api03-CANARY-do-not-leak-0123456789"


def counts(c):
    with closing(open_db(c.db_path, c.settings)) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def scripted(tmp_path, *replies, **kw):
    app, worker, db, settings = build_app(tmp_path, mode="replay", inner_client=client_for(*replies), **kw)
    c = TestClient(app)
    c.__enter__()
    c.worker, c.db_path, c.settings, c.tracker = worker, db, settings, app.state.api.tracker
    return c


# ------------------------------------------------------------------------------------------ the live rule

def test_offline_refuses_both_draft_endpoints_without_any_work(tmp_path):
    with api(tmp_path) as c:                                              # the helper's app is offline
        r1 = c.post("/api/pos/drafts/text", json={"text": TYPED})
        r2 = c.post("/api/pos/drafts/document", files={"file": ("po.docx", docx_bytes(["PO-1"]))})
        assert (r1.status_code, r1.json()["error"]) == (503, "offline") and r2.status_code == 503
        assert not (tmp_path / "po_drafts").exists() and c.tracker.session_spent == 0


# ------------------------------------------------------------------------------------------ typed text

def test_a_text_draft_prefills_the_form_suggests_the_vendor_and_writes_nothing(tmp_path):
    c = scripted(tmp_path, po_reply())
    try:
        before = counts(c)
        r = c.post("/api/pos/drafts/text", json={"text": TYPED})
        assert r.status_code == 200, r.text
        d = r.json()
        assert counts(c) == before                                        # nothing in the database, for any table
        assert d["status"] == "ok" and d["source"] == "text" and len(d["draft_id"]) == 32
        assert d["values"]["po_number"] == "PO-7788" and d["values"]["total"] == "1250.00" and d["values"]["currency"] == "USD"
        assert d["suggested_vendor_id"] == 1 and "SuperStore" in d["vendor_hint"] and d["new_vendor"] is None
        assert d["marks"]["total"]["found"] and d["marks"]["total"]["source_text"] == "total USD 1,250.00"
        assert d["marks"]["lines[0]"]["confidence"] == 0.9 and d["cost_usd"] != "0"
        assert ("total", "lines_sum") not in {(i["field"], i["code"]) for i in d["issues"]}  # the only line has no amount
        stored = json.loads((tmp_path / "po_drafts" / d["draft_id"] / "draft.json").read_text(encoding="utf-8"))
        assert stored["provenance"]["text"] == TYPED and stored["values"]["po_number"] == "PO-7788"
    finally:
        c.__exit__(None, None, None)


def test_saving_the_confirmed_draft_records_where_it_came_from(tmp_path):
    c = scripted(tmp_path, po_reply())
    try:
        d = c.post("/api/pos/drafts/text", json={"text": TYPED}).json()
        v = d["values"]
        po = {"po_number": v["po_number"], "vendor_id": d["suggested_vendor_id"], "currency": v["currency"], "total": v["total"],
              "issued_date": v["issued_date"], "lines": [{**l, "amount": "1250.00"} for l in v["lines"]]}   # the person adds the amount
        r = c.post("/api/pos", json={"po": po, "draft_id": d["draft_id"]})
        assert r.status_code == 201, r.text
        with closing(open_db(c.db_path, c.settings)) as conn:
            meta = json.loads(conn.execute("SELECT meta FROM purchase_orders WHERE id = ?", (r.json()["po_id"],)).fetchone()[0])
        assert meta["source"] == "text" and meta["draft_id"] == d["draft_id"] and meta["prompt_version"] == "po-draft-v1"
        assert meta["edited_fields"] == ["lines[0].amount"] and meta["text"] == TYPED
    finally:
        c.__exit__(None, None, None)


def test_an_unknown_vendor_is_proposed_as_new_but_not_created(tmp_path):
    c = scripted(tmp_path, po_reply(fields={"vendor_name": entry("vendor_name", "Globex Corporation", "to Globex Corporation")}))
    try:
        before = counts(c)
        d = c.post("/api/pos/drafts/text", json={"text": TYPED.replace("SuperStore", "Globex Corporation")}).json()
        assert d["suggested_vendor_id"] is None and d["new_vendor"]["name"] == "Globex Corporation"
        assert "NEW vendor" in d["vendor_hint"] and counts(c)["vendors"] == before["vendors"]
    finally:
        c.__exit__(None, None, None)


def test_a_missing_currency_is_flagged_for_the_person_to_choose(tmp_path):
    c = scripted(tmp_path, po_reply(fields={"currency": entry("currency", found=False)}))
    try:
        d = c.post("/api/pos/drafts/text", json={"text": "PO-7788 SuperStore 5 chairs total 1,250.00"}).json()
        assert d["values"]["currency"] is None and d["marks"]["currency"]["found"] is False
        assert any("never guessed" in w for w in d["warnings"]) and ("currency", "required") in {(i["field"], i["code"]) for i in d["issues"]}
    finally:
        c.__exit__(None, None, None)


def test_a_failed_draft_is_reported_and_writes_nothing(tmp_path):
    c = scripted(tmp_path, ReplayMiss("No recorded response for this request."))
    try:
        before = counts(c)
        d = c.post("/api/pos/drafts/text", json={"text": TYPED}).json()
        assert d["status"] == "failed" and d["failure"]["code"] == "replay_miss" and d["values"]["po_number"] is None
        assert counts(c) == before
    finally:
        c.__exit__(None, None, None)


def test_text_limits(tmp_path):
    c = scripted(tmp_path, po_reply())
    try:
        assert c.post("/api/pos/drafts/text", json={"text": "   "}).status_code == 422
        assert c.post("/api/pos/drafts/text", json={"text": "x" * 9000}).status_code == 422
        assert c.post("/api/pos/drafts/text", json={}).status_code == 422
    finally:
        c.__exit__(None, None, None)


# ------------------------------------------------------------------------------------------ documents

def test_a_docx_draft(tmp_path):
    c = scripted(tmp_path, po_reply())
    try:
        before = counts(c)
        r = c.post("/api/pos/drafts/document", files={"file": ("order.docx", docx_bytes([TYPED]))})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["source"] == "document" and d["status"] == "ok" and d["pages"] == [] and counts(c) == before
        stored = json.loads((tmp_path / "po_drafts" / d["draft_id"] / "draft.json").read_text(encoding="utf-8"))
        assert stored["provenance"]["file_name"] == "order.docx" and len(stored["provenance"]["sha256"]) == 64
        assert not (tmp_path / "po_drafts" / "_incoming").exists()
    finally:
        c.__exit__(None, None, None)


def test_a_pdf_draft_serves_its_pages(tmp_path):
    c = scripted(tmp_path, po_reply())
    try:
        pdf = make_native_pdf(tmp_path / "po.pdf", [[TYPED[:90], TYPED[90:]]])
        d = c.post("/api/pos/drafts/document", files={"file": ("po.pdf", pdf.read_bytes())}).json()
        assert d["pages"] == [1]
        img = c.get(f"/api/pos/drafts/{d['draft_id']}/pages/1")
        assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
        for bad in (f"/api/pos/drafts/{d['draft_id']}/pages/2", "/api/pos/drafts/..%2F..%2Fx/pages/1", f"/api/pos/drafts/{d['draft_id']}/pages/0"):
            assert c.get(bad).status_code == 404, bad
    finally:
        c.__exit__(None, None, None)


def test_refused_documents_are_clear_and_leave_nothing(tmp_path):
    c = scripted(tmp_path, po_reply())
    try:
        r = c.post("/api/pos/drafts/document", files={"file": ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600)})
        assert (r.status_code, r.json()["error"]) == (415, "legacy_office") and ".docx" in r.json()["message"]
        r = c.post("/api/pos/drafts/document", files={"file": ("x.txt", b"hello")})
        assert r.status_code == 415
        assert c.post("/api/pos/drafts/document", data={"a": "b"}).status_code == 422
        root = tmp_path / "po_drafts"
        assert not root.exists() or not any(root.iterdir())
    finally:
        c.__exit__(None, None, None)


def test_an_oversize_document_is_413(tmp_path):
    from tests.api.helpers import api_settings
    c = scripted(tmp_path, po_reply(), settings=api_settings(tmp_path, max_file_bytes=1000))
    try:
        r = c.post("/api/pos/drafts/document", files={"file": ("big.csv", b"a," * 2000)})
        assert r.status_code == 413
    finally:
        c.__exit__(None, None, None)


def test_a_canary_key_never_appears_in_draft_responses(tmp_path):
    from tests.api.helpers import api_settings
    c = scripted(tmp_path, po_reply(), ReplayMiss("miss"), settings=api_settings(tmp_path, anthropic_api_key=CANARY))
    try:
        bodies = [c.post("/api/pos/drafts/text", json={"text": TYPED}).text, c.post("/api/pos/drafts/text", json={"text": TYPED}).text]
        assert all(CANARY not in b and "CANARY" not in b for b in bodies)
    finally:
        c.__exit__(None, None, None)


def test_the_multi_po_warning_reaches_the_form(tmp_path):
    c = scripted(tmp_path, po_reply(other_pos_present="yes"))
    try:
        d = c.post("/api/pos/drafts/text", json={"text": TYPED}).json()
        assert any("more than one purchase order" in w for w in d["warnings"])
    finally:
        c.__exit__(None, None, None)
