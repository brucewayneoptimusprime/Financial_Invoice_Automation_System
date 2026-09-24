"""Fake evaluators for exercising the ENGINE (not the real rule families): well-behaved ones and
deliberately misbehaving ones (raise, return junk, cheat on severity, mutate the context)."""
from app.engine.evaluators.registry import BaseParams, EvaluatorSpec
from app.enums import Outcome, RuleSource
from app.models.rules import Rule, RuleResult


def _result(outcome, severity, message="fake", **kw):
    return RuleResult(rule_id="ignored-engine-stamps-it", outcome=outcome, severity=severity, message=message, **kw)


def fake_pass(ctx, params):
    return _result(Outcome.PASS, 0, detail={"amount": __import__("decimal").Decimal("1.50")})


def fake_info(ctx, params):
    return _result(Outcome.INFO, 0)


def fake_flag(ctx, params):
    return _result(Outcome.FLAG, params["severity_on_trigger"], detail={"saw_params": {k: v for k, v in params.items()}})


def fake_fail(ctx, params):
    return _result(Outcome.FAIL, params["severity_on_trigger"])


def fake_raise(ctx, params):
    raise RuntimeError("boom")


def fake_garbage(ctx, params):
    return {"outcome": "pass"}


def fake_cheat_zero_flag(ctx, params):  # a flag that claims severity 0 (bypasses validation)
    return RuleResult.model_construct(rule_id="x", outcome=Outcome.FLAG, severity=0, message="cheat", detail={}, outcome_key=None)


def fake_cheat_pass_severity(ctx, params):
    return RuleResult.model_construct(rule_id="x", outcome=Outcome.PASS, severity=3, message="cheat", detail={}, outcome_key=None)


def fake_mutator(ctx, params):  # tries to tamper with the context it was handed, then reports a pass
    ctx.extracted.total.value = None
    ctx.decision = "approve"
    ctx.rule_results.clear()
    return _result(Outcome.PASS, 0)


def fake_facts_mutator(ctx, params):
    ctx.facts.settings.confidence_threshold = 0.0  # frozen -> raises
    return _result(Outcome.PASS, 0)


FAKES = {
    "fake_pass": fake_pass, "fake_info": fake_info, "fake_flag": fake_flag, "fake_fail": fake_fail,
    "fake_raise": fake_raise, "fake_garbage": fake_garbage, "fake_cheat_zero_flag": fake_cheat_zero_flag,
    "fake_cheat_pass_severity": fake_cheat_pass_severity, "fake_mutator": fake_mutator,
    "fake_facts_mutator": fake_facts_mutator,
    # names the engine treats specially (locked rules) still need SOME evaluator in a fake registry
    "vendor_status": fake_pass, "duplicate_exact": fake_pass,
}
FAKE_REGISTRY = {name: EvaluatorSpec(fn=fn, params_model=None) for name, fn in FAKES.items()}
FAKE_REGISTRY["strict_params"] = EvaluatorSpec(fn=fake_pass, params_model=BaseParams)


def rule(id: str = "r1", type: str = "fake_flag", severity: int = 1, source: str = "user", enabled: bool = True,
         params: dict | None = None, original_text: str | None = None) -> Rule:
    if RuleSource(source) is RuleSource.NL and original_text is None:
        original_text = "policy text"
    return Rule(id=id, name=f"rule {id}", type=type, params=params or {}, severity_on_trigger=severity,
                source=source, enabled=enabled, original_text=original_text)
