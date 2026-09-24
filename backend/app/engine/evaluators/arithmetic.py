"""arithmetic_consistency: line math, lines vs subtotal, subtotal + adjustments + tax vs total.

Adjustments (shipping, discounts, credits, fees, rounding) are already signed by kind by the extraction post-process, so
the expected total is subtotal + sum(adjustment.amount) + tax.

Every sub-check that can run records expected / actual / difference / allowance; sub-checks whose
inputs are null are listed as skipped (null is missing regardless of confidence). Rounding allowance is
`rounding_per_term` (major units) multiplied by the number of terms in the comparison; the line-math check adds
`unit_price_rounding` per unit, because a printed unit price is rounded to the cent.
"""
from decimal import Decimal
from typing import Any

from pydantic import Field

from app.config import get_settings
from app.engine.evaluators.base import flag, not_evaluable, ok
from app.engine.evaluators.registry import BaseParams, register
from app.models.run import RunContext

D = Decimal


class ArithmeticParams(BaseParams):
    rounding_per_term: float = Field(default_factory=lambda: get_settings().arithmetic_rounding_per_term, ge=0.0)
    unit_price_rounding: float = Field(default_factory=lambda: get_settings().arithmetic_unit_price_rounding, ge=0.0)


def _check(name: str, expected: Decimal, actual: Decimal, allowance: Decimal, **extra: Any) -> dict:
    diff = abs(expected - actual)
    return {"check": name, "expected": expected, "actual": actual, "difference": diff, "allowance": allowance,
            "ok": diff <= allowance, **extra}


@register("arithmetic_consistency", ArithmeticParams)
def arithmetic_consistency(ctx: RunContext, params: dict[str, Any]):
    ex = ctx.extracted
    if ex is None:
        return not_evaluable("no_extraction", "no extracted invoice is available")
    per_term = D(str(params["rounding_per_term"]))
    unit_rounding = D(str(params["unit_price_rounding"]))
    checks: list[dict] = []
    skipped: list[dict] = []

    # 1. each line: quantity x unit_price = amount
    for i, line in enumerate(ex.line_items, start=1):
        if None in (line.quantity, line.unit_price, line.amount):
            missing = [n for n in ("quantity", "unit_price", "amount") if getattr(line, n) is None]
            skipped.append({"check": "line_math", "line": i, "reason": "missing:" + ",".join(missing)})
            continue
        allowance = per_term + abs(line.quantity) * unit_rounding      # the printed unit price is itself rounded
        checks.append(_check("line_math", line.quantity * line.unit_price, line.amount, allowance, line=i))

    # 2. sum of line amounts = subtotal
    subtotal, tax, total = ex.subtotal.value, ex.tax.value, ex.total.value
    line_amounts = [ln.amount for ln in ex.line_items]
    if not line_amounts:
        skipped.append({"check": "lines_vs_subtotal", "reason": "no_line_items"})
    elif any(a is None for a in line_amounts):
        skipped.append({"check": "lines_vs_subtotal", "reason": "missing:line_amount"})
    elif subtotal is None:
        skipped.append({"check": "lines_vs_subtotal", "reason": "missing:subtotal"})
    else:
        checks.append(_check("lines_vs_subtotal", sum(line_amounts, D(0)), subtotal, per_term * len(line_amounts),
                             terms=len(line_amounts)))

    # 3. subtotal + adjustments + tax = total (or without tax when the tax is already inside the total)
    included = ex.tax.included_in_total
    adjustments = [a.amount for a in ex.adjustments]
    adjusted = sum((a for a in adjustments if a is not None), D(0))
    extra = {"adjustments_total": adjusted, "adjustment_count": len(adjustments)}
    if subtotal is None or total is None:
        skipped.append({"check": "subtotal_tax_total", "reason": "missing:" + ("subtotal" if subtotal is None else "total")})
    elif any(a is None for a in adjustments):
        skipped.append({"check": "subtotal_tax_total", "reason": "missing:adjustment_amount"})
    elif included is True:
        checks.append(_check("total_equals_subtotal_tax_included", subtotal + adjusted, total, per_term * (1 + len(adjustments)),
                             tax_included_in_total=True, **extra))
    elif tax is None:
        # No tax line was found: the invoice is checked as "no tax printed" (tax = 0). A total that is higher than
        # subtotal + adjustments then shows up as an unexplained difference instead of being skipped.
        checks.append(_check("subtotal_plus_adjustments_equals_total", subtotal + adjusted, total,
                             per_term * (1 + len(adjustments)), tax_included_in_total=included, tax_assumed_zero=True, **extra))
    else:
        checks.append(_check("subtotal_plus_tax_equals_total", subtotal + adjusted + tax, total,
                             per_term * (2 + len(adjustments)), tax_included_in_total=False,
                             tax_flag_assumed=included is None, **extra))

    detail = {"checks": checks, "skipped": skipped, "rounding_per_term": per_term, "unit_price_rounding": unit_rounding}
    if not checks:
        return not_evaluable("no_checkable_amounts", "no arithmetic check could be performed", detail)
    failed = [c for c in checks if not c["ok"]]
    if failed:
        parts = "; ".join(
            f"{c['check']}{' (line ' + str(c['line']) + ')' if 'line' in c else ''}: expected {c['expected']:,.2f}, "
            f"found {c['actual']:,.2f} (difference {c['difference']:,.2f}, allowed {c['allowance']:,.2f})" for c in failed)
        detail["failed"] = [c["check"] for c in failed]
        return flag(params, "mismatch", f"The invoice arithmetic is inconsistent: {parts}.", detail)
    return ok(f"All {len(checks)} arithmetic check(s) are consistent"
              + (f" ({len(skipped)} skipped for missing data)." if skipped else "."), detail, "consistent")
