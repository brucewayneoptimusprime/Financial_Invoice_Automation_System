"""The engine floor holds even when every rule is disabled (or there are no rules at all)."""
import pytest

from app.engine.engine import EngineConfig, run_validate_stage
from app.enums import MatchStatus, Outcome
from app.models import POCandidate
from tests.engine.fakes import FAKE_REGISTRY, rule
from tests.factories import field, make_ctx, make_extracted, make_facts, make_po


def floor_of(ctx, rules=(), **kw):
    """Run with every supplied rule disabled; return (stage, floor result)."""
    disabled = [rule(f"off{i}", "fake_pass", enabled=False) for i in range(3)] + list(rules)
    stage = run_validate_stage(ctx, disabled, registry=FAKE_REGISTRY, **kw)
    return stage, next(r for r in ctx.rule_results if r.rule_id == "engine_floor")


def codes(floor):
    return [r["code"] for r in floor.detail["reasons"]]


def test_clean_matched_invoice_is_not_floored_and_approves():
    stage, floor = floor_of(make_ctx())
    assert floor.outcome is Outcome.PASS and floor.severity == 0 and codes(floor) == []
    assert stage.outputs["decision"] == "approve"


def test_no_po_matched_with_all_rules_disabled_is_at_least_review():
    ctx = make_ctx(matched=False)                       # match_status NO_CANDIDATES, matched_po None
    stage, floor = floor_of(ctx)
    assert floor.outcome is Outcome.FLAG and floor.severity == 1 and floor.outcome_key == "floor_applied"
    assert "no_po_candidates" in codes(floor)
    assert stage.outputs["final_severity"] >= 1 and stage.outputs["decision"] == "review"
    assert "no candidate purchase order" in floor.message


def test_low_score_match_with_all_rules_disabled_is_at_least_review():
    stage, floor = floor_of(make_ctx(matched=False, match_status=MatchStatus.LOW_SCORE))
    assert "no_confident_po_match" in codes(floor) and stage.outputs["decision"] == "review"


def test_ambiguous_match_with_all_rules_disabled_is_at_least_review():
    ctx = make_ctx(matched=False, match_status=MatchStatus.AMBIGUOUS)
    stage, floor = floor_of(ctx)
    assert "ambiguous_po_match" in codes(floor)
    assert stage.outputs["final_severity"] >= 1 and stage.outputs["decision"] == "review"
    assert "too close" in floor.message


def test_a_po_that_is_set_but_not_marked_confident_still_floors():
    ctx = make_ctx(match_status=MatchStatus.AMBIGUOUS)  # matched_po present but status says ambiguous
    assert ctx.matched_po is not None
    assert "ambiguous_po_match" in codes(floor_of(ctx)[1])


def test_unmarked_match_state_is_treated_as_unmatched():
    assert "match_not_run" in codes(floor_of(make_ctx(match_status=None))[1])


@pytest.mark.parametrize("total", [None, field(None, 0.99), field(None, 0.0)])
def test_null_total_with_all_rules_disabled_is_at_least_review(total):
    ctx = make_ctx(extracted=make_extracted(total=total))
    stage, floor = floor_of(ctx)
    assert floor.severity == 1
    assert "required_fields_missing" in codes(floor) and "amount_not_evaluable:missing_amount" in codes(floor)
    assert floor.detail["reasons"][1]["fields"] == ["total"] or "total" in floor.message
    assert stage.outputs["decision"] == "review"


@pytest.mark.parametrize("name", ["vendor_name", "invoice_number", "invoice_date", "currency", "total"])
def test_any_null_required_field_floors_regardless_of_confidence(name):
    ctx = make_ctx(extracted=make_extracted(**{name: field(None, 0.99)}))
    _, floor = floor_of(ctx)
    assert floor.severity == 1 and "required_fields_missing" in codes(floor)
    assert name in [r for r in floor.detail["reasons"] if r["code"] == "required_fields_missing"][0]["fields"]


def test_non_required_null_field_does_not_floor():
    _, floor = floor_of(make_ctx(extracted=make_extracted(tax=None, po_reference=None)))
    assert floor.outcome is Outcome.PASS


