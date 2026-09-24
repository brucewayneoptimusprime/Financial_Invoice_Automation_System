"""Evaluator registry: rule `type` -> evaluator function (+ optional params model).

An evaluator is a pure function `(ctx: RunContext, params: dict) -> RuleResult`. The engine passes
`params` = the rule's validated params plus the reserved key `severity_on_trigger` (the rule's
default severity), and stamps `rule_id` on the result. The engine does not care where a rule came from.
"""
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import get_settings
from app.enums import Decision
from app.models.rules import RuleResult
from app.models.run import RunContext

RESERVED_PARAM_KEYS = frozenset({"severity_on_trigger"})

Evaluator = Callable[[RunContext, dict[str, Any]], RuleResult]


class BaseParams(BaseModel):
    """Params every evaluator accepts. Unknown keys are rejected (a typo must not silently do nothing)."""

    model_config = ConfigDict(extra="forbid")

    # Per-outcome severity overrides, e.g. {"new": 1, "blocked": 3}. Values stay within 1..3.
    severity_by_outcome: dict[str, int] = Field(default_factory=dict)

    @field_validator("severity_by_outcome")
    @classmethod
    def _escalate_only_values(cls, v: dict[str, int]) -> dict[str, int]:
        allowed = {s for name, s in get_settings().decision_severity.items() if name != Decision.APPROVE.value}
        bad = {k: s for k, s in v.items() if s not in allowed}
        if bad:
            raise ValueError(f"severity values must be one of {sorted(allowed)}, got {bad}")
        return v


@dataclass(frozen=True)
class EvaluatorSpec:
    fn: Evaluator
    params_model: type[BaseModel] | None = None

    def validate_params(self, raw: dict[str, Any]) -> dict[str, Any]:
        if self.params_model is None:
            return dict(raw)
        return self.params_model.model_validate(raw).model_dump()


REGISTRY: dict[str, EvaluatorSpec] = {}


def register(rule_type: str, params_model: type[BaseModel] | None = BaseParams):
    """Decorator: register `fn` as the evaluator for rules of `rule_type`."""

    def wrap(fn: Evaluator) -> Evaluator:
        if rule_type in REGISTRY:
            raise ValueError(f"evaluator for {rule_type!r} already registered")
        REGISTRY[rule_type] = EvaluatorSpec(fn=fn, params_model=params_model)
        return fn

    return wrap
