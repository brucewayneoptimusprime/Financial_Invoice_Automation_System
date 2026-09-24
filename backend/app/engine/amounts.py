"""Amount-comparison inputs shared by the tolerance evaluator and the engine floor.

`assess_amount` answers one question: can the invoice amount be compared to the matched PO's
remaining balance at all? If not, `reason` says why. It never applies tolerance (see tolerance.py).
"""
from dataclasses import dataclass
from decimal import Decimal

from app.models.run import RunContext
from app.money import to_minor

# Reasons an amount check cannot be performed.
NO_MATCHED_PO = "no_matched_po"
NO_FACTS = "no_facts"
PO_NOT_IN_FACTS = "po_not_in_facts"
MISSING_AMOUNT = "missing_amount"
INVALID_AMOUNT = "invalid_amount"
NON_POSITIVE_AMOUNT = "non_positive_amount"
MISSING_CURRENCY = "missing_currency"
CURRENCY_MISMATCH = "currency_mismatch"


@dataclass(frozen=True)
class AmountAssessment:
    evaluable: bool
    reason: str | None = None
    invoice_minor: int | None = None
    balance_minor: int | None = None
    currency: str | None = None
    po_currency: str | None = None
    po_number: str | None = None
    compare_field: str = "total"

    @property
    def excess_minor(self) -> int | None:
        if self.invoice_minor is None or self.balance_minor is None:
            return None
        return self.invoice_minor - self.balance_minor


def assess_amount(ctx: RunContext, compare_field: str = "total") -> AmountAssessment:
    def no(reason: str, **kw) -> AmountAssessment:
        return AmountAssessment(evaluable=False, reason=reason, compare_field=compare_field, **kw)

    if ctx.matched_po is None:
        return no(NO_MATCHED_PO)
    if ctx.facts is None:
        return no(NO_FACTS)
    po = ctx.facts.po_by_id(ctx.matched_po.po_id)
    if po is None:
        return no(PO_NOT_IN_FACTS)
    extracted = ctx.extracted
    if extracted is None:
        return no(MISSING_AMOUNT, po_number=po.po_number, po_currency=po.currency)

    raw: Decimal | None = getattr(extracted, compare_field).value
    currency = extracted.currency.value
    common = dict(currency=currency, po_currency=po.currency, po_number=po.po_number)
    if raw is None:
        return no(MISSING_AMOUNT, **common)
    if currency is None:
        return no(MISSING_CURRENCY, **common)
    if currency.upper() != po.currency.upper():
        return no(CURRENCY_MISMATCH, **common)
    try:
        invoice_minor = to_minor(raw)
    except (ValueError, ArithmeticError):
        return no(INVALID_AMOUNT, **common)
    balance_minor = to_minor(po.balance)
    if invoice_minor <= 0:
        return AmountAssessment(False, NON_POSITIVE_AMOUNT, invoice_minor, balance_minor,
                                compare_field=compare_field, **common)
    return AmountAssessment(True, None, invoice_minor, balance_minor, compare_field=compare_field, **common)
