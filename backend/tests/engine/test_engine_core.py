import json
import random

import pytest

from app.engine.engine import EngineConfig, canonical_order, decide, run_decide_stage, run_validate_stage
from app.enums import Decision, Outcome, StageStatus
from tests.engine.fakes import FAKE_REGISTRY, rule
from tests.factories import make_ctx


def run(ctx, rules, **kw):
    return run_validate_stage(ctx, rules, registry=FAKE_REGISTRY, **kw)


def results_by_id(ctx):
    return {r.rule_id: r for r in ctx.rule_results}


def events_of(stage, event_type=None):
    return [e for e in stage.events if event_type is None or e.event_type == event_type]


# ------------------------------------------------------------------ no short-circuit / aggregation

def test_all_enabled_rules_run_even_after_a_severity_3_result():
    ctx = make_ctx()
    rules = [rule("a", "fake_fail", 3), rule("b", "fake_flag", 1), rule("c", "fake_pass"), rule("d", "fake_info")]
    stage = run(ctx, rules)
    assert set(results_by_id(ctx)) == {"a", "b", "c", "d", "engine_floor"}
    assert stage.outputs["final_severity"] == 3 and stage.outputs["decision"] == "reject"
    assert stage.status is StageStatus.FLAGGED
    assert sorted(stage.outputs["triggered_rule_ids"]) == ["a", "b"]


def test_no_triggered_rule_means_zero_and_approve():
    ctx = make_ctx()
    stage = run(ctx, [rule("a", "fake_pass"), rule("b", "fake_info")])
    assert stage.outputs["final_severity"] == 0 and stage.outputs["decision"] == "approve"
    assert stage.status is StageStatus.OK
    assert results_by_id(ctx)["engine_floor"].outcome is Outcome.PASS


def test_empty_rule_list_on_a_clean_context_approves():
    ctx = make_ctx()
    assert run(ctx, []).outputs["decision"] == "approve"


def test_severity_is_the_max_not_a_sum():
    ctx = make_ctx()
    stage = run(ctx, [rule("a", "fake_flag", 1), rule("b", "fake_flag", 1), rule("c", "fake_flag", 2)])
    assert stage.outputs["final_severity"] == 2


def test_evaluator_receives_reserved_default_severity_and_validated_params():
    ctx = make_ctx()
    run(ctx, [rule("a", "fake_flag", 2, params={"custom": 7})])
    saw = results_by_id(ctx)["a"].detail["saw_params"]
    assert saw["severity_on_trigger"] == 2 and saw["custom"] == 7


# ------------------------------------------------------------------ audit trail

def test_every_result_has_exactly_one_audit_event_with_its_numbers():
    ctx = make_ctx()
    stage = run(ctx, [rule("a", "fake_flag", 2), rule("b", "fake_pass")])
    rule_events = [e for e in stage.events if e.event_type in ("rule_evaluated", "engine_floor")]
    assert [e.rule_id for e in rule_events] == ["a", "b", "engine_floor"]
    assert len(rule_events) == len(ctx.rule_results)
    for event, result in zip(rule_events, ctx.rule_results):
        assert event.outcome is result.outcome and event.message == result.message
        assert event.stage == "validate" and event.detail["severity"] == result.severity
    assert results_by_id(ctx)["b"].detail["amount"] == "1.50"        # Decimal made JSON-safe, exactly
    assert events_of(stage, "severity_aggregated")[0].detail["final_severity"] == 2


def test_event_order_is_rules_then_floor_then_aggregate():
    stage = run(make_ctx(), [rule("a", "fake_flag"), rule("b", "fake_pass")])
    assert [e.event_type for e in stage.events] == ["rule_evaluated", "rule_evaluated", "engine_floor", "severity_aggregated"]


def test_stage_result_and_results_serialise_to_json():
    ctx = make_ctx()
    stage = run(ctx, [rule("a", "fake_flag"), rule("b", "fake_pass")])
    json.loads(stage.model_dump_json())
    json.loads(json.dumps([r.model_dump(mode="json") for r in ctx.rule_results]))


# ------------------------------------------------------------------ disabled and locked rules

def test_disabled_rule_is_skipped_with_an_info_event():
    ctx = make_ctx()
    stage = run(ctx, [rule("a", "fake_fail", 3, enabled=False), rule("b", "fake_pass")])
    assert "a" not in results_by_id(ctx)
    skip = events_of(stage, "rule_skipped")
    assert len(skip) == 1 and skip[0].rule_id == "a" and skip[0].outcome is Outcome.INFO
    assert stage.outputs["skipped_rule_ids"] == ["a"] and stage.outputs["final_severity"] == 0


@pytest.mark.parametrize("locked_id,rule_type", [("r_vendor_status", "vendor_status"), ("r_duplicate_exact", "duplicate_exact")])
def test_locked_rules_ignore_enabled_false_and_say_so(locked_id, rule_type):
    ctx = make_ctx()
    stage = run(ctx, [rule(locked_id, rule_type, source="builtin", enabled=False)])
    assert locked_id in results_by_id(ctx)                                  # it RAN
    note = events_of(stage, "locked_rule_enabled")
    assert len(note) == 1 and note[0].rule_id == locked_id and note[0].outcome is Outcome.INFO
    assert "ignored" in note[0].message and stage.outputs["skipped_rule_ids"] == []


def test_locked_rule_params_remain_editable():
    ctx = make_ctx()
    run(ctx, [rule("r_vendor_status", "fake_flag", 1, source="builtin", enabled=False, params={"tuned": "yes"})])
    assert results_by_id(ctx)["r_vendor_status"].detail["saw_params"]["tuned"] == "yes"


