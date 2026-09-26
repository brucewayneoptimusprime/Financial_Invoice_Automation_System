"""po_line_unit_price: each confidently matched invoice line's unit price vs its PO line's unit price (line-item consumption).

Distinct from `arithmetic_consistency` (the invoice's own consistency) and from `amount_tolerance` (the invoice total vs the PO
balance). Allowance A = the lesser (default) or greater of `pct`% of the PO unit price and `abs`, in Decimal and not rounded to
cents (unit prices may carry more decimals). D = invoice unit price - PO unit price. `direction = above` (default) flags only
D > A; `both` also flags D < -A. A lower price with `above` passes and is recorded in the detail. Lines whose match is not
confident are skipped here (the reviewer handles them); no compared line at all is not evaluable, never a pass.
"""
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from app.engine.evaluators.base import flag, not_evaluable, ok
from app.engine.evaluators.registry import BaseParams, register
from app.engine.line_matching import price_allowance
from app.enums import LineMatchStatus
from app.models.run import RunContext


class LinePriceParams(BaseParams):
    pct: float = Field(default=1.0, ge=0.0)
    abs: Decimal = Field(default=Decimal("1.00"), ge=0)
    mode: Literal["lesser_of", "greater_of"] = "lesser_of"
    direction: Literal["above", "both"] = "above"


def _fmt(x: Decimal) -> str:
    return format(x.normalize() if x == x.to_integral() else x, "f")


@register("po_line_unit_price", LinePriceParams)
def po_line_unit_price(ctx: RunContext, params: dict[str, Any]):
    lm = ctx.line_matches
    if lm is None or lm.po_id is None:
        return not_evaluable("no_line_matching", "no purchase order was matched, so no invoice line was matched to a PO line")
    po = ctx.facts.po_by_id(lm.po_id) if ctx.facts else None
    if po is None:
        return not_evaluable("po_not_in_snapshot", "the matched purchase order is not in the snapshot")
    po_lines = {pl.line_no: pl for pl in po.lines}
    items = ctx.extracted.line_items if ctx.extracted else []
    compared, skipped = [], []
    for m in lm.lines:
        if m.status is not LineMatchStatus.MATCHED:
            skipped.append({"invoice_line_no": m.invoice_line_no, "reason": f"line match {m.status.value}"})
            continue
        inv = items[m.invoice_line_no - 1].unit_price if m.invoice_line_no <= len(items) else None
        pl = po_lines.get(m.po_line_no)
        if inv is None or pl is None or pl.unit_price is None:
            skipped.append({"invoice_line_no": m.invoice_line_no, "reason": "unit price missing"})
            continue
        allowance = price_allowance(pl.unit_price, params["pct"], Decimal(str(params["abs"])), params["mode"])
        diff = inv - pl.unit_price
        above, below = diff > allowance, diff < -allowance
        compared.append({"invoice_line_no": m.invoice_line_no, "po_line_no": pl.line_no, "invoice_unit_price": inv,
                         "po_unit_price": pl.unit_price, "difference": diff,
                         "relative_difference": None if pl.unit_price == 0 else float(diff / pl.unit_price),
                         "allowance": allowance, "above_po": above, "below_po": below,
                         "ok": not above and not (below and params["direction"] == "both")})
    detail: dict[str, Any] = {"po_number": po.po_number, "pct": params["pct"], "abs": params["abs"], "mode": params["mode"],
                              "direction": params["direction"], "lines": compared, "skipped": skipped}
    if not compared:
        return not_evaluable("no_compared_lines", "no invoice line has both a confident PO-line match and unit prices on both sides",
                             detail)
    over = [c for c in compared if c["above_po"]]
    under = [c for c in compared if c["below_po"]]
    if over:
        c = over[0]
        return flag(params, "price_above_po", f"Invoice line {c['invoice_line_no']} is priced {_fmt(c['invoice_unit_price'])} per unit, "
                    f"above PO {po.po_number} line {c['po_line_no']} at {_fmt(c['po_unit_price'])} by {_fmt(c['difference'])} "
                    f"(allowance {_fmt(c['allowance'])})" + (f"; {len(over) - 1} more line(s) above the PO price." if len(over) > 1 else "."),
                    detail)
    if under and params["direction"] == "both":
        c = under[0]
        return flag(params, "price_below_po", f"Invoice line {c['invoice_line_no']} is priced {_fmt(c['invoice_unit_price'])} per unit, "
                    f"below PO {po.po_number} line {c['po_line_no']} at {_fmt(c['po_unit_price'])} by {_fmt(-c['difference'])} "
                    f"(allowance {_fmt(c['allowance'])}).", detail)
    note = f" ({len(under)} line(s) priced below the PO, allowed with direction 'above')" if under else ""
    return ok(f"All {len(compared)} matched line(s) are priced within tolerance of {po.po_number}{note}.", detail, "within_tolerance")
