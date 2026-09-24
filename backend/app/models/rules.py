"""Rule schema (SPEC 6.3) and the per-rule result recorded on a run."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import get_settings
from app.enums import Decision, Outcome, RuleSource


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)  # evaluator lookup key; the engine (M1) owns the set of types
    params: dict[str, Any] = Field(default_factory=dict)
    severity_on_trigger: int
    source: RuleSource
    enabled: bool = True
    original_text: str | None = None

    @field_validator("severity_on_trigger")
    @classmethod
    def _escalate_only(cls, v: int) -> int:
        """A rule may only raise severity. Severity 0 (approve) is not a valid trigger outcome."""
        allowed = {s for name, s in get_settings().decision_severity.items() if name != Decision.APPROVE.value}
        if v not in allowed:
            raise ValueError(f"severity_on_trigger must be one of {sorted(allowed)}, got {v}")
        return v

    @model_validator(mode="after")
    def _nl_keeps_original_text(self) -> "Rule":
        if self.source == RuleSource.NL and not (self.original_text and self.original_text.strip()):
            raise ValueError("rules with source 'nl' must store original_text")
        return self


class RuleResult(BaseModel):
    """What one rule produced for one run. `detail` carries the numbers that drove the outcome."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    outcome: Outcome
    severity: int = Field(ge=0)  # severity contributed (0 if the rule passed)
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
