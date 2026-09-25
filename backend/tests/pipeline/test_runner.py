"""The pipeline runner end to end on the demo database: what each decision writes, atomicity, escalation, and failure handling.

Real fixtures (recorded model replies and real documents) are played back through a scripted client: no live calls. The approve
path needs a PO reference that no real sample prints, so those tests use a labelled SYNTHETIC controlled variant.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.enums import Decision
from app.ingest.validate import IngestRejected
from app.llm.budget import CostTracker
from app.llm.client import MeteredClient
from app.llm.errors import LLMTimeoutError
from app.pipeline import persist
from app.pipeline.runner import run_pipeline
from tests.extraction.helpers import make_source
from tests.llm.fakes import FakeLLMClient, ok_response
from tests.extraction.real import real_reply
from tests.pipeline.helpers import (LABEL_CONTROLLED, cfg, controlled_variant_reply, demo_db, one, real_pdf, rows, run_real, scripted)

D = Decimal
SS_24429 = "superstore_24429"
SS_10963 = "superstore_10963"


@pytest.fixture
def db(tmp_path):
    conn = demo_db(tmp_path)
    yield conn
    conn.close()


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("runs", "invoices", "invoice_lines", "ledger_entries", "review_queue", "drafts", "audit_events")}


def events(conn, run_id):
    return rows(conn, "SELECT seq, stage, event_type, outcome, message, detail FROM audit_events WHERE run_id = ? ORDER BY seq", run_id)


def approve_variant(db, tmp_path):
    """SYNTHETIC controlled variant of real invoice 24429 with PO-SS-002 edited in: the only way to reach approve."""
    return run_real(db, tmp_path, SS_24429, controlled_variant_reply(SS_24429, "PO-SS-002"))


# --------------------------------------------------------------------------------------------- review

def test_a_real_invoice_with_no_po_reference_is_reviewed_and_writes_exactly_the_review_rows(db, tmp_path):
    before = counts(db)
    r = run_real(db, tmp_path, SS_10963)
    assert (r.status, r.decision, r.error) == ("completed", Decision.REVIEW, None)
    after = counts(db)
    assert after["runs"] == before["runs"] + 1 and after["invoices"] == before["invoices"] + 1 and after["invoice_lines"] == before["invoice_lines"] + 1
    assert after["review_queue"] == 1 and after["drafts"] == 0 and after["ledger_entries"] == before["ledger_entries"]      # no commit, no email

    run = one(db, "SELECT * FROM runs WHERE id = ?", r.run_id)
    assert (run["status"], run["final_decision"], run["source_file"], run["model"]) == ("completed", "review", "superstore_10963.pdf", "claude-sonnet-5")
    assert run["finished_at"] and (run["tokens_in"], run["tokens_out"]) == (6800, 920)
    inv = one(db, "SELECT * FROM invoices WHERE run_id = ?", r.run_id)
    assert (inv["decision"], inv["status"], inv["invoice_number"], inv["currency"], inv["total"], inv["subtotal"]) == (
        "review", "in_review", "10963", "USD", 533808, 514176)
    assert (inv["vendor_id"], inv["po_id"]) == (1, 1) and inv["file_hash"] == r.ctx.file_hash and inv["tax"] is None
    assert json.loads(inv["extracted"])["adjustments"][0]["kind"] == "shipping"
    q = one(db, "SELECT * FROM review_queue WHERE run_id = ?", r.run_id)
    assert q["status"] == "open" and q["resolution"] is None and q["reason"].startswith("Review: r_po_found (matched_without_reference)")
    assert one(db, "SELECT status FROM purchase_orders WHERE id = 1")["status"] == "open"                        # PO untouched
    assert {w.table for w in r.writes} == {"invoices", "invoice_lines", "review_queue"}


def test_the_explanation_is_stored_in_the_trail_and_names_the_rule_that_held_the_invoice(db, tmp_path):
    r = run_real(db, tmp_path, SS_10963)
    e = next(x for x in events(db, r.run_id) if x["event_type"] == "explanation")
    detail = json.loads(e["detail"])
    assert detail["source"] == "template" and detail["model"] is None and detail["text"].startswith("Decision: REVIEW.")
    assert "matched_without_reference" in detail["text"] and detail["cited_facts"] and e["message"] == "review: r_po_found matched_without_reference"
    assert r.explanation.one_line == e["message"]


# ---------------------------------------------------------------------------------------- audit trail

def test_the_audit_trail_is_complete_ordered_and_gapless(db, tmp_path):
    r = run_real(db, tmp_path, SS_10963)
    ev = events(db, r.run_id)
    assert [e["seq"] for e in ev] == list(range(len(ev)))
    order = []
    for e in ev:
        if not order or order[-1] != e["stage"]:
            order.append(e["stage"])
    assert order == ["ingest", "pipeline", "extract", "match", "validate", "decide", "explain", "act", "pipeline"]
    kinds = [e["event_type"] for e in ev]
    assert kinds.count("rule_evaluated") == 13 and kinds.count("engine_floor") == 2 and kinds.count("severity_aggregated") == 1
    assert kinds.index("run_started") < kinds.index("llm_call") < kinds.index("vendor_resolved") < kinds.index("decision_made") \
        < kinds.index("explanation") < kinds.index("invoice_saved") < kinds.index("review_queued") < kinds.index("run_completed")
    assert kinds[-1] == "run_completed" and 30 <= len(ev) <= 60
    last = json.loads(ev[-1]["detail"])
    assert last["decision"] == "review" and last["tokens_in"] == 6800


def test_every_stage_result_is_available_to_the_caller(db, tmp_path):
    r = run_real(db, tmp_path, SS_10963)
    assert set(r.stages) == {"ingest", "extract", "match", "validate", "decide"} and r.digest.decision is Decision.REVIEW


def test_the_runs_recorded_decision_matches_the_invoice_and_the_engine(db, tmp_path):
    r = run_real(db, tmp_path, SS_10963)
    assert one(db, "SELECT final_decision FROM runs WHERE id = ?", r.run_id)["final_decision"] == one(db, "SELECT decision FROM invoices WHERE run_id = ?", r.run_id)["decision"] \
        == r.ctx.decision.value == r.decision.value


def test_costs_and_tokens_are_summed_into_the_run(db, tmp_path):
    from app.config import DEFAULT_LLM_PRICES

    client = MeteredClient(scripted(real_reply(SS_10963)), CostTracker(D("0.25"), D("5")), DEFAULT_LLM_PRICES)
    r = run_pipeline(real_pdf(SS_10963), db, client=client, settings=cfg(tmp_path))
    run = one(db, "SELECT * FROM runs WHERE id = ?", r.run_id)
    assert run["cost_usd"] == pytest.approx(0.0228) and (run["tokens_in"], run["tokens_out"]) == (6800, 920)
    assert r.cost_usd == D("0.022800") and r.stage_costs["extract"] == D("0.022800")


# ---------------------------------------------------------------------------------------------- approve

def test_controlled_variant_synthetic_approve_commits_to_the_ledger_and_updates_the_po(db, tmp_path):
    """SYNTHETIC: real invoice 24429 with PO-SS-002 edited in. Label: %s""" % LABEL_CONTROLLED
    before = counts(db)
    r = approve_variant(db, tmp_path)
    assert (r.status, r.decision) == ("completed", Decision.APPROVE) and r.po_number == "PO-SS-002"
    assert r.po_balance == (D("2500.00"), D("729.39"))
    after = counts(db)
    assert after["ledger_entries"] == before["ledger_entries"] + 1 and after["review_queue"] == 0 and after["drafts"] == 0
    entry = one(db, "SELECT * FROM ledger_entries ORDER BY id DESC LIMIT 1")
    inv = one(db, "SELECT * FROM invoices WHERE run_id = ?", r.run_id)
    assert (entry["po_id"], entry["invoice_id"], entry["amount"], entry["type"]) == (2, inv["id"], 177061, "commit")
    assert (inv["decision"], inv["status"], inv["po_id"]) == ("approve", "approved", 2)
    po = one(db, "SELECT * FROM purchase_orders WHERE id = 2")
    assert po["status"] == "partially_billed" and po["total_amount"] == 250000
    from app.db.queries import get_po_balance
    assert get_po_balance(db, 2) == D("729.39") and "balance" not in po                                      # derived, never stored
    ev = {e["event_type"]: json.loads(e["detail"]) for e in events(db, r.run_id)}
    assert ev["ledger_committed"]["balance_before"] == "2500.00" and ev["ledger_committed"]["balance_after"] == "729.39"
    assert ev["ready_for_payment"]["invoice_id"] == inv["id"] and ev["po_status_updated"]["status"] == "partially_billed"
    assert one(db, "SELECT final_decision FROM runs WHERE id = ?", r.run_id)["final_decision"] == "approve"


def test_a_commit_that_uses_up_the_balance_makes_the_po_fully_billed(db, tmp_path):
    with persist.transaction(db):
        db.execute("UPDATE purchase_orders SET total_amount = 177061 WHERE id = 2")                          # exactly the invoice total
    r = approve_variant(db, tmp_path)
    assert r.decision is Decision.APPROVE and r.po_balance == (D("1770.61"), D("0.00"))
    assert one(db, "SELECT status FROM purchase_orders WHERE id = 2")["status"] == "fully_billed"


def test_the_same_file_again_is_a_duplicate_reject_and_never_commits_twice(db, tmp_path):
    first = approve_variant(db, tmp_path / "a")
    ledger = counts(db)["ledger_entries"]
    second = run_real(db, tmp_path / "b", SS_24429, controlled_variant_reply(SS_24429, "PO-SS-002"))
    assert first.decision is Decision.APPROVE and second.decision is Decision.REJECT
    assert counts(db)["ledger_entries"] == ledger
    from app.db.queries import get_po_balance
    assert get_po_balance(db, 2) == D("729.39")
    draft = one(db, "SELECT * FROM drafts WHERE run_id = ?", second.run_id)
    assert draft["kind"] == "vendor_email" and draft["status"] == "draft" and draft["subject"] == "Invoice 24429: unable to process"
    assert "already received this exact file" in draft["body"]
    assert one(db, "SELECT status FROM invoices WHERE run_id = ?", second.run_id)["status"] == "rejected"


# ---------------------------------------------------------------------------------------- request_info

def remove_pos(conn):
    with persist.transaction(conn):
        conn.execute("DELETE FROM ledger_entries")
        conn.execute("UPDATE invoices SET po_id = NULL")
        conn.execute("DELETE FROM po_lines")
        conn.execute("DELETE FROM purchase_orders")


def test_no_po_at_all_asks_the_vendor_with_a_draft_that_is_never_sent(db, tmp_path):
    remove_pos(db)
    r = run_real(db, tmp_path, SS_24429)
    assert r.decision is Decision.REQUEST_INFO
    d = one(db, "SELECT * FROM drafts WHERE run_id = ?", r.run_id)
    assert (d["kind"], d["status"], d["to"], d["subject"]) == ("vendor_email", "draft", None, "Invoice 24429: information needed")
    assert d["body"].startswith("Dear SuperStore,") and "which purchase order (PO) number" in d["body"] and "Accounts Payable" in d["body"]
    assert counts(db)["review_queue"] == 0 and one(db, "SELECT status FROM invoices WHERE run_id = ?", r.run_id)["status"] == "awaiting_info"
    assert db.execute("SELECT COUNT(*) FROM drafts WHERE status <> 'draft'").fetchone()[0] == 0
    saved = next(e for e in events(db, r.run_id) if e["event_type"] == "draft_saved")
    assert "nothing is sent" in saved["message"].lower() and json.loads(saved["detail"])["source"] == "template"


def test_a_password_protected_file_asks_the_vendor_to_send_it_again(db, tmp_path):
    client = scripted("unused")
    r = run_pipeline(make_source(tmp_path / "docs", "locked"), db, client=client, settings=cfg(tmp_path))
    assert r.decision is Decision.REQUEST_INFO and client.requests == []                                      # no model call, $0
    d = one(db, "SELECT * FROM drafts WHERE run_id = ?", r.run_id)
    assert "password-protected" in d["body"] and "Please send it again" in d["body"] and d["body"].startswith("Dear Sir or Madam,")
    assert one(db, "SELECT cost_usd FROM runs WHERE id = ?", r.run_id)["cost_usd"] == 0


# ------------------------------------------------------------------------------------------------ reject

def test_a_blocked_vendor_is_rejected_with_an_internal_note_and_no_vendor_email(db, tmp_path):
    with persist.transaction(db):
        db.execute("UPDATE vendors SET status = 'blocked' WHERE id = 1")
    r = run_real(db, tmp_path, SS_10963)
    assert r.decision is Decision.REJECT
    d = one(db, "SELECT * FROM drafts WHERE run_id = ?", r.run_id)
    assert d["kind"] == "notification" and d["status"] == "draft" and d["body"].startswith("Internal note (no email was drafted to the vendor).")
    assert db.execute("SELECT COUNT(*) FROM drafts WHERE kind = 'vendor_email'").fetchone()[0] == 0


# ------------------------------------------------------------------------------- failures and escalation

def test_a_file_that_is_not_accepted_creates_no_run(db, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("not an invoice", encoding="utf-8")
    before = counts(db)
    with pytest.raises(IngestRejected):
        run_pipeline(bad, db, client=scripted("x"), settings=cfg(tmp_path))
    assert counts(db) == before


def test_a_system_side_extraction_failure_is_a_review_never_an_email_to_the_vendor(db, tmp_path):
    r = run_pipeline(real_pdf(SS_10963), db, client=FakeLLMClient(LLMTimeoutError("timed out")), settings=cfg(tmp_path))
    assert (r.status, r.decision) == ("completed", Decision.REVIEW)
    assert counts(db)["drafts"] == 0 and counts(db)["review_queue"] == 1
    inv = one(db, "SELECT * FROM invoices WHERE run_id = ?", r.run_id)
    assert inv["total"] is None and inv["invoice_number"] is None and inv["status"] == "in_review"
    q = one(db, "SELECT reason FROM review_queue")
    assert "extraction_degraded" in q["reason"]
    assert any(e["event_type"] == "extraction_degraded" for e in events(db, r.run_id))


def test_a_failure_in_the_act_stage_rolls_everything_back_and_marks_the_run_failed(db, tmp_path, monkeypatch):
    remove_pos(db)
    before = counts(db)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(persist, "save_draft", boom)
    r = run_real(db, tmp_path, SS_24429)
    assert r.status == "failed" and r.decision is None and "disk on fire" in r.error
    after = counts(db)
    assert after["invoices"] == before["invoices"] and after["drafts"] == 0 and after["ledger_entries"] == before["ledger_entries"]
    assert after["invoice_lines"] == before["invoice_lines"] and after["review_queue"] == before["review_queue"]
    run = one(db, "SELECT * FROM runs WHERE id = ?", r.run_id)
    assert run["status"] == "failed" and run["final_decision"] is None and run["finished_at"]
    ev = events(db, r.run_id)
    assert ev[-1]["event_type"] == "pipeline_error" and ev[-1]["outcome"] == "fail"
    assert any(e["event_type"] == "decision_made" for e in ev)                     # what happened before the failure is kept
    assert not any(e["event_type"] in ("invoice_saved", "explanation", "run_completed") for e in ev)      # the act stage left nothing behind
    assert not db.in_transaction


def test_an_approve_whose_records_changed_during_the_run_is_escalated_to_review(db, tmp_path):
    """SYNTHETIC controlled variant. Another process records an invoice while the run is explaining: the approve must not commit."""
    def meddling_explain(digest, **kw):
        from app.pipeline.explain import template_explanation

        with persist.transaction(db):
            db.execute("INSERT INTO invoices (vendor_id, invoice_number, total, status) VALUES (1, 'OTHER-1', 100, 'pending')")
        return template_explanation(digest)

    before = counts(db)["ledger_entries"]
    r = run_real(db, tmp_path, SS_24429, controlled_variant_reply(SS_24429, "PO-SS-002"), explain_fn=meddling_explain)
    assert r.decision is Decision.REVIEW and "other invoices were recorded" in r.downgraded_reason
    assert counts(db)["ledger_entries"] == before and one(db, "SELECT final_decision FROM runs WHERE id = ?", r.run_id)["final_decision"] == "review"
    assert one(db, "SELECT status FROM purchase_orders WHERE id = 2")["status"] == "open"
    assert "Approval withheld: other invoices were recorded" in r.explanation.text and r.explanation.source == "template"
    esc = next(e for e in events(db, r.run_id) if e["event_type"] == "decision_escalated")
    assert json.loads(esc["detail"]) == {"from": "approve", "to": "review", "reason": r.downgraded_reason}
    assert one(db, "SELECT reason FROM review_queue")["reason"].startswith("Review: Approval withheld")
    assert one(db, "SELECT decision, status FROM invoices WHERE run_id = ?", r.run_id) == {"decision": "review", "status": "in_review"}


def test_a_po_balance_change_during_the_run_also_withholds_the_approve(db, tmp_path):
    def meddling_explain(digest, **kw):
        from app.pipeline.explain import template_explanation

        with persist.transaction(db):
            db.execute("INSERT INTO invoices (id, vendor_id, invoice_number, total, status) VALUES (900, 1, 'OTHER-2', 100, 'approved')")
            db.execute("DELETE FROM invoices WHERE id = 900")                       # the count is unchanged again, only the ledger differs
            db.execute("INSERT INTO invoices (id, vendor_id, invoice_number, total, status) VALUES (901, 1, 'OTHER-3', 100, 'approved')")
            db.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (2, 901, 100, 'commit')")
        return template_explanation(digest)

    r = run_real(db, tmp_path, SS_24429, controlled_variant_reply(SS_24429, "PO-SS-002"), explain_fn=meddling_explain)
    assert r.decision is Decision.REVIEW and r.downgraded_reason


def test_nothing_the_explainer_or_drafter_returns_can_change_the_decision(db, tmp_path):
    from app.pipeline.explain import Explanation

    def lying_explain(digest, **kw):
        return Explanation(text="Decision: APPROVE. Everything is fine.", one_line="approve: fine", reasons=(), next_step="pay", source="llm")

    r = run_real(db, tmp_path, SS_10963, explain_fn=lying_explain)
    assert r.decision is Decision.REVIEW and one(db, "SELECT final_decision FROM runs WHERE id = ?", r.run_id)["final_decision"] == "review"
    assert counts(db)["ledger_entries"] == 1                                       # only the seeded historic commit


def test_no_vendor_or_file_name_is_hard_coded_in_the_application():
    import app

    root = Path(app.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for needle in ("superstore", "electronics mart", "iq electronics", "10963", "24429", "superstore_", "wooten", "zettner"):
            if needle in text:
                offenders.append((path.name, needle))
    assert offenders == []
