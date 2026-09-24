"""amount_tolerance: invoice amount vs the matched PO's remaining balance (semantics in tolerance.py)."""
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, field_validator

from app.config import get_settings
from app.engine import amounts as A
from app.engine.evaluators.base import flag, money, not_evaluable, ok
from app.engine.evaluators.registry import BaseParams, register
from app.engine.tolerance import evaluate_tolerance
from app.models.run import RunContext
from app.money import from_minor, to_minor


class ToleranceParams(BaseParams):
    pct: float = Field(default_factory=lambda: get_settings().tolerance_pct, ge=0.0)
    abs: float = Field(default_factory=lambda: get_settings().tolerance_abs, ge=0.0)  # major units, whole cents
    mode: Literal["lesser_of", "greater_of"] = Field(default_factory=lambda: get_settings().tolerance_mode)
    compare_field: Literal["total", "subtotal"] = Field(default_factory=lambda: get_settings().amount_compare_field)

    @field_validator("abs")
    @classmethod
    def _whole_cents(cls, v: float) -> float:
        to_minor(Decimal(str(v)))  # raises ValueError if it has sub-cent precision
        return v


@register("amount_tolerance", ToleranceParams)
def amount_tolerance(ctx: RunContext, params: dict[str, Any]):
    a = A.assess_amount(ctx, params["compare_field"])
    field = params["compare_field"]
    detail: dict[str, Any] = {"compare_field": field, "po_number": a.po_number, "invoice_currency": a.currency,
                              "po_currency": a.po_currency}
    if not a.evaluable:
        if a.reason == A.NON_POSITIVE_AMOUNT:
            return flag(params, "non_positive_total", f"The invoice {field} is {money(from_minor(a.invoice_minor), a.currency)}; "
                        "zero or negative amounts (credit notes) are not supported.",
                        {**detail, "invoice_amount": from_minor(a.invoice_minor)})
        if a.reason == A.INVALID_AMOUNT:
            return flag(params, "invalid_amount", f"The invoice {field} has more than two decimal places and cannot be "
                        "compared exactly.", detail)
        if a.reason == A.MISSING_AMOUNT:
            return not_evaluable(f"missing:{field}", f"the invoice has no {field}", detail)
        if a.reason == A.MISSING_CURRENCY:
            return not_evaluable("missing:currency", "the invoice has no currency", detail)
        if a.reason == A.CURRENCY_MISMATCH:
            return not_evaluable("currency_mismatch", "invoice and PO currencies differ (see the currency rule)", detail)
        return not_evaluable(a.reason or "not_evaluable", "no purchase order is matched", detail)

    t = evaluate_tolerance(a.invoice_minor, a.balance_minor, params["pct"], params["abs"], params["mode"])
    cur = a.currency
    detail.update(
        invoice_amount=from_minor(t.invoice_minor), balance=from_minor(t.balance_minor), excess=from_minor(t.excess_minor),
        allowance=from_minor(t.allowance_minor), pct=params["pct"], pct_allowance=from_minor(t.pct_allowance_minor),
        abs_allowance=from_minor(t.abs_allowance_minor), mode=t.mode,
        within_balance=t.within_balance, within_tolerance=t.within_tolerance,
    )
    inv, bal = money(detail["invoice_amount"], cur), money(detail["balance"], cur)
    if t.within_balance:
        return ok(f"Invoice {field} {inv} is within the remaining PO balance {bal}.", detail, "within_balance")
    which = "the lesser" if t.mode == "lesser_of" else "the greater"
    rule_text = (f"allowance {money(detail['allowance'], cur)} = {which} of {params['pct']}% of balance "
                 f"({money(detail['pct_allowance'], cur)}) and the fixed limit ({money(detail['abs_allowance'], cur)})")
    if t.within_tolerance:
        return ok(f"Invoice {field} {inv} exceeds the remaining PO balance {bal} by {money(detail['excess'], cur)}, "
                  f"within tolerance ({rule_text}).", detail, "within_tolerance")
    return flag(params, "over_tolerance", f"Invoice {field} {inv} exceeds the remaining PO balance {bal} by "
                f"{money(detail['excess'], cur)}, beyond tolerance ({rule_text}).", detail)
