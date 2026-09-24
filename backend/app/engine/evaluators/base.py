"""Helpers for writing evaluators. Evaluators are pure: (ctx, params) -> RuleResult.

`params` always contains `severity_on_trigger` (the rule's default severity, injected by the engine)
and `severity_by_outcome` (optional per-outcome overrides). The engine stamps the real rule_id.
"""
from decimal import Decimal
from typing import Any

from app.enums import Outcome
from app.models.rules import RuleResult
from app.models.run import RunContext

_ID = "pending"  # placeholder: the engine overwrites rule_id


def severity_for(params: dict[str, Any], key: str) -> int:
    return params.get("severity_by_outcome", {}).get(key, params["severity_on_trigger"])


def flag(params: dict[str, Any], key: str, message: str, detail: dict | None = None, *, hard: bool = False) -> RuleResult:
    """A triggered result. `hard=True` makes it a `fail` (definite violation) instead of a `flag`."""
    return RuleResult(rule_id=_ID, outcome=Outcome.FAIL if hard else Outcome.FLAG, severity=severity_for(params, key),
                      message=message, detail={"outcome_key": key, **(detail or {})}, outcome_key=key)


def ok(message: str, detail: dict | None = None, key: str = "ok") -> RuleResult:
    return RuleResult(rule_id=_ID, outcome=Outcome.PASS, severity=0, message=message,
                      detail={"outcome_key": key, **(detail or {})}, outcome_key=key)


def not_evaluable(reason: str, message: str, detail: dict | None = None) -> RuleResult:
    """The check could not be performed (a required input is missing). Never a pass: severity 0 but
    `info`, and the engine floor / completeness rule are what escalate missing data."""
    return RuleResult(rule_id=_ID, outcome=Outcome.INFO, severity=0, message=f"Not evaluated: {message}",
                      detail={"outcome_key": "not_evaluable", "reason": reason, **(detail or {})},
                      outcome_key="not_evaluable")


def money(value: Decimal | None, currency: str | None = None) -> str:
    if value is None:
        return "n/a"
    text = format(value, ",.2f")
    return f"{text} {currency}" if currency else text


def extracted_value(ctx: RunContext, name: str) -> Any:
    """The raw value of an evidenced top-level field, or None if missing (null counts as missing)."""
    if ctx.extracted is None:
        return None
    field = getattr(ctx.extracted, name, None)
    return getattr(field, "value", None)
