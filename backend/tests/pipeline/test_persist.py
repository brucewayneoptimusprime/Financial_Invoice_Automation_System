"""Stage 1 persistence: runs, audit events (monotonic seq), invoices and lines."""
import json
from datetime import date
from decimal import Decimal

import pytest

from app.enums import Decision, InvoiceStatus, Outcome
from app.models import RunContext
from app.models.run import VendorMatch
from app.models.audit import AuditEvent
from app.pipeline.persist import STATUS_FOR_DECISION, AuditWriter, finish_run, save_invoice, start_run, transaction
from tests.factories import make_extracted

D = Decimal


def ctx_for(extracted=None, run_id="run-1", **kw):
    return RunContext(run_id=run_id, source_file="a.pdf", file_hash="h" * 64, extracted=extracted or make_extracted(), **kw)


def event(n=1, stage="validate", **kw):
    return AuditEvent(stage=stage, event_type=f"e{n}", outcome=Outcome.INFO, message=f"m{n}", **kw)


@pytest.fixture
def run(conn):
    with transaction(conn):
        start_run(conn, "run-1", "a.pdf")
    return conn


# ------------------------------------------------------------------------------------------------- runs

def test_start_run_records_a_running_run(conn):
    with transaction(conn):
        start_run(conn, "run-1", "a.pdf")
    row = conn.execute("SELECT * FROM runs WHERE id = 'run-1'").fetchone()
    assert (row["status"], row["source_file"], row["final_decision"], row["finished_at"]) == ("running", "a.pdf", None, None)
    assert row["started_at"] and row["tokens_in"] == 0 and row["cost_usd"] == 0


def test_finish_run_sets_the_outcome_once_and_a_run_can_never_be_finished_twice(run):
    with transaction(run):
        finish_run(run, "run-1", status="completed", decision=Decision.REVIEW, tokens_in=5000, tokens_out=900,
                   cost_usd=D("0.023214"), model="claude-sonnet-5")
    row = run.execute("SELECT * FROM runs WHERE id = 'run-1'").fetchone()
    assert (row["status"], row["final_decision"], row["tokens_in"], row["tokens_out"], row["model"]) == (
        "completed", "review", 5000, 900, "claude-sonnet-5")
    assert row["cost_usd"] == pytest.approx(0.023214) and row["finished_at"]
    with pytest.raises(RuntimeError, match="not open"):
        with transaction(run):
            finish_run(run, "run-1", status="completed", decision=Decision.APPROVE, tokens_in=0, tokens_out=0, cost_usd=0, model=None)
    assert run.execute("SELECT final_decision FROM runs").fetchone()[0] == "review"                 # the original decision stands


def test_a_failed_run_has_no_decision(run):
    with transaction(run):
        finish_run(run, "run-1", status="failed", decision=None, tokens_in=0, tokens_out=0, cost_usd=0, model=None)
    row = run.execute("SELECT status, final_decision FROM runs").fetchone()
    assert (row["status"], row["final_decision"]) == ("failed", None)


