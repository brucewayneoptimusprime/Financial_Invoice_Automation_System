"""The six real sample invoices (five SuperStore PDFs and the scanned IQ Electronics photo) through the ENTIRE pipeline against the
demo dataset: real documents, the model replies recorded live (extract-v4) played back, and a scripted double for the explainer and
drafter (their real-model behaviour is not measured yet). No live calls.

Expected results and why are pinned here and reported in STATUS.md. The approve path needs a PO reference that none of the six
prints, so it is exercised only by a clearly labelled SYNTHETIC controlled variant, never presented as one of the six.
"""
import json
from decimal import Decimal

import pytest

from app.config import DEFAULT_LLM_PRICES
from app.enums import Decision
from app.llm.budget import CostTracker
from app.llm.client import MeteredClient
from app.pipeline.runner import run_pipeline
from tests.extraction.real import REAL, real_pdf
from tests.pipeline.doubles import ModelDouble
from tests.pipeline.helpers import LABEL_CONTROLLED, cfg, demo_db, one, rows

D = Decimal
SUPERSTORE = ["superstore_10963", "superstore_24429", "superstore_14021", "superstore_6459", "superstore_14130"]
SIX = [*SUPERSTORE, "iq_electronics"]
PO_OF = {"superstore_10963": "PO-SS-001", "superstore_24429": "PO-SS-002", "superstore_14021": "PO-SS-003",
         "superstore_6459": "PO-SS-004", "superstore_14130": "PO-SS-005"}
INTERNAL_WORDS = ("r_", "severity", "score", "threshold", "engine", "floor", "blocked", "vendor status")


def v4_reply(name):
    return json.loads((REAL / f"{name}.v4.reply.json").read_text(encoding="utf-8"))


def run_six(db, tmp_path, name, *, reply=None, **kw):
    double = ModelDouble(v4_reply(name) if reply is None else reply)
    client = MeteredClient(double, CostTracker(D("0.25"), D("5")), DEFAULT_LLM_PRICES)
    result = run_pipeline(real_pdf(name), db, client=client, settings=cfg(tmp_path / name, explain_with_llm=True, draft_with_llm=True), **kw)
    return result, double


