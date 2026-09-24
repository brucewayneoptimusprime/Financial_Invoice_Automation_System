"""Run the REAL evaluators through the engine's own evaluate_rule (so params validation and severity
resolution are exercised too)."""
from app.builtin_rules import builtin_rules
from app.config import Settings
from app.engine.engine import EngineConfig, evaluate_rule
from app.engine.evaluators import REGISTRY
from app.models.rules import Rule, RuleResult
from app.models.run import RunContext

SETTINGS = Settings(_env_file=None)
BUILTIN: dict[str, Rule] = {r.id: r for r in builtin_rules(SETTINGS)}
CONFIG = EngineConfig.from_settings(SETTINGS)


def ev(rule_type: str, ctx: RunContext, params: dict | None = None, severity: int = 1) -> RuleResult:
    """Evaluate an ad-hoc rule of `rule_type` with the given params."""
    rule = Rule(id="r_test", name="t", type=rule_type, params=params or {}, severity_on_trigger=severity, source="user")
    return evaluate_rule(ctx, rule, CONFIG, REGISTRY)


def ev_builtin(rule_id: str, ctx: RunContext, **param_overrides) -> RuleResult:
    """Evaluate a seeded builtin rule (its real severity and params), optionally overriding params."""
    base = BUILTIN[rule_id]
    rule = base.model_copy(update={"params": {**base.params, **param_overrides}})
    return evaluate_rule(ctx, rule, CONFIG, REGISTRY)
