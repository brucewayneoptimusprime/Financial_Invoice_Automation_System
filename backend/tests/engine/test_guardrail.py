"""Escalate-only guardrail: adding any ENABLED rule of any source never lowers the final decision."""
import itertools
import random

import pytest

from app.engine.engine import run_validate_stage
from app.engine.severity import aggregate
from app.enums import RuleSource
from tests.engine.fakes import FAKE_REGISTRY, rule
from tests.factories import make_ctx, make_extracted, make_facts

TYPES = sorted(FAKE_REGISTRY)
SOURCES = [s.value for s in RuleSource]
IDS = ["r_vendor_status", "r_duplicate_exact", "r_tolerance_pct"]  # locked and unlocked ids


def final(ctx, rules) -> int:
    return run_validate_stage(ctx, rules, registry=FAKE_REGISTRY).outputs["final_severity"]


def contexts():
    yield "clean", make_ctx()
    yield "unmatched", make_ctx(matched=False)
    yield "null_total", make_ctx(extracted=make_extracted(total=None))
    yield "no_facts", make_ctx(facts=None)


def random_rule(rng: random.Random, i: int):
    source = rng.choice(SOURCES)
    return rule(id=rng.choice([f"r{i}", f"r{i}", rng.choice(IDS)]), type=rng.choice(TYPES), severity=rng.choice([1, 2, 3]),
                source=source, enabled=rng.random() < 0.75)


@pytest.mark.parametrize("name,ctx_factory", [(n, (lambda c=c: c)) for n, c in contexts()])
def test_adding_a_random_rule_never_lowers_the_final_severity(name, ctx_factory):
    rng = random.Random(20260924)
    for trial in range(250):
        base = [random_rule(rng, i) for i in range(rng.randint(0, 5))]
        extra = random_rule(rng, 99)
        ctx = ctx_factory()
        without = final(ctx, base)
        with_extra = final(ctx, base + [extra])
        assert with_extra >= without, (trial, extra, base)


def test_exhaustive_source_type_severity_enabled_product_never_lowers():
    """Every source x every evaluator type (well- and mis-behaved) x every severity x enabled/disabled,
    added to bases of every strength."""
    bases = [[], [rule("b1", "fake_flag", 1)], [rule("b2", "fake_flag", 2)], [rule("b3", "fake_fail", 3)]]
    ctx = make_ctx()
    for base, source, rtype, sev, enabled in itertools.product(bases, SOURCES, TYPES, [1, 2, 3], [True, False]):
        extra = rule("extra", rtype, sev, source=source, enabled=enabled)
        assert final(ctx, base + [extra]) >= final(ctx, base), (source, rtype, sev, enabled)


@pytest.mark.parametrize("source", SOURCES)
def test_a_passing_rule_cannot_lower_a_triggered_outcome(source):
    ctx = make_ctx()
    hard = [rule("hard", "fake_fail", 3, source="builtin")]
    assert final(ctx, hard + [rule("soft", "fake_pass", 1, source=source)]) == 3
    assert final(ctx, hard + [rule("soft", "fake_info", 1, source=source)]) == 3


def test_a_rule_claiming_severity_zero_on_a_trigger_cannot_be_built_or_used():
    ctx = make_ctx()
    # the evaluator cheats with a zero-severity flag: the engine rejects it and ESCALATES instead of trusting it
    assert final(ctx, [rule("cheat", "fake_cheat_zero_flag", 2)]) == 2
    assert final(ctx, [rule("cheat", "fake_cheat_pass_severity", 1)]) == 1


def test_final_severity_equals_max_of_triggered_results_always():
    rng = random.Random(7)
    for _ in range(100):
        ctx = make_ctx(matched=rng.random() < 0.5)
        rules = [random_rule(rng, i) for i in range(rng.randint(0, 6))]
        run_validate_stage(ctx, rules, registry=FAKE_REGISTRY)
        expected = max((r.severity for r in ctx.rule_results if r.outcome.value in ("flag", "fail")), default=0)
        assert aggregate(ctx.rule_results) == expected
        assert all(r.severity == 0 for r in ctx.rule_results if r.outcome.value in ("pass", "info"))


def test_disabling_a_non_locked_rule_is_the_only_way_a_result_disappears():
    """Documented boundary: only a human toggling a builtin rule off removes its contribution, and the
    engine floor + locked rules still apply. Adding rules never does."""
    ctx = make_ctx(matched=False)
    off = final(ctx, [rule("r_tolerance_pct", "fake_fail", 3, source="builtin", enabled=False)])
    on = final(ctx, [rule("r_tolerance_pct", "fake_fail", 3, source="builtin", enabled=True)])
    assert off == 1 and on == 3                       # floor still holds at 1 with the rule off
    assert final(ctx, [rule("r_vendor_status", "fake_fail", 3, source="builtin", enabled=False)]) == 3   # locked
