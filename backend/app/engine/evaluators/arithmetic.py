"""arithmetic_consistency: line math, lines vs subtotal, subtotal + tax vs total.

Every sub-check that can run records expected / actual / difference / allowance; sub-checks whose
inputs are null are listed as skipped (null is missing regardless of confidence). Rounding allowance is
`rounding_per_term` (major units) multiplied by the number of terms in the comparison.
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
    checks: list[dict] = []
    skipped: list[dict] = []

    # 1. each line: quantity x unit_price = amount
    for i, line in enumerate(ex.line_items, start=1):
        if None in (line.quantity, line.unit_price, line.amount):
            missing = [n for n in ("quantity", "unit_price", "amount") if getattr(line, n) is None]
            skipped.append({"check": "line_math", "line": i, "reason": "missing:" + ",".join(missing)})
            continue
        checks.append(_check("line_math", line.quantity * line.unit_price, line.amount, per_term, line=i))

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

    # 3. subtotal + tax = total (or subtotal = total when tax is already inside the total)
    included = ex.tax.included_in_total
    if subtotal is None or total is None:
        skipped.append({"check": "subtotal_tax_total", "reason": "missing:" + ("subtotal" if subtotal is None else "total")})
    elif included is True:
        checks.append(_check("total_equals_subtotal_tax_included", subtotal, total, per_term, tax_included_in_total=True))
    elif tax is None:
        skipped.append({"check": "subtotal_tax_total", "reason": "missing:tax"})
    else:
        checks.append(_check("subtotal_plus_tax_equals_total", subtotal + tax, total, per_term * 2,
                             tax_included_in_total=False, tax_flag_assumed=included is None))

    detail = {"checks": checks, "skipped": skipped, "rounding_per_term": D(str(params["rounding_per_term"]))}
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
