"""Escalate-only guardrail against the REAL evaluators: adding any enabled rule of any source, type, params
or severity never lowers the final decision."""
import itertools
import random

import pytest

from app.engine.engine import run_validate_stage
from app.engine.evaluators import REGISTRY
from app.enums import InvoiceStatus, MatchStatus, RuleSource, VendorStatus
from app.models import Rule
from tests.engine.real import BUILTIN
from tests.factories import field, make_ctx, make_extracted, make_facts, make_po, make_prior, make_vendor

TYPES = sorted(REGISTRY) + ["not_a_registered_type"]
PARAM_POOL = [
    {}, {"severity_by_outcome": {"new": 1}}, {"severity_by_outcome": {"blocked": 3, "mismatch": 2}},
    {"severity_by_outcome": {"x": 0}},                       # invalid: 0 not allowed
    {"pct": 0.0, "abs": 0.0}, {"pct": 100.0, "abs": 1e6, "mode": "greater_of"}, {"fields": ["total"]},
    {"fields": ["nope"]}, {"days": 365}, {"counted_statuses": []}, {"rounding_per_term": 100.0},
    {"severity_on_trigger": 1},                              # reserved key
    {"totally_unknown_param": 1},
]
IDS = ["r_vendor_status", "r_duplicate_exact", "r_tolerance_pct", "custom_a", "custom_b"]


def contexts():
    yield "clean", make_ctx()
    yield "unmatched", make_ctx(matched=False)
    yield "ambiguous", make_ctx(matched=False, match_status=MatchStatus.AMBIGUOUS)
    yield "null_total", make_ctx(extracted=make_extracted(total=field(None, 0.99)))
    yield "new_vendor", make_ctx(facts=make_facts(vendors=[make_vendor(status=VendorStatus.NEW)]))
    yield "blocked_vendor", make_ctx(facts=make_facts(vendors=[make_vendor(status=VendorStatus.BLOCKED)]))
    yield "duplicate", make_ctx(facts=make_facts(priors=[make_prior(file_hash="hash-current")]))
    yield "over_balance", make_ctx(extracted=make_extracted(total="5000.00"))
    yield "no_facts", make_ctx(facts=None)


def random_rule(rng: random.Random, i: int) -> Rule:
    source = rng.choice(list(RuleSource))
    return Rule(id=rng.choice([f"x{i}", rng.choice(IDS)]), name="n", type=rng.choice(TYPES), params=rng.choice(PARAM_POOL),
                severity_on_trigger=rng.choice([1, 2, 3]), source=source, enabled=rng.random() < 0.8,
                original_text="policy" if source is RuleSource.NL else None)


def final(ctx, rules) -> int:
    return run_validate_stage(ctx, rules).outputs["final_severity"]


@pytest.mark.parametrize("name,ctx", list(contexts()), ids=[n for n, _ in contexts()])
def test_adding_a_random_real_rule_never_lowers_the_final_severity(name, ctx):
    rng = random.Random(4242)
    for trial in range(120):
        base = list(BUILTIN.values()) if rng.random() < 0.5 else [random_rule(rng, i) for i in range(rng.randint(0, 4))]
        extra = random_rule(rng, 99)
        assert final(ctx, base + [extra]) >= final(ctx, base), (trial, extra)


def test_exhaustive_source_type_severity_product_on_top_of_the_builtin_set():
    base = list(BUILTIN.values())
    for name, ctx in contexts():
        baseline = final(ctx, base)
        for source, rtype, sev in itertools.product(RuleSource, TYPES, [1, 2, 3]):
            extra = Rule(id="extra", name="n", type=rtype, params={}, severity_on_trigger=sev, source=source,
                         original_text="policy" if source is RuleSource.NL else None)
            assert final(ctx, base + [extra]) >= baseline, (name, source, rtype, sev)


@pytest.mark.parametrize("source", ["user", "nl"])
def test_a_user_or_nl_rule_reusing_a_builtin_id_can_only_add_never_lower(source):
    """Adding a rule with a builtin's id (an attempted override) runs alongside it; it cannot silence it."""
    ctx = make_ctx(facts=make_facts(vendors=[make_vendor(status=VendorStatus.BLOCKED)]))
    impostor = Rule(id="r_vendor_status", name="pass everything", type="required_fields", params={"fields": ["total"]},
                    severity_on_trigger=1, source=source, original_text="policy" if source == "nl" else None)
    with_impostor = final(ctx, list(BUILTIN.values()) + [impostor])
    assert with_impostor >= final(ctx, list(BUILTIN.values())) == 3


def test_disabling_is_the_only_removal_path_and_locked_rules_resist_it():
    ctx = make_ctx(facts=make_facts(vendors=[make_vendor(status=VendorStatus.BLOCKED)],
                                    priors=[make_prior(file_hash="hash-current", status=InvoiceStatus.APPROVED)]))
    rules = [r.model_copy(update={"enabled": False}) for r in BUILTIN.values()]
    assert final(ctx, rules) == 3                              # locked vendor_status / duplicate_exact still fire
    ok_ctx = make_ctx(facts=make_facts(pos=[make_po(total="100.00")]))
    assert final(ok_ctx, rules) == 0                           # nothing fires, floor satisfied: legitimately approve
