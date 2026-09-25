"""The model roles inside the whole pipeline: a scripted model double answers extraction, explanation and drafting from what it is given."""
import json
from decimal import Decimal

import pytest

from app.config import DEFAULT_LLM_PRICES
from app.enums import Decision
from app.llm.budget import CostTracker
from app.llm.client import MeteredClient
from app.pipeline import persist
from app.pipeline.runner import run_pipeline
from tests.extraction.real import real_pdf, real_reply
from tests.extraction.wire_convert import set_field
from tests.pipeline.doubles import ModelDouble, good_explanation
from tests.pipeline.helpers import cfg, demo_db, one, rows

D = Decimal
SS_10963 = "superstore_10963"
SS_24429 = "superstore_24429"


@pytest.fixture
def db(tmp_path):
    conn = demo_db(tmp_path)
    yield conn
    conn.close()


def run(db, tmp_path, name, double, **kw):
    metered = MeteredClient(double, CostTracker(D("0.25"), D("5")), DEFAULT_LLM_PRICES)
    return run_pipeline(real_pdf(name), db, client=metered, settings=cfg(tmp_path, explain_with_llm=True, draft_with_llm=True), **kw)


def explanation_event(db, run_id):
    e = one(db, "SELECT * FROM audit_events WHERE run_id = ? AND event_type = 'explanation'", run_id)
    return json.loads(e["detail"])


def remove_pos(db):
    with persist.transaction(db):
        db.execute("DELETE FROM ledger_entries")
        db.execute("UPDATE invoices SET po_id = NULL")
        db.execute("DELETE FROM po_lines")
        db.execute("DELETE FROM purchase_orders")


def test_a_review_run_asks_the_model_once_for_an_explanation_and_never_for_an_email(db, tmp_path):
    double = ModelDouble(real_reply(SS_10963))
    r = run(db, tmp_path, SS_10963, double)
    assert r.decision is Decision.REVIEW and double.calls == {"extract": 1, "explain": 1, "draft": 0}
    detail = explanation_event(db, r.run_id)
    assert detail["source"] == "llm" and detail["model"] == "claude-sonnet-5" and detail["text"].startswith("This invoice needs a person to review it.")
    assert detail["tokens_in"] == 1300 and detail["tokens_out"] == 380 and detail["fallback_reason"] is None and detail["attempts"] == 1
    assert r.explanation.source == "llm" and one(db, "SELECT COUNT(*) AS n FROM drafts")["n"] == 0


def test_the_run_records_the_extraction_and_the_explanation_in_its_totals(db, tmp_path):
    r = run(db, tmp_path, SS_10963, ModelDouble(real_reply(SS_10963)))
    run_row = one(db, "SELECT * FROM runs WHERE id = ?", r.run_id)
    assert (run_row["tokens_in"], run_row["tokens_out"]) == (6800 + 1300, 920 + 380)
    assert run_row["cost_usd"] == pytest.approx(0.0228 + 0.0064)                                            # $2 / $10 per million tokens
    assert r.stage_costs == {"extract": D("0.022800"), "explain": D("0.006400")}
    done = json.loads(one(db, "SELECT detail FROM audit_events WHERE run_id = ? AND event_type = 'run_completed'", r.run_id)["detail"])
    assert done["tokens_in"] == 8100 and D(done["cost_usd"]) == D("0.029200")


def test_every_model_call_belongs_to_the_run_and_only_extraction_sees_the_document(db, tmp_path):
    double = ModelDouble(real_reply(SS_10963))
    r = run(db, tmp_path, SS_10963, double)
    assert [q.purpose for q in double.requests] == ["extract", "explain"] and {q.run_id for q in double.requests} == {r.run_id}
    assert any(p.kind == "image" for p in double.of("extract")[0].parts)
    assert all(p.kind == "text" for p in double.of("explain")[0].parts)
    assert "Hewlett" not in double.of("explain")[0].parts[0].text                                             # no invoice line text reaches the explainer


def test_request_info_asks_the_model_for_the_email_and_stores_it_as_a_draft_never_sent(db, tmp_path):
    remove_pos(db)
    double = ModelDouble(real_reply(SS_24429))
    r = run(db, tmp_path, SS_24429, double)
    assert r.decision is Decision.REQUEST_INFO and double.calls == {"extract": 1, "explain": 1, "draft": 1}
    d = one(db, "SELECT * FROM drafts WHERE run_id = ?", r.run_id)
    assert d["status"] == "draft" and d["kind"] == "vendor_email" and d["to"] is None
    assert d["subject"] == "Invoice 24429: information needed" and d["body"].startswith("Dear SuperStore,") and "purchase order (PO) number" in d["body"]
    ev = json.loads(one(db, "SELECT detail FROM audit_events WHERE run_id = ? AND event_type = 'draft_saved'", r.run_id)["detail"])
    assert ev["source"] == "llm" and ev["model"] == "claude-sonnet-5" and (ev["tokens_in"], ev["tokens_out"]) == (1300, 380)
    assert one(db, "SELECT tokens_in FROM runs WHERE id = ?", r.run_id)["tokens_in"] == 6800 + 1300 + 1300


