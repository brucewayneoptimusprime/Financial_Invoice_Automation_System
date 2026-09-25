"""PO save path (PO integration stage 2): validation, the single writer, provenance, and matching staying unchanged."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.api.worker import open_db
from app.po import drafts as po_drafts
from app.po import store as po_store
from app.pipeline.runner import run_pipeline
from tests.api.helpers import api, api_settings
from tests.extraction.real import real_pdf, real_reply
from tests.pipeline.helpers import scripted

API_DIR = Path(__file__).resolve().parents[2] / "app"


def good(**over):
    po = {"po_number": "PO-NEW-001", "vendor_id": 1, "currency": "USD", "total": "1,250.00", "issued_date": "2026-01-15",
          "lines": [{"description": "Office chairs", "quantity": "5", "unit_price": "250.00", "amount": "1250.00"}]}
    po.update(over)
    return po


def counts(c):
    with closing(open_db(c.db_path, c.settings)) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("vendors", "purchase_orders", "po_lines")}


def issue_codes(body):
    return {(i["field"], i["code"]) for i in body["issues"]}


# ----------------------------------------------------------------------------------------------------- save

def test_a_valid_po_is_saved_open_with_exact_cents_and_manual_provenance(tmp_path):
    with api(tmp_path) as c:
        r = c.post("/api/pos", json={"po": good()})
        assert r.status_code == 201, r.text
        po_id = r.json()["po_id"]
        with closing(open_db(c.db_path, c.settings)) as conn:
            row = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
            lines = conn.execute("SELECT * FROM po_lines WHERE po_id = ?", (po_id,)).fetchall()
        assert (row["po_number"], row["vendor_id"], row["currency"], row["total_amount"], row["issued_date"], row["status"]) == (
            "PO-NEW-001", 1, "USD", 125000, "2026-01-15", "open")
        meta = json.loads(row["meta"])
        assert meta["source"] == "manual" and meta["entered_at"].endswith("Z")
        assert [(l["line_no"], l["description"], l["quantity"], l["unit_price"], l["amount"]) for l in lines] == [
            (1, "Office chairs", "5", "250.00", 125000)]


def test_optional_fields_may_be_empty(tmp_path):
    with api(tmp_path) as c:
        r = c.post("/api/pos", json={"po": good(issued_date=None, lines=[])})
        assert r.status_code == 201, r.text


@pytest.mark.parametrize("over, field, code", [
    ({"po_number": ""}, "po_number", "required"),
    ({"po_number": "PO-SS-001"}, "po_number", "duplicate"),
    ({"vendor_id": None}, "vendor", "required"),
    ({"vendor_id": 999}, "vendor", "unknown"),
    ({"currency": ""}, "currency", "required"),
    ({"currency": "US$"}, "currency", "invalid"),
    ({"currency": "JPY"}, "currency", "unsupported"),
    ({"total": ""}, "total", "required"),
    ({"total": "abc"}, "total", "not_a_number"),
    ({"total": "10.005"}, "total", "too_many_decimals"),
    ({"total": "-5.00"}, "total", "negative"),
    ({"issued_date": "15/01/2026"}, "issued_date", "invalid"),
    ({"lines": [{}]}, "lines[0]", "empty"),
    ({"lines": [{"description": "x", "quantity": "two"}]}, "lines[0].quantity", "not_a_number"),
    ({"lines": [{"description": "x", "amount": "1.001"}]}, "lines[0].amount", "too_many_decimals"),
])
def test_each_blocking_problem_names_its_field_and_writes_nothing(tmp_path, over, field, code):
    with api(tmp_path) as c:
        before = counts(c)
        r = c.post("/api/pos", json={"po": good(**over)})
        assert r.status_code == (409 if code == "duplicate" else 422), r.text
        assert (field, code) in issue_codes(r.json())
        assert counts(c) == before
        v = c.post("/api/pos/validate", json={"po": good(**over)}).json()
        assert v["can_save"] is False and (field, code) in issue_codes(v)


@pytest.mark.parametrize("over, field, code", [
    ({"po_number": "po ss 001"}, "po_number", "similar"),
    ({"total": "1300.00"}, "total", "lines_sum"),
    ({"lines": [{"description": "x", "quantity": "5", "unit_price": "250.00", "amount": "1200.00"}], "total": "1200.00"},
     "lines[0].amount", "line_math"),
    ({"total": "0"}, "total", "zero"),
    ({"total": "20000000.00", "lines": []}, "total", "large"),
    ({"issued_date": "2999-01-01"}, "issued_date", "future"),
])
def test_warnings_are_reported_and_may_still_be_saved(tmp_path, over, field, code):
    with api(tmp_path) as c:
        v = c.post("/api/pos/validate", json={"po": good(**over)}).json()
        assert v["can_save"] is True and (field, code) in issue_codes(v)
        r = c.post("/api/pos", json={"po": good(**over)})
        assert r.status_code == 201 and (field, code) in {(w["field"], w["code"]) for w in r.json()["warnings"]}


def test_validate_reports_the_sum_of_the_lines_but_never_applies_it(tmp_path):
    lines = [{"description": "a", "amount": "100.10"}, {"description": "b", "amount": "1,000.05"}]
    with api(tmp_path) as c:
        v = c.post("/api/pos/validate", json={"po": good(total="5", lines=lines)}).json()
    assert v["lines_sum"] == "1100.15" and ("total", "lines_sum") in issue_codes(v)


def test_the_duplicate_race_is_a_409_and_writes_nothing(tmp_path, monkeypatch):
    real = po_store.save_po

    def racing(conn, parsed, **kw):                                         # another request saved the same number just before
        conn.execute("INSERT INTO purchase_orders (po_number, vendor_id, currency, total_amount, status) VALUES (?, 1, 'USD', 1, 'open')",
                     (parsed.po_number,))
        conn.commit()
        return real(conn, parsed, **kw)

    import app.api.routes_po as routes_po
    monkeypatch.setattr(routes_po, "save_po", racing)
    with api(tmp_path) as c:
        before = counts(c)
        r = c.post("/api/pos", json={"po": good()})
        assert r.status_code == 409
        assert counts(c)["purchase_orders"] == before["purchase_orders"] + 1 and counts(c)["po_lines"] == before["po_lines"]


# ----------------------------------------------------------------------------------------------------- vendors

def test_a_new_vendor_is_created_with_status_new_in_the_same_transaction(tmp_path):
    with api(tmp_path) as c:
        r = c.post("/api/pos", json={"po": good(vendor_id=None), "new_vendor": {"name": "Acme Widgets Ltd", "tax_id": "GB123456789"}})
        assert r.status_code == 201, r.text
        assert ("vendor", "new_vendor") in {(w["field"], w["code"]) for w in r.json()["warnings"]}
        with closing(open_db(c.db_path, c.settings)) as conn:
            v = conn.execute("SELECT * FROM vendors WHERE id = ?", (r.json()["vendor_id"],)).fetchone()
        assert (v["name"], v["status"], v["tax_id"]) == ("Acme Widgets Ltd", "new", "GB123456789")


def test_a_request_cannot_ask_for_an_approved_vendor(tmp_path):
    with api(tmp_path) as c:
        before = counts(c)
        r = c.post("/api/pos", json={"po": good(vendor_id=None), "new_vendor": {"name": "Acme", "status": "approved"}})
        assert r.status_code == 422 and counts(c) == before


def test_status_cannot_be_set_on_entry(tmp_path):
    with api(tmp_path) as c:
        assert c.post("/api/pos", json={"po": {**good(), "status": "closed"}}).status_code == 422


def test_a_failure_after_the_vendor_insert_rolls_back_both(tmp_path, monkeypatch):
    with api(tmp_path) as c:
        before = counts(c)
        bad_line = {"description": "x", "amount": "1.00"}
        r0 = c.post("/api/pos/validate", json={"po": good(vendor_id=None, lines=[bad_line], total="1.00"), "new_vendor": {"name": "Rollback Co"}})
        assert r0.json()["can_save"]
        original = po_store.transaction

        def failing_lines(conn, **kw):                                     # break the po_lines insert inside the transaction
            conn.execute("CREATE TEMP TRIGGER IF NOT EXISTS boom BEFORE INSERT ON po_lines BEGIN SELECT RAISE(ABORT, 'boom'); END")
            return original(conn, **kw)
        monkeypatch.setattr(po_store, "transaction", failing_lines)
        with pytest.raises(sqlite3.IntegrityError):
            c.post("/api/pos", json={"po": good(vendor_id=None, lines=[bad_line], total="1.00"), "new_vendor": {"name": "Rollback Co"}})
        assert counts(c) == before


def test_similar_vendor_names_and_tax_ids_are_warned(tmp_path):
    with api(tmp_path) as c:
        v = c.post("/api/pos/validate", json={"po": good(vendor_id=None), "new_vendor": {"name": "Super Store", "tax_id": "36AAFCE1683D1ZT"}}).json()
    assert {("vendor", "similar_vendor"), ("vendor", "same_tax_id")} <= issue_codes(v)


def test_choosing_both_an_existing_and_a_new_vendor_is_refused(tmp_path):
    with api(tmp_path) as c:
        v = c.post("/api/pos/validate", json={"po": good(), "new_vendor": {"name": "X"}}).json()
    assert ("vendor", "ambiguous") in issue_codes(v)


# ----------------------------------------------------------------------------------------------------- drafts / provenance

def test_saving_from_a_draft_records_source_and_the_fields_the_person_changed(tmp_path):
    settings = api_settings(tmp_path)
    po_drafts.write_draft(settings, "d" * 32, {"source": "text", "suggested_vendor_id": 1,
                                                "provenance": {"model": "claude-sonnet-5", "cost_usd": "0.0101", "text": "5 chairs..."},
                                                "values": {"po_number": "PO-NEW-001", "vendor_name": "SuperStore", "currency": "USD",
                                                           "total": "1250", "issued_date": None,
                                                           "lines": [{"description": "Office chairs", "quantity": "5", "unit_price": "250",
                                                                      "amount": "1250.00"}]}})
    with api(tmp_path, settings=settings) as c:
        r = c.post("/api/pos", json={"po": good(), "draft_id": "d" * 32})
        assert r.status_code == 201, r.text
        with closing(open_db(c.db_path, c.settings)) as conn:
            meta = json.loads(conn.execute("SELECT meta FROM purchase_orders WHERE id = ?", (r.json()["po_id"],)).fetchone()[0])
    assert meta["source"] == "text" and meta["draft_id"] == "d" * 32 and meta["model"] == "claude-sonnet-5"
    assert meta["edited_fields"] == ["issued_date"]                        # 1250 == 1,250.00; only the date was added by the person


def test_an_unknown_draft_is_refused(tmp_path):
    with api(tmp_path) as c:
        before = counts(c)
        assert c.post("/api/pos", json={"po": good(), "draft_id": "e" * 32}).status_code == 422
        assert c.post("/api/pos", json={"po": good(), "draft_id": "../../x"}).status_code == 422
        assert counts(c) == before


# ----------------------------------------------------------------------------------------------------- structure

def test_save_po_is_called_from_exactly_one_route_and_only_store_writes_pos():
    callers = [p for p in API_DIR.rglob("*.py") if "save_po(" in p.read_text(encoding="utf-8") and p.name != "store.py"]
    assert [p.name for p in callers] == ["routes_po.py"]
    src = (API_DIR / "api" / "routes_po.py").read_text(encoding="utf-8")
    assert src.count("save_po(") == 1
    writers = [p for p in API_DIR.rglob("*.py")
               if any(k in p.read_text(encoding="utf-8") for k in ("INSERT INTO purchase_orders", "INSERT INTO po_lines"))]
    assert sorted(p.relative_to(API_DIR).as_posix() for p in writers) == ["db/seed.py", "po/store.py"]


# ----------------------------------------------------------------------------------------------------- matching unchanged

def test_a_po_entered_through_the_form_is_matched_like_the_seeded_one(tmp_path):
    """Enter PO-SS-001's twin under a new number for a new copy of the same vendor... the existing engine picks it up."""
    with api(tmp_path) as c:
        with closing(open_db(c.db_path, c.settings)) as conn:
            seeded = conn.execute("SELECT * FROM purchase_orders WHERE po_number = 'PO-SS-001'").fetchone()
            line = conn.execute("SELECT * FROM po_lines WHERE po_id = ?", (seeded["id"],)).fetchone()
            conn.execute("UPDATE purchase_orders SET status = 'closed' WHERE id = ?", (seeded["id"],))   # take the seeded twin out
            conn.commit()
        r = c.post("/api/pos", json={"po": {"po_number": "PO-FORM-001", "vendor_id": seeded["vendor_id"], "currency": "USD",
                                            "total": "6000.00", "lines": [{"description": line["description"], "quantity": line["quantity"],
                                                                           "unit_price": line["unit_price"], "amount": "5141.76"}]}})
        assert r.status_code == 201, r.text
        with closing(open_db(c.db_path, c.settings)) as conn:
            res = run_pipeline(real_pdf("superstore_10963"), conn, client=scripted(real_reply("superstore_10963")), settings=c.settings)
    scores = {cand.po_number: cand.score for cand in res.ctx.candidates}
    assert scores["PO-FORM-001"] == pytest.approx(scores["PO-SS-001"])     # identical facts, identical score
    assert res.ctx.match_status.value in ("matched", "ambiguous")          # two identical POs: the engine does not guess
