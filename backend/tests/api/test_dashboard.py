"""GET /api/dashboard (landing dashboard, stage 1): counts, current outcomes, reviews, spend, PO totals per currency, recent lists.
Read-only; built from existing tables and query functions."""
import json
from contextlib import closing

import pytest

from app.api.worker import open_db
from tests.api.helpers import SS_10963, SS_24429, ScriptedRuns, api, run_and_wait, upload
from tests.extraction.real import REAL
from tests.extraction.wire_convert import set_field
from tests.pipeline.helpers import controlled_variant_reply
from tests.po.helpers import TYPED, po_reply

TABLES = ("runs", "invoices", "invoice_lines", "ledger_entries", "po_consumption", "review_queue", "drafts", "audit_events",
          "purchase_orders", "po_lines", "vendors", "rules", "settings", "invoice_line_matches")


def snapshot_counts(c):
    with closing(open_db(c.db_path, c.settings)) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def test_an_empty_history_shows_zeros_and_the_seeded_pos(tmp_path):
    with api(tmp_path) as c:
        d = c.get("/api/dashboard").json()
    assert d["runs"] == {"processed": 0, "failed": 0, "running": 0, "by_decision": {"approve": 0, "review": 0, "request_info": 0, "reject": 0}}
    assert d["review"] == {"open_count": 0, "oldest_open": []} and d["recent_runs"] == []
    assert d["spend"] == {"invoice_runs_usd": "0.000000", "po_drafts_usd": "0.000000", "total_usd": "0.000000", "runs_counted": 0, "drafts_counted": 0}
    assert d["pos"]["count"] == 6 and d["pos"]["by_status"] == {"open": 5, "partially_billed": 1, "fully_billed": 0, "closed": 0}
    assert d["pos"]["currencies"] == [
        {"currency": "INR", "count": 1, "total_value": "5000.00", "consumed": "0.00", "balance": "5000.00", "consumed_without_line": "0.00"},
        {"currency": "USD", "count": 5, "total_value": "39500.00", "consumed": "1500.00", "balance": "38000.00", "consumed_without_line": "1500.00"}]


def v4(name):
    return json.loads((REAL / f"{name}.v4.reply.json").read_text(encoding="utf-8"))


def many_runs():
    no_number = v4("superstore_6459")
    set_field(no_number, "invoice_number", found=False, value="", page=0, source_text="", confidence=0.0)
    return ScriptedRuns({SS_24429: controlled_variant_reply(SS_24429, "PO-SS-002"),   # SYNTHETIC approve variant
                         "superstore_14021": v4("superstore_14021"),                    # real, review
                         "superstore_6459": no_number})                                 # SYNTHETIC: invoice number removed