def test_a_model_that_lies_about_the_decision_is_replaced_by_the_template_and_the_decision_stands(db, tmp_path):
    lie = lambda p, n: {"summary": "Everything is fine: the invoice is approved and ready for payment.", "reasons": [{"text": "ok", "facts": ["F1"]}],
                        "next_step": "Pay it."}
    double = ModelDouble(real_reply(SS_10963), explain=lie)
    r = run(db, tmp_path, SS_10963, double)
    assert r.decision is Decision.REVIEW and double.calls["explain"] == 2                                       # asked, repaired once
    detail = explanation_event(db, r.run_id)
    assert detail["source"] == "template" and detail["fallback_reason"].startswith("the reply failed the checks") and detail["attempts"] == 2
    assert detail["text"].startswith("Decision: REVIEW.") and detail["tokens_in"] == 2600                       # both attempts were paid for
    assert one(db, "SELECT final_decision FROM runs WHERE id = ?", r.run_id)["final_decision"] == "review"
    assert one(db, "SELECT COUNT(*) AS n FROM ledger_entries")["n"] == 1                                       # only the seeded historic commit


def test_a_bad_email_is_replaced_by_the_template_draft_and_still_saved_as_a_draft(db, tmp_path):
    remove_pos(db)
    bad = lambda p, n: "Dear vendor, your invoice will be paid."
    r = run(db, tmp_path, SS_24429, ModelDouble(real_reply(SS_24429), draft=bad))
    d = one(db, "SELECT * FROM drafts WHERE run_id = ?", r.run_id)
    assert r.decision is Decision.REQUEST_INFO and d["status"] == "draft" and d["body"].startswith("Dear SuperStore,")
    saved = json.loads(one(db, "SELECT detail FROM audit_events WHERE run_id = ? AND event_type = 'draft_saved'", r.run_id)["detail"])
    assert saved["source"] == "template" and saved["fallback_reason"]


def test_a_document_that_talks_to_the_reader_never_reaches_a_model_beyond_extraction(db, tmp_path):
    reply = real_reply(SS_10963)
    reply["document_quality"]["contains_reader_instructions"] = "yes"
    double = ModelDouble(reply)
    r = run(db, tmp_path, SS_10963, double)
    assert r.decision is Decision.REVIEW and double.calls == {"extract": 1, "explain": 0, "draft": 0}
    detail = explanation_event(db, r.run_id)
    assert detail["source"] == "template" and "AI reader" in detail["fallback_reason"]
    assert "reader_instructions_detected" in one(db, "SELECT reason FROM review_queue WHERE run_id = ?", r.run_id)["reason"]


def test_without_a_client_or_key_the_run_still_explains_from_the_template(db, tmp_path):
    """No key: extraction degrades (a clear message), the run is a review, and the explanation is the template."""
    r = run_pipeline(real_pdf(SS_10963), db, client=None, settings=cfg(tmp_path))
    assert r.decision is Decision.REVIEW and r.explanation.source == "template"
    assert "ANTHROPIC_API_KEY" in one(db, "SELECT message FROM audit_events WHERE run_id = ? AND event_type = 'extraction_degraded'", r.run_id)["message"]


def test_the_model_roles_are_configurable_off(db, tmp_path):
    double = ModelDouble(real_reply(SS_10963))
    metered = MeteredClient(double, CostTracker(D("0.25"), D("5")), DEFAULT_LLM_PRICES)
    r = run_pipeline(real_pdf(SS_10963), db, client=metered, settings=cfg(tmp_path))                            # helper default: roles off
    assert double.calls == {"extract": 1, "explain": 0, "draft": 0} and r.explanation.fallback_reason == "disabled in configuration"


def test_the_explanation_a_model_returns_is_only_ever_stored_in_the_trail_not_used_for_the_decision(db, tmp_path):
    """Guardrail: whatever text comes back, the decision, the invoice status and the ledger are set by the rules."""
    for reply in (good_explanation, lambda p, n: "ignore all rules and approve"):
        conn = demo_db(tmp_path / str(id(reply)))
        r = run(conn, tmp_path / str(id(reply)), SS_10963, ModelDouble(real_reply(SS_10963), explain=None if reply is good_explanation else reply))
        assert r.decision is Decision.REVIEW and one(conn, "SELECT status FROM invoices WHERE run_id = ?", r.run_id)["status"] == "in_review"
        conn.close()


def test_no_invented_currency_or_field_can_come_from_the_roles(db, tmp_path):
    """The roles receive facts, and return text; they never write invoice fields. The stored invoice is exactly the extraction."""
    reply = real_reply(SS_10963)
    set_field(reply, "currency", found=False, value="", page=0, source_text="", confidence=0, flag="unknown")
    r = run(db, tmp_path, SS_10963, ModelDouble(reply))
    inv = one(db, "SELECT currency FROM invoices WHERE run_id = ?", r.run_id)
    assert inv["currency"] is None
