"""Severity aggregation. The ONLY place a final severity is computed, and it is a plain max:
there is no code path in which a rule (of any source) lowers the outcome."""
from typing import Iterable

from app.enums import Decision
from app.models.rules import TRIGGERED_OUTCOMES, RuleResult


def is_triggered(result: RuleResult) -> bool:
    return result.outcome in TRIGGERED_OUTCOMES


def aggregate(results: Iterable[RuleResult]) -> int:
    """final severity = max severity over triggered results; 0 (approve) if none triggered."""
    return max((r.severity for r in results if is_triggered(r)), default=0)


def decision_for(severity: int, decision_severity: dict[str, int]) -> Decision:
    for name, sev in decision_severity.items():
        if sev == severity:
            return Decision(name)
    raise ValueError(f"no decision has severity {severity}")