def test_low_confidence_required_field_floors_with_the_numbers():
    ctx = make_ctx(extracted=make_extracted(total=field("1100.00", 0.5)))
    _, floor = floor_of(ctx)
    reason = next(r for r in floor.detail["reasons"] if r["code"] == "required_fields_low_confidence")
    assert reason["fields"] == [{"field": "total", "confidence": 0.5, "threshold": 0.8}]


def test_confidence_exactly_at_the_threshold_is_acceptable():
    _, floor = floor_of(make_ctx(extracted=make_extracted(total=field("1100.00", 0.8))))
    assert floor.outcome is Outcome.PASS


def test_threshold_comes_from_the_facts_snapshot():
    ctx = make_ctx(facts=make_facts(threshold=0.99))     # default fixture confidence is 0.95
    assert "required_fields_low_confidence" in codes(floor_of(ctx)[1])


@pytest.mark.parametrize("kwargs,reason", [
    (dict(extracted=make_extracted(currency="EUR")), "amount_not_evaluable:currency_mismatch"),
    (dict(extracted=make_extracted(total="0.00")), "amount_not_evaluable:non_positive_amount"),
    (dict(extracted=make_extracted(total="-5.00")), "amount_not_evaluable:non_positive_amount"),
    (dict(extracted=make_extracted(total="10.005")), "amount_not_evaluable:invalid_amount"),
])
def test_amount_not_evaluable_floors(kwargs, reason):
    _, floor = floor_of(make_ctx(**kwargs))
    assert reason in codes(floor) and floor.severity == 1


def test_missing_extraction_or_facts_floors():
    assert "no_extraction" in codes(floor_of(make_ctx(extracted=None))[1])
    ctx = make_ctx()
    ctx.facts = None                                    # PO "matched" but no snapshot to compare against
    _, floor = floor_of(ctx)
    assert floor.severity == 1 and "amount_not_evaluable:no_facts" in codes(floor)
    _, floor = floor_of(make_ctx(facts=None))           # nothing matched and no snapshot at all
    assert floor.severity == 1 and "no_po_matched" in codes(floor)


def test_matched_po_missing_from_the_snapshot_floors():
    ctx = make_ctx()
    ctx.matched_po = POCandidate(po_id=999, po_number="GHOST", score=0.9)
    assert "amount_not_evaluable:po_not_in_facts" in codes(floor_of(ctx)[1])


def test_floor_is_always_in_the_trail_with_its_reasons():
    stage, floor = floor_of(make_ctx(matched=False))
    ev = [e for e in stage.events if e.event_type == "engine_floor"]
    assert len(ev) == 1 and ev[0].rule_id == "engine_floor" and ev[0].detail["reasons"] == floor.detail["reasons"]
    assert floor_of(make_ctx())[0].events[-2].detail["reasons"] == []


def test_floor_never_lowers_a_higher_severity():
    stage, _ = floor_of(make_ctx(matched=False), rules=[rule("hard", "fake_fail", 3)])
    assert stage.outputs["final_severity"] == 3


def test_floor_severity_is_configurable_and_valid():
    cfg = EngineConfig.from_settings()
    strict = EngineConfig(cfg.required_fields, cfg.decision_severity, cfg.locked_rule_ids, floor_severity=2)
    stage, floor = floor_of(make_ctx(matched=False), config=strict)
    assert floor.severity == 2 and stage.outputs["decision"] == "request_info"


def test_floor_required_fields_come_from_config():
    cfg = EngineConfig.from_settings()
    narrow = EngineConfig(("vendor_name",), cfg.decision_severity, cfg.locked_rule_ids, cfg.floor_severity)
    _, floor = floor_of(make_ctx(extracted=make_extracted(total=None)), config=narrow)
    assert "required_fields_missing" not in codes(floor)  # total is no longer required here (amount check still floors)
    assert "amount_not_evaluable:missing_amount" in codes(floor)


def test_po_with_over_billed_balance_is_still_evaluable():
    ctx = make_ctx(facts=make_facts(pos=[make_po(total="100.00", net_committed="150.00")]))
    _, floor = floor_of(ctx)
    assert floor.outcome is Outcome.PASS          # negative balance is a rules matter (tolerance), not a floor matter