def test_finishing_an_unknown_run_fails(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            finish_run(conn, "nope", status="completed", decision=Decision.APPROVE, tokens_in=0, tokens_out=0, cost_usd=0, model=None)


# ------------------------------------------------------------------------------------------------ audit

def test_audit_events_get_a_monotonic_seq_in_order_across_writes(run):
    writer = AuditWriter(run, "run-1")
    with transaction(run):
        writer.write([event(1, "ingest"), event(2, "extract")])
    with transaction(run):
        writer.write([event(3, "match")])
        writer.emit("validate", "rule_evaluated", Outcome.FLAG, "flagged", {"severity": 1}, rule_id="r_x")
    rows = run.execute("SELECT seq, stage, event_type, rule_id, outcome FROM audit_events WHERE run_id = 'run-1' ORDER BY seq").fetchall()
    assert [r["seq"] for r in rows] == [0, 1, 2, 3]
    assert [r["stage"] for r in rows] == ["ingest", "extract", "match", "validate"]
    assert (rows[3]["rule_id"], rows[3]["outcome"]) == ("r_x", "flag")


def test_a_new_writer_continues_the_sequence_and_runs_are_independent(run):
    with transaction(run):
        AuditWriter(run, "run-1").write([event(1), event(2)])
        start_run(run, "run-2", "b.pdf")
    with transaction(run):
        AuditWriter(run, "run-1").write([event(3)])
        AuditWriter(run, "run-2").write([event(1)])
    seqs = lambda rid: [r[0] for r in run.execute("SELECT seq FROM audit_events WHERE run_id = ? ORDER BY seq", (rid,))]
    assert seqs("run-1") == [0, 1, 2] and seqs("run-2") == [0]


def test_audit_detail_is_stored_as_json_with_exact_decimals_and_iso_dates(run):
    with transaction(run):
        AuditWriter(run, "run-1").write([event(1, detail={"amount": D("1234.50"), "when": date(2026, 3, 14), "ids": {3, 1, 2}, "n": None})])
    detail = json.loads(run.execute("SELECT detail FROM audit_events").fetchone()[0])
    assert detail == {"amount": "1234.50", "when": "2026-03-14", "ids": [1, 2, 3], "n": None}


def test_events_from_the_engine_persist_unchanged(run):
    """Every event type the M1/M2 stages emit fits the table (outcome CHECK, JSON detail)."""
    from tests.engine.real import pipeline
    from tests.factories import make_facts

    ctx, res, (match, validate, decide) = pipeline(make_extracted(), make_facts())
    with transaction(run):
        n = AuditWriter(run, "run-1").write([*match.events, *validate.events, *decide.events])
    assert n == run.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == len(match.events) + len(validate.events) + len(decide.events)
    assert run.execute("SELECT COUNT(*) FROM audit_events WHERE event_type = 'rule_evaluated'").fetchone()[0] == 13
    assert run.execute("SELECT COUNT(*) FROM audit_events WHERE event_type = 'engine_floor'").fetchone()[0] == 2


def test_a_failed_transaction_rolls_back_every_write(run):
    with pytest.raises(ValueError):
        with transaction(run):
            AuditWriter(run, "run-1").write([event(1)])
            raise ValueError("boom")
    assert run.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    assert not run.in_transaction


def test_transactions_do_not_nest(run):
    with transaction(run):
        with pytest.raises(RuntimeError, match="already open"):
            with transaction(run):
                pass


# ---------------------------------------------------------------------------------------------- invoices

def test_an_invoice_and_its_lines_are_saved_in_minor_units(run):
    ctx = ctx_for()
    ctx.matched_vendor = VendorMatch(vendor_id=None, method="none")
    with transaction(run):
        saved = save_invoice(run, ctx, Decision.APPROVE)
    inv = run.execute("SELECT * FROM invoices WHERE id = ?", (saved.invoice_id,)).fetchone()
    assert (inv["run_id"], inv["invoice_number"], inv["invoice_date"], inv["currency"]) == ("run-1", "INV-1001", "2026-03-14", "USD")
    assert (inv["subtotal"], inv["tax"], inv["total"]) == (100000, 10000, 110000)
    assert (inv["decision"], inv["status"], inv["source_file"], inv["file_hash"]) == ("approve", "approved", "a.pdf", "h" * 64)
    assert run.execute("SELECT typeof(total) FROM invoices").fetchone()[0] == "integer"
    lines = run.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no", (saved.invoice_id,)).fetchall()
    assert [(r["line_no"], r["description"], r["quantity"], r["unit_price"], r["amount"]) for r in lines] == [
        (1, "Standard widget", "10", "60.00", 60000), (2, "Premium gadget", "5", "80.00", 40000)]
    assert saved.lines == 2 and saved.notes == []


def test_the_extraction_json_is_kept_whole_including_adjustments(run):
    ex = make_extracted(adjustments=[{"kind": "shipping", "amount": "25.00", "printed_amount": "25.00", "confidence": 0.9}])
    with transaction(run):
        save_invoice(run, ctx_for(ex), Decision.REVIEW)
    stored = json.loads(run.execute("SELECT extracted FROM invoices").fetchone()[0])
    assert stored["adjustments"][0]["kind"] == "shipping" and stored["adjustments"][0]["amount"] == "25.00"
    assert stored["total"]["value"] == "1100.00" and stored["vendor_name"]["value"] == "Vendor Alpha Ltd"


@pytest.mark.parametrize("decision,status", [(Decision.APPROVE, "approved"), (Decision.REVIEW, "in_review"),
                                             (Decision.REQUEST_INFO, "awaiting_info"), (Decision.REJECT, "rejected")])
def test_the_invoice_status_follows_the_decision(run, decision, status):
    with transaction(run):
        save_invoice(run, ctx_for(), decision)
    row = run.execute("SELECT decision, status FROM invoices").fetchone()
    assert (row["decision"], row["status"]) == (decision.value, status)
    assert STATUS_FOR_DECISION[decision] is InvoiceStatus(status)


def test_every_decision_has_a_status_and_none_is_pending():
    assert set(STATUS_FOR_DECISION) == set(Decision) and InvoiceStatus.PENDING not in STATUS_FOR_DECISION.values()


def test_missing_values_are_null_and_an_all_null_extraction_still_saves(run):
    from app.models.extraction import ExtractedInvoice

    with transaction(run):
        saved = save_invoice(run, ctx_for(ExtractedInvoice()), Decision.REVIEW)
    row = run.execute("SELECT * FROM invoices WHERE id = ?", (saved.invoice_id,)).fetchone()
    assert all(row[c] is None for c in ("invoice_number", "invoice_date", "currency", "subtotal", "tax", "total", "vendor_id", "po_id"))
    assert saved.lines == 0 and run.execute("SELECT COUNT(*) FROM invoice_lines").fetchone()[0] == 0


def test_a_sub_cent_amount_is_stored_as_null_and_reported_never_rounded(run):
    ex = make_extracted(total="10.005", line_items=[{"description": "x", "quantity": "1", "unit_price": "10.005", "amount": "10.005",
                                                       "confidence": 0.9}])
    with transaction(run):
        saved = save_invoice(run, ctx_for(ex), Decision.REVIEW)
    assert run.execute("SELECT total FROM invoices").fetchone()[0] is None
    assert tuple(run.execute("SELECT amount, unit_price FROM invoice_lines").fetchone()) == (None, "10.005")
    assert any("total 10.005" in n for n in saved.notes) and any("line 1 amount" in n for n in saved.notes)


def test_vendor_and_po_links_come_from_the_match_and_ambiguity_leaves_the_vendor_null(seeded_conn):
    from app.models.run import POCandidate

    with transaction(seeded_conn):
        start_run(seeded_conn, "r", "a.pdf")
    ctx = ctx_for(run_id="r")
    ctx.matched_vendor = VendorMatch(vendor_id=1, score=1.0, method="exact_name")
    ctx.matched_po = POCandidate(po_id=1, po_number="PH-PO-0001", score=0.9)
    with transaction(seeded_conn):
        save_invoice(seeded_conn, ctx, Decision.APPROVE)
    row = seeded_conn.execute("SELECT vendor_id, po_id FROM invoices WHERE run_id = 'r'").fetchone()
    assert (row["vendor_id"], row["po_id"]) == (1, 1)
    with transaction(seeded_conn):
        start_run(seeded_conn, "r2", "b.pdf")
    ctx2 = ctx_for(run_id="r2")
    ctx2.matched_vendor = VendorMatch(vendor_id=1, score=0.9, method="fuzzy", ambiguous=True, candidate_vendor_ids=[1, 2])
    with transaction(seeded_conn):
        save_invoice(seeded_conn, ctx2, Decision.REVIEW)
    row = seeded_conn.execute("SELECT vendor_id, po_id FROM invoices WHERE run_id = 'r2'").fetchone()
    assert (row["vendor_id"], row["po_id"]) == (None, None)


def test_a_saved_invoice_is_seen_by_the_next_runs_duplicate_check(seeded_conn):
    """The whole point of saving review, request_info and reject invoices too."""
    from app.engine.loader import load_facts

    with transaction(seeded_conn):
        start_run(seeded_conn, "r", "a.pdf")
    ctx = ctx_for(run_id="r")
    ctx.matched_vendor = VendorMatch(vendor_id=1, score=1.0, method="exact_name")
    with transaction(seeded_conn):
        save_invoice(seeded_conn, ctx, Decision.REVIEW)
    facts = load_facts(seeded_conn, current_run_id="other")
    assert any(p.run_id == "r" and p.invoice_number == "INV-1001" and p.file_hash == "h" * 64 for p in facts.prior_invoices)
    assert not any(p.run_id == "r" for p in load_facts(seeded_conn, current_run_id="r").prior_invoices)     # a run never matches itself