def test_a_non_locked_builtin_rule_can_be_disabled():
    ctx = make_ctx()
    run(ctx, [rule("r_tolerance_pct", "fake_fail", 3, source="builtin", enabled=False)])
    assert "r_tolerance_pct" not in results_by_id(ctx)


def test_locked_set_comes_from_config():
    ctx = make_ctx()
    cfg = EngineConfig.from_settings()
    custom = EngineConfig(cfg.required_fields, cfg.decision_severity, frozenset({"only_me"}), cfg.floor_severity)
    run(ctx, [rule("r_vendor_status", "fake_pass", source="builtin", enabled=False), rule("only_me", "fake_pass", enabled=False)], config=custom)
    assert "only_me" in results_by_id(ctx) and "r_vendor_status" not in results_by_id(ctx)


# ------------------------------------------------------------------ fail-safe: never a silent pass

@pytest.mark.parametrize("rule_type,key", [
    ("fake_raise", "evaluator_exception"),
    ("fake_garbage", "invalid_result"),
    ("fake_cheat_zero_flag", "invalid_result"),
    ("fake_cheat_pass_severity", "invalid_result"),
    ("fake_facts_mutator", "evaluator_exception"),
    ("no_such_type", "unknown_rule_type"),
])
def test_broken_evaluators_escalate_at_the_rules_default_severity(rule_type, key):
    ctx = make_ctx()
    stage = run(ctx, [rule("bad", rule_type, 2)])
    result = results_by_id(ctx)["bad"]
    assert result.outcome is Outcome.FLAG and result.severity == 2 and result.outcome_key == key
    assert result.detail["error"] == key and "escalated" in result.message
    assert stage.outputs["final_severity"] == 2


def test_invalid_and_reserved_params_escalate():
    ctx = make_ctx()
    run(ctx, [rule("typo", "strict_params", 3, params={"severity_by_outcom": {}}),
              rule("bad_sev", "strict_params", 2, params={"severity_by_outcome": {"x": 0}}),
              rule("reserved", "fake_pass", 1, params={"severity_on_trigger": 1})])
    res = results_by_id(ctx)
    assert res["typo"].outcome_key == res["bad_sev"].outcome_key == res["reserved"].outcome_key == "invalid_params"
    assert (res["typo"].severity, res["bad_sev"].severity, res["reserved"].severity) == (3, 2, 1)


def test_valid_per_outcome_overrides_are_accepted():
    ctx = make_ctx()
    run(ctx, [rule("ok", "strict_params", 1, params={"severity_by_outcome": {"new": 1, "blocked": 3}})])
    assert results_by_id(ctx)["ok"].outcome is Outcome.PASS


def test_evaluators_cannot_mutate_the_run_context():
    ctx = make_ctx()
    before = ctx.model_dump(mode="json", exclude={"rule_results"})
    run(ctx, [rule("m", "fake_mutator")])
    assert ctx.model_dump(mode="json", exclude={"rule_results"}) == before
    assert ctx.extracted.total.value is not None


# ------------------------------------------------------------------ determinism

def test_same_input_gives_identical_output():
    rules = [rule("a", "fake_flag", 2), rule("b", "fake_pass"), rule("c", "fake_raise", 1, source="nl")]
    out = []
    for _ in range(3):
        ctx = make_ctx()
        stage = run(ctx, rules)
        out.append((stage.model_dump(mode="json"), [r.model_dump(mode="json") for r in ctx.rule_results]))
    assert out[0] == out[1] == out[2]


def test_rule_order_in_the_input_does_not_matter():
    rules = [rule(f"r{i}", t, sev, source=src) for i, (t, sev, src) in enumerate([
        ("fake_flag", 1, "user"), ("fake_pass", 1, "builtin"), ("fake_fail", 3, "nl"),
        ("fake_info", 1, "builtin"), ("fake_raise", 2, "user"), ("fake_flag", 2, "builtin")])]
    baseline = None
    for seed in range(8):
        shuffled = rules[:]
        random.Random(seed).shuffle(shuffled)
        ctx = make_ctx()
        stage = run(ctx, shuffled)
        dumped = stage.model_dump(mode="json")
        baseline = baseline or dumped
        assert dumped == baseline


def test_canonical_order_is_builtin_then_user_then_nl_then_id():
    rules = [rule("z", source="nl"), rule("b", source="user"), rule("a", source="builtin"), rule("c", source="builtin")]
    assert [r.id for r in canonical_order(rules)] == ["a", "c", "b", "z"]


# ------------------------------------------------------------------ decide stage

def test_decide_stage_sets_the_decision_from_rule_results():
    ctx = make_ctx()
    run(ctx, [rule("a", "fake_flag", 2)])
    stage = run_decide_stage(ctx)
    assert ctx.decision is Decision.REQUEST_INFO and stage.outputs["decision"] == "request_info"
    assert stage.events[0].stage == "decide" and stage.events[0].event_type == "decision_made"
    assert decide(ctx) == (2, Decision.REQUEST_INFO)


def test_decision_order_is_read_from_config():
    ctx = make_ctx()
    cfg = EngineConfig.from_settings()
    swapped = EngineConfig(cfg.required_fields, {"approve": 0, "review": 1, "request_info": 3, "reject": 2},
                           cfg.locked_rule_ids, cfg.floor_severity)
    run(ctx, [rule("a", "fake_flag", 3)], config=swapped)
    assert decide(ctx, swapped)[1] is Decision.REQUEST_INFO
