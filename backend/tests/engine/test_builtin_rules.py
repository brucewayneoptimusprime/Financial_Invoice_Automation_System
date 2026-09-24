"""The 12 builtin rules together: registry coverage, DB round-trip, full runs through the real engine."""
import json
import random

import pytest

from app.builtin_rules import builtin_rules
from app.config import get_settings
from app.engine.engine import run_decide_stage, run_validate_stage
from app.engine.evaluators import REGISTRY
from app.engine.loader import load_facts, load_rules
from app.enums import Decision, InvoiceStatus, MatchStatus, Outcome, POStatus, RuleSource, VendorStatus
from app.models import POCandidate, Rule
from app.models.run import VendorMatch
from tests.engine.real import BUILTIN
from tests.factories import field, make_ctx, make_extracted, make_facts, make_po, make_prior, make_vendor

ALL_IDS = sorted(BUILTIN)


def run_all(ctx, rules=None):
    stage = run_validate_stage(ctx, list(BUILTIN.values()) if rules is None else rules)
    return stage, {r.rule_id: r for r in ctx.rule_results}


def test_there_are_thirteen_builtin_rules_with_unique_ids_and_registered_evaluators():
    assert len(BUILTIN) == 13
    for rule in BUILTIN.values():
        assert rule.type in REGISTRY, rule.id
        REGISTRY[rule.type].validate_params(rule.params)            # seeded params are valid for their evaluator
        assert rule.source is RuleSource.BUILTIN and rule.enabled


def test_locked_rule_ids_are_real_builtin_rules():
    assert get_settings().locked_rule_ids <= set(BUILTIN)


def test_final_severity_table():
    """Documents the shipped defaults: (default severity, per-outcome overrides)."""
    table = {r.id: (r.severity_on_trigger, r.params.get("severity_by_outcome", {})) for r in BUILTIN.values()}
    assert table == {
        "r_vendor_status": (1, {"new": 1, "blocked": 3, "unknown": 1, "ambiguous": 1}),
        "r_po_found": (1, {"no_reference": 2, "reference_not_found": 1, "no_confident_match": 1}),
        "r_po_ambiguity": (1, {}),
        "r_vendor_po_mismatch": (1, {}),
        "r_currency_mismatch": (1, {}),
        "r_tolerance_pct": (1, {"over_tolerance": 1, "non_positive_total": 1, "invalid_amount": 1}),
        "r_arithmetic": (1, {}),
        "r_duplicate_exact": (3, {"same_file_hash": 3, "same_vendor_number_same_total": 3,
                                  "same_vendor_number_different_total": 1, "resubmission": 1}),
        "r_duplicate_fuzzy": (1, {}),
        "r_required_fields": (2, {}),
        "r_extraction_confidence": (1, {}),
        "r_document_type": (1, {"not_an_invoice": 1}),
        "r_po_status": (1, {"fully_billed": 1, "closed": 3}),
    }
    assert all(1 <= s <= 3 for s, ov in table.values()) and all(1 <= v <= 3 for _, ov in table.values() for v in ov.values())


def test_seeded_database_rules_equal_the_builtin_definitions(conn):
    db_rules = {r.id: r for r in load_rules(conn)}
    assert set(db_rules) == set(BUILTIN)
    for rid, rule in BUILTIN.items():
        assert db_rules[rid] == rule


def test_rules_are_data_edits_in_the_db_change_engine_behaviour(conn):
    with conn:
        conn.execute("UPDATE rules SET params = ? WHERE id = 'r_tolerance_pct'",
                     (json.dumps({"pct": 50.0, "abs": 500.0, "mode": "lesser_of", "compare_field": "total"}),))
        conn.execute("INSERT INTO rules (id, name, type, params, severity_on_trigger, source, enabled, original_text) "
                     "VALUES ('u_custom', 'custom', 'required_fields', ?, 3, 'nl', 1, 'we need a PO reference')",
                     (json.dumps({"fields": ["po_reference"]}),))
    rules = load_rules(conn)
    assert next(r for r in rules if r.id == "r_tolerance_pct").params["pct"] == 50.0
    custom = next(r for r in rules if r.id == "u_custom")
    assert custom.source is RuleSource.NL and custom.original_text == "we need a PO reference"
    ctx = make_ctx(extracted=make_extracted(po_reference=None))
    stage = run_validate_stage(ctx, rules)
    assert "u_custom" in stage.outputs["triggered_rule_ids"] and stage.outputs["final_severity"] == 3