def test_counts_after_several_runs(tmp_path, monkeypatch):
    with api(tmp_path, run_fn=many_runs()) as c:
        run_and_wait(c, SS_10963)                                            # review
        run_and_wait(c, "superstore_14021")                                  # review
        run_and_wait(c, SS_24429)                                            # approve (synthetic variant)
        run_and_wait(c, "superstore_6459")                                   # request_info (synthetic)
        from app.pipeline import runner
        monkeypatch.setattr(runner.persist, "save_invoice", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        run_and_wait(c, "superstore_14130")                                  # failed
        d = c.get("/api/dashboard").json()
    assert (d["runs"]["processed"], d["runs"]["failed"]) == (4, 1)
    assert d["runs"]["by_decision"] == {"approve": 1, "review": 2, "request_info": 1, "reject": 0}
    assert d["outcomes"] == {"approved": 1, "in_review": 2, "awaiting_info": 1, "rejected": 0, "pending": 0}
    assert d["review"]["open_count"] == 2 and len(d["review"]["oldest_open"]) == 2
    assert d["recent_runs"][0]["source_file"] == "superstore_14130.pdf" and d["recent_runs"][0]["status"] == "failed"
    assert len(d["recent_runs"]) == 5
    approved = next(r for r in d["recent_runs"] if r["final_decision"] == "approve")
    assert (approved["vendor"], approved["invoice_number"], approved["invoice_total"], approved["invoice_status"]) == (
        "SuperStore", "24429", "1770.61", "approved")
    usd = next(x for x in d["pos"]["currencies"] if x["currency"] == "USD")
    assert usd["consumed"] == "3270.61" and d["pos"]["by_status"]["partially_billed"] == 2


def test_approving_a_review_moves_the_current_outcome_not_the_system_decision(tmp_path):
    with api(tmp_path) as c:
        run_and_wait(c, SS_10963)
        before = c.get("/api/dashboard").json()
        iid = before["review"]["oldest_open"][0]["id"]
        token = c.get(f"/api/review-queue/{iid}").json()["approve"]["state_token"]
        assert c.post(f"/api/review-queue/{iid}/approve", json={"confirm": True, "state_token": token}).status_code == 200
        after = c.get("/api/dashboard").json()
    assert before["runs"]["by_decision"] == after["runs"]["by_decision"]            # the system decision never changes
    assert (before["outcomes"]["in_review"], after["outcomes"]["in_review"], after["outcomes"]["approved"]) == (1, 0, 1)
    assert (before["review"]["open_count"], after["review"]["open_count"]) == (1, 0)
    usd = next(x for x in after["pos"]["currencies"] if x["currency"] == "USD")
    assert (usd["consumed"], usd["consumed_without_line"]) == ("6838.08", "1696.32")   # 1,500 history + 5,338.08; 1,500 + 196.32 remainder


def test_the_po_block_matches_the_po_endpoint(tmp_path):
    with api(tmp_path) as c:
        d = c.get("/api/dashboard").json()["pos"]
        pos = c.get("/api/pos").json()["pos"]
    from decimal import Decimal
    for cur in d["currencies"]:
        mine = [p for p in pos if p["currency"] == cur["currency"]]
        assert Decimal(cur["total_value"]) == sum(Decimal(p["total"]) for p in mine)
        assert Decimal(cur["balance"]) == sum(Decimal(p["balance"]) for p in mine) and cur["count"] == len(mine)


def test_spend_adds_invoice_runs_and_po_drafts(tmp_path):
    from tests.api.helpers import build_app
    from tests.po.helpers import client_for
    from fastapi.testclient import TestClient
    app, worker, db, settings = build_app(tmp_path, mode="replay", inner_client=client_for(po_reply()))
    with TestClient(app) as c:
        assert c.post("/api/pos/drafts/text", json={"text": TYPED}).json()["status"] == "ok"      # a scripted PO draft with a cost
        with closing(open_db(db, settings)) as conn:
            with conn:
                conn.execute("INSERT INTO runs (id, source_file, status, cost_usd) VALUES ('r1', 'a.pdf', 'completed', 0.0236)")
                conn.execute("INSERT INTO runs (id, source_file, status, cost_usd) VALUES ('r2', 'b.pdf', 'failed', 0.0100)")
        s = c.get("/api/dashboard").json()["spend"]
    draft = json.loads(next((settings.po_drafts_dir).glob("*/draft.json")).read_text(encoding="utf-8"))["provenance"]["cost_usd"]
    assert (s["invoice_runs_usd"], s["runs_counted"], s["drafts_counted"]) == ("0.033600", 2, 1)
    from decimal import Decimal
    assert Decimal(s["po_drafts_usd"]) == Decimal(draft) and Decimal(s["total_usd"]) == Decimal("0.0336") + Decimal(draft)


def test_it_is_read_only(tmp_path):
    with api(tmp_path) as c:
        run_and_wait(c, SS_10963)
        before = snapshot_counts(c)
        for _ in range(3):
            c.get("/api/dashboard")
        assert snapshot_counts(c) == before


@pytest.mark.parametrize("query", ["recent=0", "recent=51", "review=0", "review=abc"])
def test_list_lengths_are_validated(tmp_path, query):
    with api(tmp_path) as c:
        assert c.get(f"/api/dashboard?{query}").status_code == 422


def test_list_lengths_are_respected(tmp_path):
    with api(tmp_path, run_fn=ScriptedRuns({"superstore_14021": v4("superstore_14021")})) as c:
        ids = [run_and_wait(c, name) for name in (SS_10963, SS_24429, "superstore_14021")]    # three different documents: three reviews
        d = c.get("/api/dashboard?recent=2&review=1").json()
    assert [r["id"] for r in d["recent_runs"]] == ids[::-1][:2]
    assert d["review"]["open_count"] == 3 and [i["run_id"] for i in d["review"]["oldest_open"]] == ids[:1]


def test_the_runs_list_filters_by_system_decision(tmp_path):
    with api(tmp_path, run_fn=many_runs()) as c:
        for name in (SS_10963, SS_24429, "superstore_6459"):
            run_and_wait(c, name)
        by = {d: [r["source_file"] for r in c.get(f"/api/runs?decision={d}").json()["runs"]] for d in ("approve", "review", "request_info", "reject")}
        assert c.get("/api/runs?decision=bogus").status_code == 422
        assert len(c.get("/api/runs").json()["runs"]) == 3
    assert by == {"approve": ["superstore_24429.pdf"], "review": ["superstore_10963.pdf"], "request_info": ["superstore_6459.pdf"], "reject": []}


def test_the_po_list_filters_by_currency(tmp_path):
    with api(tmp_path) as c:
        inr = c.get("/api/pos?currency=inr").json()["pos"]
        usd = c.get("/api/pos?currency=USD&status=partially_billed").json()["pos"]
        none = c.get("/api/pos?currency=EUR").json()["pos"]
    assert [p["po_number"] for p in inr] == ["PO-IQ-2025-001"] and [p["po_number"] for p in usd] == ["PO-SS-005"] and none == []
