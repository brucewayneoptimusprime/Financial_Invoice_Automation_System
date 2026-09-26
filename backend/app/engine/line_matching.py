"""Invoice line -> PO line matching (line-item PO consumption; deterministic, no LLM).

Runs in the match stage, ONLY against the confidently matched PO (owner decision 6). For each invoice line, every PO line with
a description is scored; score = sum of weight * signal (weights and thresholds: config.LineMatchConfig):

  description  max(token_similarity, containment). Containment = share of the SHORTER description's tokens found in the longer
               one, counted only when the shorter has at least `containment_min_tokens` tokens. Item codes (letters and digits
               joined by hyphens, or the invoice line's item_code): the same code on both sides gives 1.0; different codes on
               both sides cap the description at `code_conflict_cap` (two different products with similar names).
  price        1.0 within the price tolerance; else linear down to 0 at `price_band` relative difference; 0.5 if a price is missing.
  quantity     invoice qty <= the line's remaining qty: 1.0; over: 1 - excess/remaining (floor 0); nothing remaining: 0; missing: 0.5.
  amount       the same shape against the line's remaining amount.

A PO line is a candidate only with description >= `description_min` (amount or price alone never make one). Status per line:
matched (top >= min_score and no close runner-up), ambiguous (a runner-up >= ambiguity_min_score within ambiguity_margin),
no_match, not_evaluable (no description). Invoice lines are taken in order and a confident match reduces that PO line's
remaining quantity and amount for the NEXT invoice line (split deliveries; owner decision 9). Mode: line_level (every evaluable
line matched), total_only (no evaluable line has any candidate: no line correspondence; owner decision 4), partial, not_evaluable.

Price is a minor weight on purpose: the same item at a wrong price must still match confidently, so r_po_line_price can flag it.
Nothing here changes a decision (owner decision 3).
"""
import re
from decimal import Decimal

from app.config import LineMatchConfig
from app.engine.facts import POFact, POLineFact
from app.engine.normalize import is_missing, normalize_text, token_similarity
from app.enums import LineMatchStatus
from app.models.extraction import ExtractedInvoice, ExtractedLineItem
from app.models.run import InvoiceLineMatch, LineCandidate, LineMatchSet

_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+(?![A-Za-z0-9])")


def _r(x: float) -> float:
    return round(x, 6)


def codes_in(text: str | None) -> set[str]:
    """Item codes: hyphen-joined tokens that contain both letters and digits ("FUR-CH-4682", "TEC-CO-4575")."""
    if not text:
        return set()
    return {m.upper() for m in _CODE.findall(text) if re.search(r"\d", m) and re.search(r"[A-Za-z]", m)}


def description_signal(inv_desc: str, inv_code: str | None, po_desc: str, cfg: LineMatchConfig) -> tuple[float, list[str]]:
    a, b = normalize_text(inv_desc), normalize_text(po_desc)
    sim = token_similarity(a, b)
    ta, tb = set(a.split()), set(b.split())
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    containment = len(short & long_) / len(short) if short and len(short) >= cfg.containment_min_tokens else 0.0
    value, reasons = max(sim, containment), [f"description:{max(sim, containment):.2f}"
                                               + ("(contained)" if containment > sim else "")]
    inv_codes = codes_in(inv_desc) | ({inv_code.strip().upper()} if inv_code and inv_code.strip() else set())
    po_codes = codes_in(po_desc)
    if inv_codes & po_codes:
        return 1.0, [*reasons, f"code:exact({sorted(inv_codes & po_codes)[0]})"]
    if inv_codes and po_codes:
        return min(value, cfg.code_conflict_cap), [*reasons, "code:conflict"]
    return value, reasons


def price_allowance(po_price: Decimal, pct: float, abs_: Decimal, mode: str) -> Decimal:
    by_pct = abs(po_price) * Decimal(str(pct)) / Decimal(100)
    return min(by_pct, abs_) if mode == "lesser_of" else max(by_pct, abs_)


def price_signal(inv: Decimal | None, po: Decimal | None, cfg: LineMatchConfig) -> tuple[float, str]:
    if inv is None or po is None:
        return 0.5, "price:unknown"
    diff = abs(inv - po)
    if diff <= price_allowance(po, cfg.price_pct, cfg.price_abs, cfg.price_mode):
        return 1.0, "price:same"
    if po == 0:
        return 0.0, "price:differs"
    rel = float(diff / abs(po))
    return max(0.0, 1.0 - rel / cfg.price_band), f"price:differs({rel:.1%})"