def test_clean_invoice_runs_all_thirteen_rules_plus_both_floors_and_is_approved():
    ctx = make_ctx()
    stage, res = run_all(ctx)
    assert set(res) == set(ALL_IDS) | {"engine_floor", "engine_floor_reference"} and len(ctx.rule_results) == 15
    assert all(r.outcome is Outcome.PASS for r in res.values()), {k: v.message for k, v in res.items() if v.outcome is not Outcome.PASS}
    assert stage.outputs["final_severity"] == 0 and stage.outputs["decision"] == "approve"
    assert run_decide_stage(ctx).outputs["decision"] == "approve" and ctx.decision is Decision.APPROVE
    assert len([e for e in stage.events if e.rule_id]) == 15


def test_a_messy_invoice_shows_every_problem_not_just_the_first():
    facts = make_facts(vendors=[make_vendor(status=VendorStatus.BLOCKED)],
                       pos=[make_po(status=POStatus.FULLY_BILLED, net_committed="1200.00")],
                       priors=[make_prior(file_hash="hash-current")])
    ctx = make_ctx(facts=facts, extracted=make_extracted(invoice_number=None, total="1105.00", currency="EUR"))
    stage, res = run_all(ctx)
    assert res["r_vendor_status"].outcome is Outcome.FAIL and res["r_vendor_status"].severity == 3
    assert res["r_duplicate_exact"].outcome_key == "same_file_hash"
    assert res["r_required_fields"].outcome_key == "missing" and res["r_currency_mismatch"].outcome_key == "mismatch"
    assert res["r_po_status"].outcome_key == "fully_billed" and res["r_arithmetic"].outcome_key == "mismatch"
    assert stage.outputs["final_severity"] == 3 and stage.outputs["decision"] == "reject"
    assert len(stage.outputs["triggered_rule_ids"]) >= 6


def test_all_builtin_rules_disabled_still_runs_locked_rules_and_the_floor():
    rules = [r.model_copy(update={"enabled": False}) for r in BUILTIN.values()]
    ctx = make_ctx(matched=False, facts=make_facts(vendors=[make_vendor(status=VendorStatus.BLOCKED)]))
    stage, res = run_all(ctx, rules)
    assert set(res) == {"r_vendor_status", "r_duplicate_exact", "engine_floor", "engine_floor_reference"}
    assert res["r_vendor_status"].severity == 3 and stage.outputs["decision"] == "reject"
    assert sorted(stage.outputs["skipped_rule_ids"]) == sorted(set(ALL_IDS) - {"r_vendor_status", "r_duplicate_exact"})
    assert len([e for e in stage.events if e.event_type == "locked_rule_enabled"]) == 2


def test_all_builtin_disabled_and_nothing_matched_is_still_at_least_review():
    rules = [r.model_copy(update={"enabled": False}) for r in BUILTIN.values()]
    for kwargs in (dict(matched=False), dict(matched=False, match_status=MatchStatus.AMBIGUOUS),
                   dict(extracted=make_extracted(total=field(None, 0.99)))):
        ctx = make_ctx(**kwargs)
        stage, _ = run_all(ctx, rules)
        assert stage.outputs["final_severity"] >= 1, kwargs


def test_full_run_is_deterministic_across_rule_and_fact_ordering():
    def build(seed):
        rng = random.Random(seed)
        vendors = [make_vendor(1), make_vendor(2, "Vendor Beta Inc", status=VendorStatus.NEW)]
        priors = [make_prior(id=1, file_hash="h1"), make_prior(id=2, invoice_number="INV-2", total="1100.00", vendor_id=1,
                                                              invoice_date=make_extracted().invoice_date.value)]
        rng.shuffle(vendors), rng.shuffle(priors)
        rules = list(BUILTIN.values())
        rng.shuffle(rules)
        return make_ctx(facts=make_facts(vendors=vendors, priors=priors)), rules

    outputs = []
    for seed in range(6):
        ctx, rules = build(seed)
        stage = run_validate_stage(ctx, rules)
        outputs.append((stage.model_dump(mode="json"), [r.model_dump(mode="json") for r in ctx.rule_results]))
    assert all(o == outputs[0] for o in outputs)


def test_every_result_detail_is_json_serialisable_and_carries_numbers():
    ctx = make_ctx(facts=make_facts(priors=[make_prior(file_hash="hash-current")]))
    stage, res = run_all(ctx)
    json.dumps(stage.model_dump(mode="json"))
    assert all(r.detail for r in res.values())
    assert "invoice_amount" in res["r_tolerance_pct"].detail and res["r_extraction_confidence"].detail["threshold"] == 0.8


def test_engine_run_does_not_mutate_facts_or_extraction():
    ctx = make_ctx()
    before = (ctx.facts.model_dump_json(), ctx.extracted.model_dump_json())
    run_all(ctx)
    assert (ctx.facts.model_dump_json(), ctx.extracted.model_dump_json()) == before