@pytest.fixture(scope="module")
def all_six(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("six")
    db = demo_db(tmp)
    results = {}
    for name in SIX:
        results[name] = run_six(db, tmp, name)
    yield db, results
    db.close()


# ------------------------------------------------------------------------------------- the five SuperStore PDFs

@pytest.mark.parametrize("name", SUPERSTORE)
def test_a_superstore_invoice_is_matched_to_its_po_and_held_for_review_only_because_it_prints_no_po_number(all_six, name):
    db, results = all_six
    r, double = results[name]
    assert (r.status, r.decision) == ("completed", Decision.REVIEW) and r.ctx.match_status.value == "matched"
    assert r.ctx.matched_po.po_number == PO_OF[name] and r.ctx.matched_vendor.method == "exact_name"
    assert [(f.rule_id, f.outcome_key, f.severity) for f in r.digest.triggered] == [("r_po_found", "matched_without_reference", 1)]
    assert r.ctx.extracted.po_reference.value is None and r.ctx.extracted.tax.value is None
    q = one(db, "SELECT * FROM review_queue WHERE run_id = ?", r.run_id)
    assert q["status"] == "open" and q["reason"].startswith("Review: r_po_found (matched_without_reference)")
    assert r.draft is None and double.calls == {"extract": 1, "explain": 1, "draft": 0}
    assert r.explanation.source == "llm" and r.explanation.one_line == "review: r_po_found matched_without_reference"


@pytest.mark.parametrize("name", SUPERSTORE)
def test_every_other_check_passes_on_a_superstore_invoice(all_six, name):
    r, _ = all_six[1][name]
    outcomes = {x.rule_id: x.outcome.value for x in r.ctx.rule_results}
    assert {k: v for k, v in outcomes.items() if v != "pass"} == {"r_po_found": "flag"}
    assert len(outcomes) == 15 and outcomes["engine_floor"] == "pass"


def test_the_five_review_items_are_in_the_queue_and_no_money_moved(all_six):
    db, results = all_six
    assert one(db, "SELECT COUNT(*) AS n FROM review_queue WHERE status = 'open'")["n"] == 5
    assert one(db, "SELECT COUNT(*) AS n FROM ledger_entries")["n"] == 1                                    # only the seeded historic commit
    from app.db.queries import get_po_balance
    assert get_po_balance(db, 5) == D("7500.00") and get_po_balance(db, 1) == D("6000.00")                 # PO balances untouched
    assert one(db, "SELECT COUNT(*) AS n FROM drafts WHERE run_id IN (SELECT run_id FROM invoices WHERE decision = 'review')")["n"] == 0


# ------------------------------------------------------------------------------------------ the IQ scan

def test_the_iq_scan_as_recorded_live_with_extract_v4_asks_the_vendor_because_the_currency_was_not_returned(all_six):
    """The v4 reply has tax null (fixed), but no currency although 'Rupees ... only' is printed: extract-v5 targets exactly this and
    is NOT re-recorded yet. Until then this invoice is a request_info."""
    db, results = all_six
    r, double = results["iq_electronics"]
    ex = r.ctx.extracted
    assert r.decision is Decision.REQUEST_INFO and ex.currency.value is None
    assert ex.tax.value is None and ex.tax.included_in_total is True and ex.vendor_tax_id.value == "36AAFCE1683D1ZT"
    assert r.ctx.matched_vendor.method == "tax_id" and r.ctx.matched_vendor.vendor_id == 2
    assert {x.rule_id: x.outcome.value for x in r.ctx.rule_results}["r_arithmetic"] == "pass"
    triggered = {(f.rule_id, f.outcome_key) for f in r.digest.triggered}
    assert {("r_required_fields", "missing"), ("r_po_found", "no_reference"), ("r_extraction_confidence", "low_confidence")} <= triggered
    assert r.ctx.match_status.value == "low_score"                                                          # no currency: the amount cannot be compared
    assert double.calls == {"extract": 1, "explain": 1, "draft": 1}


def test_the_iq_email_asks_for_the_currency_the_date_and_the_po_and_reveals_nothing_internal(all_six):
    db, results = all_six
    r, _ = results["iq_electronics"]
    d = one(db, "SELECT * FROM drafts WHERE run_id = ?", r.run_id)
    assert (d["kind"], d["status"], d["to"]) == ("vendor_email", "draft", None) and d["subject"] == "Invoice 1801/24/S-3641: information needed"
    body = d["body"]
    assert "the currency" in body and "the invoice date" in body and "purchase order (PO) number" in body and body.endswith("Accounts Payable")
    assert not any(w in body.lower() for w in INTERNAL_WORDS)
    assert r.draft.source == "llm"


def test_the_iq_invoice_is_saved_awaiting_information_with_a_null_tax_and_currency(all_six):
    db, results = all_six
    r, _ = results["iq_electronics"]
    inv = one(db, "SELECT * FROM invoices WHERE run_id = ?", r.run_id)
    assert (inv["status"], inv["decision"], inv["vendor_id"], inv["po_id"]) == ("awaiting_info", "request_info", 2, None)
    assert inv["currency"] is None and inv["tax"] is None and inv["total"] == 490000 and inv["invoice_number"] == "1801/24/S-3641"
    assert json.loads(inv["extracted"])["tax"]["included_in_total"] is True


# ---------------------------------------------------------------------------------- across all six runs

def test_the_database_holds_one_completed_run_per_invoice_with_a_gapless_trail(all_six):
    db, results = all_six
    runs = rows(db, "SELECT * FROM runs ORDER BY started_at, id")
    assert len(runs) == 6 and all(r["status"] == "completed" and r["finished_at"] for r in runs)
    for name, (res, _) in results.items():
        seqs = [e["seq"] for e in rows(db, "SELECT seq FROM audit_events WHERE run_id = ? ORDER BY seq", res.run_id)]
        assert seqs == list(range(len(seqs))) and 30 <= len(seqs) <= 60, name
        assert one(db, "SELECT final_decision FROM runs WHERE id = ?", res.run_id)["final_decision"] == res.decision.value \
            == one(db, "SELECT decision FROM invoices WHERE run_id = ?", res.run_id)["decision"]


def test_costs_are_recorded_per_run_and_add_up(all_six):
    db, results = all_six
    stored = sum(one(db, "SELECT cost_usd FROM runs WHERE id = ?", res.run_id)["cost_usd"] for res, _ in results.values())
    assert stored == pytest.approx(float(sum(r.cost_usd for r, _ in results.values())), abs=1e-6) and stored > 0.16
    for name, (res, _) in results.items():
        roles = 1 + (1 if name == "iq_electronics" else 0)                                                # explanation, plus a draft for the IQ scan
        assert res.tokens_in == res.ctx.extraction_meta.tokens_in + 1300 * roles and set(res.stage_costs) >= {"extract", "explain"}


def test_every_draft_is_a_draft_and_nothing_was_sent(all_six):
    db, _ = all_six
    assert one(db, "SELECT COUNT(*) AS n FROM drafts WHERE status <> 'draft'")["n"] == 0 and one(db, "SELECT COUNT(*) AS n FROM drafts")["n"] == 1


# --------------------------------------------------- CONTROLLED VARIANTS (SYNTHETIC: never one of the six real samples)

def test_controlled_variant_synthetic_a_po_number_edited_into_a_real_invoice_is_approved_and_committed(tmp_path):
    """SYNTHETIC controlled variant of real invoice 24429 with PO-SS-002 edited into the recorded reply: the only route to approve."""
    from tests.pipeline.helpers import controlled_variant_reply

    db = demo_db(tmp_path)
    reply = controlled_variant_reply("superstore_24429", "PO-SS-002")
    reply.update({k: v for k, v in v4_reply("superstore_24429").items() if k in ("line_items", "adjustments", "document_quality")})
    r, double = run_six(db, tmp_path, "superstore_24429", reply=reply)
    assert r.decision is Decision.APPROVE and r.po_balance == (D("2500.00"), D("729.39"))
    assert "SYNTHETIC CONTROLLED VARIANT" in r.ctx.extracted.extraction_notes
    assert one(db, "SELECT status FROM invoices WHERE run_id = ?", r.run_id)["status"] == "approved"
    assert r.explanation.source == "llm" and double.calls == {"extract": 1, "explain": 1, "draft": 0}
    db.close()


def test_controlled_variant_synthetic_is_labelled_wherever_it_is_used():
    import pathlib

    text = pathlib.Path(__file__).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("def test_controlled_variant"):
            assert "synthetic" in line
    from tests.pipeline import helpers

    assert "SYNTHETIC" in helpers.controlled_variant_reply.__doc__ and "CONTROLLED VARIANT" in LABEL_CONTROLLED
    assert "SYNTHETIC CONTROLLED VARIANT" in helpers.controlled_variant_reply("superstore_24429", "PO-X")["extraction_notes"]