def fit_signal(kind: str, wanted: Decimal | None, remaining: Decimal | None) -> tuple[float, str]:
    if wanted is None or remaining is None:
        return 0.5, f"{kind}:unknown"
    if remaining <= 0:
        return 0.0, f"{kind}:none_remaining"
    if wanted <= remaining:
        return 1.0, f"{kind}:fits"
    return max(0.0, float(1 - (wanted - remaining) / remaining)), f"{kind}:over"


def score_line(line: ExtractedLineItem, pl: POLineFact, remaining_qty: Decimal | None, remaining_amt: Decimal | None,
               cfg: LineMatchConfig) -> LineCandidate | None:
    if is_missing(line.description) or is_missing(pl.description):
        return None
    desc, reasons = description_signal(line.description, line.item_code, pl.description, cfg)
    if desc < cfg.description_min:
        return None
    price, p_reason = price_signal(line.unit_price, pl.unit_price, cfg)
    qty, q_reason = fit_signal("quantity", line.quantity, remaining_qty)
    amt, a_reason = fit_signal("amount", line.amount, remaining_amt)
    breakdown = {"description": _r(cfg.weight_description * desc), "price": _r(cfg.weight_price * price),
                 "quantity": _r(cfg.weight_quantity * qty), "amount": _r(cfg.weight_amount * amt)}
    return LineCandidate(po_line_id=pl.id, po_line_no=pl.line_no, score=_r(min(1.0, sum(breakdown.values()))), breakdown=breakdown,
                         reasons=[*reasons, p_reason, q_reason, a_reason])


def _classify(cands: list[LineCandidate], cfg: LineMatchConfig) -> LineMatchStatus:
    if not cands or cands[0].score < cfg.min_score:
        return LineMatchStatus.NO_MATCH
    if len(cands) > 1 and cands[1].score >= cfg.ambiguity_min_score and _r(cands[0].score - cands[1].score) < cfg.ambiguity_margin:
        return LineMatchStatus.AMBIGUOUS
    return LineMatchStatus.MATCHED


def match_lines(extracted: ExtractedInvoice | None, po: POFact | None, cfg: LineMatchConfig | None = None) -> LineMatchSet:
    cfg = cfg or LineMatchConfig()
    if po is None:
        return LineMatchSet(mode="not_evaluable", reason="no confidently matched purchase order")
    base = {"po_id": po.id, "po_number": po.po_number}
    items = [] if extracted is None else list(extracted.line_items)
    if not items:
        return LineMatchSet(**base, mode="not_evaluable", reason="the invoice has no line items")
    po_lines = [pl for pl in po.lines if not is_missing(pl.description)]
    if not po_lines:
        return LineMatchSet(**base, mode="not_evaluable", reason="the purchase order has no described lines",
                            lines=[InvoiceLineMatch(invoice_line_no=i, status=LineMatchStatus.NOT_EVALUABLE,
                                                    reason="the purchase order has no described lines") for i in range(1, len(items) + 1)])
    remaining = {pl.line_no: [pl.remaining_quantity, pl.remaining_amount] for pl in po_lines}
    results: list[InvoiceLineMatch] = []
    for i, line in enumerate(items, start=1):
        if is_missing(line.description):
            results.append(InvoiceLineMatch(invoice_line_no=i, status=LineMatchStatus.NOT_EVALUABLE, reason="the line has no description"))
            continue
        cands = [c for pl in po_lines if (c := score_line(line, pl, *remaining[pl.line_no], cfg)) is not None]
        cands.sort(key=lambda c: (-c.score, c.po_line_no))
        cands = cands[: cfg.max_candidates]
        status = _classify(cands, cfg)
        top = cands[0] if cands else None
        if status is LineMatchStatus.MATCHED:
            rq, ra = remaining[top.po_line_no]
            remaining[top.po_line_no] = [None if rq is None or line.quantity is None else rq - line.quantity,
                                         None if ra is None or line.amount is None else ra - line.amount]
        results.append(InvoiceLineMatch(
            invoice_line_no=i, status=status, score=None if top is None else top.score, candidates=cands,
            po_line_id=top.po_line_id if status is LineMatchStatus.MATCHED else None,
            po_line_no=top.po_line_no if status is LineMatchStatus.MATCHED else None,
            reason=None if cands else "no PO line has a similar description"))
    evaluable = [r for r in results if r.status is not LineMatchStatus.NOT_EVALUABLE]
    if not evaluable:
        mode = "not_evaluable"
    elif all(r.status is LineMatchStatus.MATCHED for r in evaluable):
        mode = "line_level"
    elif all(not r.candidates for r in evaluable):
        mode = "total_only"
    else:
        mode = "partial"
    bundled = mode == "total_only" and len(items) == 1 and len(po_lines) >= 2
    return LineMatchSet(**base, mode=mode, bundled_hint=bundled, lines=results)
