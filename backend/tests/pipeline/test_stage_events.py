"""M4 stage timing events: every stage is bracketed by stage_started / stage_completed with a duration and a whitelisted summary.

Played back through scripted clients on the demo database; no live calls.
"""
import json

import pytest

from app.enums import Decision
from app.pipeline import runner as runner_mod
from app.pipeline.summary import HEADER_FIELDS, STAGES
from app.db.connection import connect
from tests.extraction.real import real_reply
from tests.pipeline.helpers import controlled_variant_reply, demo_db, rows, run_real

SS_10963 = "superstore_10963"
SS_24429 = "superstore_24429"


@pytest.fixture
def db(tmp_path):
    conn = demo_db(tmp_path)
    yield conn
    conn.close()


def timing(conn, run_id):
    ev = rows(conn, "SELECT seq, stage, event_type, outcome, detail FROM audit_events WHERE run_id = ? ORDER BY seq", run_id)
    return ev, [(e["event_type"], json.loads(e["detail"])) for e in ev if e["event_type"] in ("stage_started", "stage_completed")]


def test_every_stage_has_a_started_then_completed_pair_in_pipeline_order(db, tmp_path):
    r = run_real(db, tmp_path, SS_10963)
    ev, pairs = timing(db, r.run_id)
    expected = [(kind, stage) for stage in STAGES for kind in ("stage_started", "stage_completed")]
    assert [(k, d["stage"]) for k, d in pairs] == expected
    for kind, d in pairs:
        if kind == "stage_completed":
            assert d["status"] in ("ok", "flagged", "failed") and isinstance(d["duration_ms"], int) and d["duration_ms"] >= 0
    assert all(e["stage"] == "pipeline" for e in ev if e["event_type"] in ("stage_started", "stage_completed"))


def test_stage_events_sit_around_the_stages_own_events(db, tmp_path):
    r = run_real(db, tmp_path, SS_10963)
    ev, _ = timing(db, r.run_id)
    kinds = [(e["event_type"], json.loads(e["detail"]).get("stage")) for e in ev]
    types = [e["event_type"] for e in ev]
    assert kinds.index(("stage_started", "extract")) < types.index("llm_call") < kinds.index(("stage_completed", "extract"))
    assert kinds.index(("stage_started", "validate")) < types.index("rule_evaluated")
    assert types.index("severity_aggregated") < kinds.index(("stage_completed", "validate"))
    assert kinds.index(("stage_completed", "act")) < types.index("run_completed") == len(types) - 1
    assert types[0] == "run_started" and kinds[1] == ("stage_started", "ingest")


def test_stage_started_is_committed_before_the_stage_runs(db, tmp_path, monkeypatch):
    seen = {}
    db_path = db.execute("PRAGMA database_list").fetchone()[2]
    real_match = runner_mod.run_match_stage

    def slow_match(ctx):
        other = connect(db_path)                                          # a second connection sees only committed rows
        try:
            got = [json.loads(r[0])["stage"] for r in other.execute(
                "SELECT detail FROM audit_events WHERE run_id = ? AND event_type = 'stage_started'", (ctx.run_id,))]
            done = [json.loads(r[0])["stage"] for r in other.execute(
                "SELECT detail FROM audit_events WHERE run_id = ? AND event_type = 'stage_completed'", (ctx.run_id,))]
        finally:
            other.close()
        seen["started"], seen["completed"] = got, done
        return real_match(ctx)

    monkeypatch.setattr(runner_mod, "run_match_stage", slow_match)
    r = run_real(db, tmp_path, SS_10963)
    assert r.status == "completed"
    assert seen["started"] == ["ingest", "extract", "match"] and seen["completed"] == ["ingest", "extract"]


def test_summaries_carry_the_key_outputs(db, tmp_path):
    r = run_real(db, tmp_path, SS_10963)
    _, pairs = timing(db, r.run_id)
    s = {d["stage"]: d["summary"] for k, d in pairs if k == "stage_completed"}
    assert s["ingest"]["media_type"] == "application/pdf" and s["ingest"]["pages_processed"] == 1
    assert s["extract"]["fields_total"] == len(HEADER_FIELDS) and 0 < s["extract"]["fields_found"] <= len(HEADER_FIELDS)
    assert s["extract"]["tokens_in"] == 6800 and s["extract"]["cost_usd"] and s["extract"]["model"] == "claude-sonnet-5"
    assert (s["match"]["match_status"], s["match"]["matched_po"], s["match"]["vendor"]) == ("matched", "PO-SS-001", "SuperStore")
    assert s["validate"]["results"] == 15 and s["validate"]["counts"]["flag"] >= 1 and "r_po_found" in s["validate"]["triggered"]
    assert s["decide"]["decision"] == "review"
    assert s["explain"]["source"] == "template"
    assert s["act"] == {"decision": "review", "rows_written": {"invoices": 1, "invoice_lines": 1, "review_queue": 1},
                        "downgraded_to_review": False, "draft_kind": None}


def test_approve_act_summary_counts_the_ledger_rows(db, tmp_path):
    r = run_real(db, tmp_path, SS_24429, controlled_variant_reply(SS_24429, "PO-SS-002"))   # SYNTHETIC controlled variant
    assert r.decision is Decision.APPROVE
    _, pairs = timing(db, r.run_id)
    act = next(d for k, d in pairs if k == "stage_completed" and d["stage"] == "act")
    assert act["status"] == "ok" and act["summary"]["rows_written"] == {"invoices": 1, "invoice_lines": 1, "ledger_entries": 1,
                                                                        "purchase_orders": 1}


def test_summaries_contain_no_page_text_invoice_text_or_model_free_text(db, tmp_path):
    reply = real_reply(SS_10963)
    r = run_real(db, tmp_path, SS_10963, reply)
    _, pairs = timing(db, r.run_id)
    blob = json.dumps([d for _, d in pairs])
    ex = r.ctx.extracted
    texts = [t for t in (ex.vendor_address.value, ex.vendor_address.source_text, ex.extraction_notes, ex.line_items[0].description,
                         ex.total.source_text) if t]  # the vendor NAME may appear: the record's name
    assert len(texts) >= 3
    for text in texts:
        assert text not in blob
    assert "sk-ant" not in blob and "base64" not in blob and "explanation" not in blob


def test_a_run_id_passed_in_is_used_and_a_bad_one_is_refused(db, tmp_path):
    r = run_real(db, tmp_path, SS_10963, run_id="chosen_by_api_0001")
    assert r.run_id == "chosen_by_api_0001" and db.execute("SELECT COUNT(*) FROM runs WHERE id = 'chosen_by_api_0001'").fetchone()[0] == 1
    with pytest.raises(ValueError):
        run_real(db, tmp_path, SS_10963, run_id="../escape")
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_source_name_replaces_the_temporary_upload_name(db, tmp_path):
    r = run_real(db, tmp_path, SS_10963, source_name="my invoice.pdf")
    assert db.execute("SELECT source_file FROM runs WHERE id = ?", (r.run_id,)).fetchone()[0] == "my invoice.pdf"


def test_a_failed_act_stage_leaves_no_act_completed_event(db, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(runner_mod.persist, "save_invoice", boom)
    r = run_real(db, tmp_path, SS_10963)
    assert r.status == "failed"
    ev, pairs = timing(db, r.run_id)
    assert ("stage_started", "act") in [(k, d["stage"]) for k, d in pairs]
    assert ("stage_completed", "act") not in [(k, d["stage"]) for k, d in pairs]
    assert ev[-1]["event_type"] == "pipeline_error"
