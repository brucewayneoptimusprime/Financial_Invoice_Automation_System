"""Stage 4 engine touch points: extraction failures, truncation and reader instructions reach the engine floor;
a system-side failure is a review, a vendor-side one is a request for information."""
import pytest

from app.engine.engine import run_decide_stage, run_validate_stage
from app.enums import Decision, Outcome
from app.models.extraction_meta import ExtractionMeta, IngestInfo
from tests.engine.fakes import FAKE_REGISTRY, rule
from tests.engine.real import BUILTIN, ev_builtin
from tests.factories import make_ctx, make_extracted, make_facts


def floor_of(ctx, rules=()):
    """Run with every rule disabled; the floor alone must do the work."""
    off = [rule(f"off{i}", "fake_pass", enabled=False) for i in range(2)] + list(rules)
    stage = run_validate_stage(ctx, off, registry=FAKE_REGISTRY)
    return stage, next(r for r in ctx.rule_results if r.rule_id == "engine_floor")


def codes(floor):
    return [r["code"] for r in floor.detail["reasons"]]


def ingest(**kw) -> IngestInfo:
    base = dict(media_type="application/pdf", size_bytes=1, sha256="a" * 64, original_name="x.pdf", run_dir="r", original_path="r/o.pdf")
    return IngestInfo(**{**base, **kw})


def ctx_with(meta=None, info=None, **kw):
    ctx = make_ctx(**kw)
    ctx.extraction_meta, ctx.ingest = meta, info
    return ctx


# ------------------------------------------------------------------------------------------ the three floors

def test_a_clean_run_with_clean_metadata_is_not_floored():
    ctx = ctx_with(ExtractionMeta(), ingest(pages_total=1, pages_processed=1))
    _, floor = floor_of(ctx)
    assert floor.outcome is Outcome.PASS and codes(floor) == []


@pytest.mark.parametrize("kind", ["system_side", "vendor_side"])
def test_a_degraded_extraction_floors_the_run_whatever_the_rules_say(kind):
    meta = ExtractionMeta(degraded=True, failure_kind=kind, failure_code="timeout", failure_reason="x")
    stage, floor = floor_of(ctx_with(meta))
    assert "extraction_degraded" in codes(floor) and floor.severity == 1 and stage.outputs["decision"] == "review"
    reason = next(r for r in floor.detail["reasons"] if r["code"] == "extraction_degraded")
    assert reason["failure_kind"] == kind and reason["failure_code"] == "timeout"


def test_truncated_documents_are_floored_from_the_ingest_or_the_meta():
    from_ingest = ctx_with(ExtractionMeta(), ingest(pages_total=14, pages_processed=10, truncated=True))
    _, floor = floor_of(from_ingest)
    assert "pages_truncated" in codes(floor) and "10 of 14" in floor.message
    _, floor = floor_of(ctx_with(ExtractionMeta(truncated=True)))
    assert "pages_truncated" in codes(floor)


def test_reader_instructions_are_floored_from_the_scan_or_from_the_models_self_report():
    scan = ctx_with(ExtractionMeta(injection_suspected=True, injection_evidence=["page 1: ...ignore previous instructions..."]))
    stage, floor = floor_of(scan)
    assert "reader_instructions_detected" in codes(floor) and stage.outputs["decision"] == "review"
    assert floor.detail["reasons"][-1]["evidence"] == ["page 1: ...ignore previous instructions..."]
    reported = ctx_with(ExtractionMeta(), extracted=make_extracted(document_quality={"type": "native", "issues": [],
                                                                                     "contains_reader_instructions": True}))
    assert "reader_instructions_detected" in codes(floor_of(reported)[1])
    denied = ctx_with(ExtractionMeta(), extracted=make_extracted(document_quality={"type": "native", "issues": [],
                                                                                   "contains_reader_instructions": False}))
    assert codes(floor_of(denied)[1]) == []


def test_the_floors_hold_with_every_builtin_rule_disabled_and_no_metadata_is_not_an_error():
    ctx = ctx_with(None, None)                                    # M1-style context without M2 metadata
    _, floor = floor_of(ctx)
    assert floor.outcome is Outcome.PASS


def test_all_three_reasons_can_be_reported_together():
    meta = ExtractionMeta(degraded=True, failure_kind="system_side", failure_code="timeout", truncated=True, injection_suspected=True)
    _, floor = floor_of(ctx_with(meta))
    assert codes(floor) == ["extraction_degraded", "pages_truncated", "reader_instructions_detected"]


# -------------------------------------------------------------- system-side vs vendor-side failures end to end

def decide(ctx):
    run_validate_stage(ctx, list(BUILTIN.values()))
    run_decide_stage(ctx)
    return {r.rule_id: r for r in ctx.rule_results}


def null_invoice():
    return make_extracted(vendor_name=None, invoice_number=None, invoice_date=None, currency=None, po_reference=None,
                          subtotal=None, tax=None, total=None, line_items=[], document_type=None)


def test_a_system_side_failure_is_a_review_not_a_request_for_information():
    meta = ExtractionMeta(degraded=True, failure_kind="system_side", failure_code="timeout", failure_reason="timed out")
    ctx = ctx_with(meta, extracted=null_invoice(), matched=False, vendor_id=None)
    res = decide(ctx)
    assert res["r_required_fields"].outcome is Outcome.INFO and res["r_required_fields"].detail["reason"] == "extraction_failed_system_side"
    assert "our side" in res["r_required_fields"].message and "timeout" in res["r_required_fields"].message
    assert res["r_po_found"].outcome is Outcome.INFO and res["r_po_found"].detail["reason"] == "extraction_failed_system_side"
    assert res["r_vendor_status"].outcome is Outcome.INFO                       # nothing here asks the vendor for anything
    assert {k for k, v in res.items() if v.severity >= 2} == set()
    assert ctx.decision is Decision.REVIEW


def test_a_vendor_side_failure_still_asks_the_vendor():
    meta = ExtractionMeta(degraded=True, failure_kind="vendor_side", failure_code="password_protected", failure_reason="locked")
    ctx = ctx_with(meta, extracted=null_invoice(), matched=False, vendor_id=None)
    res = decide(ctx)
    assert res["r_required_fields"].outcome is Outcome.FLAG and res["r_required_fields"].severity == 2
    assert ctx.decision is Decision.REQUEST_INFO


def test_missing_fields_without_a_failure_are_still_the_vendors_omission():
    ctx = ctx_with(ExtractionMeta(), extracted=make_extracted(total=None))
    assert ev_builtin("r_required_fields", ctx).outcome is Outcome.FLAG


def test_a_system_side_failure_with_the_rules_disabled_is_still_floored():
    meta = ExtractionMeta(degraded=True, failure_kind="system_side", failure_code="cost_ceiling")
    stage, floor = floor_of(ctx_with(meta, extracted=null_invoice(), matched=False, vendor_id=None))
    assert stage.outputs["decision"] == "review" and "extraction_degraded" in codes(floor)
