"""The engine floor: a safety net that is NOT a rule and cannot be disabled.

Final severity is at least `floor_severity` (1 = review) unless ALL of these hold:
  * a PO was confidently and unambiguously matched,
  * the amount check was actually evaluable,
  * every configured required field is present, and
  * every present required field meets the confidence threshold.
It is computed straight from the context (not from rule results), so it holds even when r_po_found,
the tolerance rule, the completeness rule or the confidence rule are disabled. It always appears in
the trail as an `engine_floor` result carrying the reasons.
"""
from dataclasses import dataclass

from app.engine.amounts import NO_MATCHED_PO, assess_amount
from app.engine.normalize import is_missing
from app.enums import MatchStatus, Outcome
from app.models.rules import RuleResult
from app.models.run import RunContext

FLOOR_RULE_ID = "engine_floor"

_MATCH_REASONS = {
    None: ("match_not_run", "PO matching has not been run"),
    MatchStatus.NO_CANDIDATES: ("no_po_candidates", "no candidate purchase order was found"),
    MatchStatus.LOW_SCORE: ("no_confident_po_match", "no candidate PO scored high enough to be a confident match"),
    MatchStatus.AMBIGUOUS: ("ambiguous_po_match", "the top candidate POs are too close to call"),
}


@dataclass(frozen=True)
class FloorConfig:
    required_fields: tuple[str, ...]
    floor_severity: int
    amount_compare_field: str = "total"


def evaluate_floor(ctx: RunContext, config: FloorConfig) -> RuleResult:
    reasons: list[dict] = []

    # 1. PO match must be confident and unambiguous.
    if ctx.matched_po is None or ctx.match_status != MatchStatus.MATCHED:
        if ctx.match_status in _MATCH_REASONS:
            code, text = _MATCH_REASONS[ctx.match_status]
        elif ctx.matched_po is None:
            code, text = "no_po_matched", "no purchase order is matched"
        else:
            code, text = "po_match_not_confident", "the PO match is not marked as confident"
        reasons.append({"code": code, "message": text})

    # 2. The amount check must have been evaluable.
    assessment = assess_amount(ctx, config.amount_compare_field)
    if not assessment.evaluable and assessment.reason != NO_MATCHED_PO:
        reasons.append({"code": f"amount_not_evaluable:{assessment.reason}",
                        "message": f"the invoice amount cannot be compared to the PO balance ({assessment.reason})"})

    # 3 + 4. Required fields present, and confident enough.
    missing: list[str] = []
    low_confidence: list[dict] = []
    threshold = ctx.facts.settings.confidence_threshold if ctx.facts is not None else None
    for name in config.required_fields:
        field = getattr(ctx.extracted, name, None) if ctx.extracted is not None else None
        if field is None or is_missing(getattr(field, "value", None)):
            missing.append(name)
        elif threshold is not None and field.confidence < threshold:
            low_confidence.append({"field": name, "confidence": field.confidence, "threshold": threshold})
    if ctx.extracted is None:
        reasons.append({"code": "no_extraction", "message": "no extracted invoice is available"})
    elif missing:
        reasons.append({"code": "required_fields_missing", "message": f"required fields missing: {', '.join(missing)}",
                        "fields": missing})
    if low_confidence:
        names = ", ".join(f["field"] for f in low_confidence)
        reasons.append({"code": "required_fields_low_confidence",
                        "message": f"required fields below the confidence threshold: {names}", "fields": low_confidence})

    detail = {
        "match_status": ctx.match_status.value if ctx.match_status else None,
        "matched_po": ctx.matched_po.po_number if ctx.matched_po else None,
        "amount_evaluable": assessment.evaluable,
        "amount_reason": assessment.reason,
        "confidence_threshold": threshold,
        "reasons": reasons,
    }
    if not reasons:
        return RuleResult(rule_id=FLOOR_RULE_ID, outcome=Outcome.PASS, severity=0, outcome_key="floor_not_applied",
                          message="Engine floor not applied: PO confidently matched, amount comparable, "
                                  "required fields present and confident.", detail=detail)
    return RuleResult(rule_id=FLOOR_RULE_ID, outcome=Outcome.FLAG, severity=config.floor_severity,
                      outcome_key="floor_applied", detail=detail,
                      message="Engine floor applied (at least review): " + "; ".join(r["message"] for r in reasons) + ".")
