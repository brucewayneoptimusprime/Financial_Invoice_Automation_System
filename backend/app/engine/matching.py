"""Deterministic PO candidate generation, scoring and the match stage.

Score = sum of  weight * signal  over four signals, each in 0..1 (weights and thresholds: config.MatchConfig):
  reference : invoice PO reference vs PO number (normalised). exact 1.0, contained ('1001' in 'PO-1001')
              reference_contained_score, fuzzy sim * reference_fuzzy_factor (>= reference_fuzzy_min).
              A reference that is not marked explicit is multiplied by inferred_reference_factor.
  vendor    : the resolved vendor equals the PO's vendor (its match score; halved if the vendor is ambiguous).
  amount    : invoice amount vs the PO's remaining balance. Fits: 0.5 + 0.5 * (amount / balance);
              slightly over: 1 - excess/balance; way over or no balance: 0.  Not comparable -> 0.
  lines     : per invoice line, best description similarity (>= line_desc_min) blended with unit-price agreement.
A PO is a candidate only if reference, vendor or line overlap gives some evidence (amount alone never does).
Candidates are ranked by (score desc, po_number asc). `matched_po` is set only for a confident (top score >=
min_score), unambiguous (top-two gap >= ambiguity_margin, or runner-up below ambiguity_min_score) match.
"""
from dataclasses import dataclass
from decimal import Decimal

from app.config import MatchConfig, get_settings
from app.engine.facts import POFact, RunFacts
from app.engine.normalize import is_missing, normalize_identifier, normalize_text, similarity, token_similarity
from app.engine.line_matching import match_lines
from app.engine.vendor_match import resolve_vendor
from app.enums import LineMatchStatus, MatchStatus, Outcome, StageStatus
from app.models.audit import AuditEvent
from app.models.extraction import ExtractedInvoice
from app.models.run import POCandidate, RunContext, StageResult, VendorMatch
from app.money import to_minor

MATCH_STAGE = "match"


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    candidates: list[POCandidate]
    matched: POCandidate | None


def _r(x: float) -> float:
    return round(x, 6)


# ------------------------------------------------------------------------------------------ signals

def reference_signal(extracted: ExtractedInvoice, po: POFact, cfg: MatchConfig) -> tuple[float, list[str]]:
    ref = extracted.po_reference
    if is_missing(ref.value):
        return 0.0, ["reference:none"]
    n_ref, n_po = normalize_identifier(ref.value), normalize_identifier(po.po_number)
    if not n_ref or not n_po:
        return 0.0, ["reference:none"]
    short, long_ = sorted((n_ref, n_po), key=len)
    if n_ref == n_po:
        score, reason = 1.0, "reference:exact"
    elif len(short) >= cfg.reference_contained_min_len and long_.endswith(short):
        score, reason = cfg.reference_contained_score, "reference:contained"
    else:
        sim = similarity(n_ref, n_po)
        if sim >= cfg.reference_fuzzy_min:
            score, reason = sim * cfg.reference_fuzzy_factor, f"reference:fuzzy({sim:.2f})"
        else:
            return 0.0, ["reference:none"]
    reasons = [reason]
    if ref.explicit is not True:                        # inferred, or unknown whether explicit -> less trust
        score *= cfg.inferred_reference_factor
        reasons.append("reference:inferred")
    return score, reasons


def vendor_signal(vm: VendorMatch | None, po: POFact) -> tuple[float, list[str]]:
    if vm is None or vm.vendor_id is None:
        return 0.0, ["vendor:unresolved"]
    if vm.vendor_id != po.vendor_id:
        return 0.0, ["vendor:mismatch"]
    if vm.ambiguous:
        return vm.score * 0.5, ["vendor:match(ambiguous)"]
    return vm.score, ["vendor:match"]


def amount_signal(extracted: ExtractedInvoice, po: POFact, compare_field: str) -> tuple[float, list[str]]:
    raw = getattr(extracted, compare_field).value
    currency = extracted.currency.value
    if raw is None:
        return 0.0, [f"amount:not_comparable(missing {compare_field})"]
    if currency is None or currency.upper() != po.currency.upper():
        return 0.0, ["amount:not_comparable(currency)"]
    try:
        invoice = to_minor(raw)
    except (ValueError, ArithmeticError):
        return 0.0, ["amount:not_comparable(invalid)"]
    balance = to_minor(po.balance)
    if invoice <= 0:
        return 0.0, ["amount:not_comparable(non-positive)"]
    if balance <= 0:
        return 0.0, ["amount:no_balance"]
    if invoice <= balance:
        ratio = Decimal(invoice) / Decimal(balance)
        return float(Decimal("0.5") + Decimal("0.5") * ratio), [f"amount:fits({float(ratio):.2f} of balance)"]
    excess_ratio = Decimal(invoice - balance) / Decimal(balance)
    return float(max(Decimal(0), 1 - excess_ratio)), [f"amount:over(+{float(excess_ratio):.2%})"]


def lines_signal(extracted: ExtractedInvoice, po: POFact, cfg: MatchConfig) -> tuple[float, list[str]]:
    inv_lines = [ln for ln in extracted.line_items if not is_missing(ln.description)]
    po_lines = [ln for ln in po.lines if not is_missing(ln.description)]
    if not inv_lines or not po_lines:
        return 0.0, ["lines:none"]
    price_weight = 1.0 - cfg.line_desc_weight
    scores: list[float] = []
    for line in inv_lines:
        d_inv = normalize_text(line.description)
        best = 0.0
        for pl in po_lines:
            sim = token_similarity(d_inv, normalize_text(pl.description))
            if sim < cfg.line_desc_min:
                continue
            price_ok = line.unit_price is not None and pl.unit_price is not None and line.unit_price == pl.unit_price
            best = max(best, cfg.line_desc_weight * sim + (price_weight if price_ok else 0.0))
        scores.append(best)
    overlap = sum(scores) / len(scores)
    return overlap, [f"lines:overlap({overlap:.2f})"]


# ------------------------------------------------------------------------------------------ ranking

def score_candidate(extracted: ExtractedInvoice, vm: VendorMatch | None, po: POFact, cfg: MatchConfig,
                    compare_field: str = "total") -> POCandidate | None:
    ref, ref_reasons = reference_signal(extracted, po, cfg)
    ven, ven_reasons = vendor_signal(vm, po)
    amt, amt_reasons = amount_signal(extracted, po, compare_field)
    lin, lin_reasons = lines_signal(extracted, po, cfg)
    if not (ref > 0 or ven > 0 or lin >= cfg.candidate_line_min):
        return None                                     # no evidence beyond a coincidental amount
    breakdown = {"reference": _r(cfg.weight_reference * ref), "vendor": _r(cfg.weight_vendor * ven),
                 "amount": _r(cfg.weight_amount * amt), "lines": _r(cfg.weight_lines * lin)}
    return POCandidate(po_id=po.id, po_number=po.po_number, score=_r(min(1.0, sum(breakdown.values()))),
                       breakdown=breakdown, reasons=[*ref_reasons, *ven_reasons, *amt_reasons, *lin_reasons])


def classify(candidates: list[POCandidate], cfg: MatchConfig) -> MatchStatus:
    """Decide the match status from ranked candidates (best first)."""
    if not candidates:
        return MatchStatus.NO_CANDIDATES
    top = candidates[0]
    if top.score < cfg.min_score:
        return MatchStatus.LOW_SCORE
    if len(candidates) > 1:
        second = candidates[1]
        if second.score >= cfg.ambiguity_min_score and _r(top.score - second.score) < cfg.ambiguity_margin:
            return MatchStatus.AMBIGUOUS
    return MatchStatus.MATCHED


def rank_po_candidates(extracted: ExtractedInvoice | None, vm: VendorMatch | None, facts: RunFacts,
                       cfg: MatchConfig | None = None, compare_field: str = "total") -> MatchResult:
    cfg = cfg or MatchConfig()
    if extracted is None:
        return MatchResult(MatchStatus.NO_CANDIDATES, [], None)
    scored = [c for po in facts.purchase_orders
              if (c := score_candidate(extracted, vm, po, cfg, compare_field)) is not None]
    scored.sort(key=lambda c: (-c.score, c.po_number))
    candidates = scored[: cfg.max_candidates]
    status = classify(candidates, cfg)
    return MatchResult(status, candidates, candidates[0] if status == MatchStatus.MATCHED else None)


# ------------------------------------------------------------------------------------------ stage

def _event(event_type: str, outcome: Outcome, message: str, detail: dict) -> AuditEvent:
    return AuditEvent(stage=MATCH_STAGE, event_type=event_type, outcome=outcome, message=message, detail=detail)


def run_match_stage(ctx: RunContext, cfg: MatchConfig | None = None, compare_field: str | None = None) -> StageResult:
    """Resolve the vendor, rank PO candidates and set ctx.matched_vendor / candidates / matched_po / match_status."""
    settings = get_settings()
    cfg = cfg or settings.match
    compare_field = compare_field or settings.amount_compare_field
    events: list[AuditEvent] = []

    if ctx.facts is None:
        ctx.matched_vendor, ctx.candidates, ctx.matched_po = None, [], None
        ctx.match_status = MatchStatus.NO_CANDIDATES
        events.append(_event("match_skipped", Outcome.FLAG, "No procurement snapshot is available; nothing to match against.", {}))
        return StageResult(stage=MATCH_STAGE, status=StageStatus.FLAGGED,
                           outputs={"match_status": ctx.match_status.value, "matched_po": None}, events=events)

    name = None if ctx.extracted is None else ctx.extracted.vendor_name.value
    tax_id = None if ctx.extracted is None else ctx.extracted.vendor_tax_id.value
    vm = resolve_vendor(name, ctx.facts.vendors, cfg, tax_id=tax_id)
    vendor = ctx.facts.vendor_by_id(vm.vendor_id) if vm.vendor_id is not None else None
    ctx.matched_vendor = vm
    events.append(_event(
        "vendor_resolved", Outcome.INFO if vm.vendor_id is not None else Outcome.FLAG,
        (f"Vendor '{name}' resolved to '{vendor.name}' ({vm.method}, score {vm.score:.2f})"
         + ("; AMBIGUOUS with another vendor." if vm.ambiguous else ".")) if vendor
        else f"Vendor '{name}' did not match any known vendor." if not is_missing(name) else "The invoice has no vendor name.",
        {"vendor_name": name, **vm.model_dump(mode="json")},
    ))

    result = rank_po_candidates(ctx.extracted, vm, ctx.facts, cfg, compare_field)
    ctx.candidates, ctx.matched_po, ctx.match_status = result.candidates, result.matched, result.status
    ranked = [{"po_number": c.po_number, "score": c.score, "breakdown": c.breakdown, "reasons": c.reasons}
              for c in result.candidates]
    events.append(_event("po_candidates_ranked", Outcome.INFO,
                         f"{len(ranked)} candidate purchase order(s) ranked.", {"candidates": ranked,
                                                                                  "min_score": cfg.min_score,
                                                                                  "ambiguity_margin": cfg.ambiguity_margin}))
    top = result.candidates[0] if result.candidates else None
    matched = result.status == MatchStatus.MATCHED
    explain = {
        MatchStatus.MATCHED: f"Matched {top.po_number if top else ''} (score {top.score if top else 0:.2f}).",
        MatchStatus.NO_CANDIDATES: "No candidate purchase order was found.",
        MatchStatus.LOW_SCORE: f"Best candidate {top.po_number if top else ''} scored {top.score if top else 0:.2f}, "
                               f"below the minimum {cfg.min_score:.2f}; no confident match.",
        MatchStatus.AMBIGUOUS: "Ambiguous: " + " and ".join(f"{c.po_number} ({c.score:.2f})" for c in result.candidates[:2])
                               + f" are within {cfg.ambiguity_margin:.2f} of each other; no match was chosen.",
    }[result.status]
    events.append(_event("po_match_decision", Outcome.PASS if matched else Outcome.FLAG, explain,
                         {"match_status": result.status.value, "matched_po": top.po_number if matched and top else None}))

    # Line-item matching against the matched PO only (stored for the reviewer; it changes no decision).
    po_fact = ctx.facts.po_by_id(result.matched.po_id) if result.matched is not None else None
    ctx.line_matches = match_lines(ctx.extracted, po_fact, settings.line_match)
    lm = ctx.line_matches
    counts = {s.value: sum(1 for ln in lm.lines if ln.status is s) for s in LineMatchStatus}
    events.append(_event(
        "po_lines_matched", Outcome.INFO,
        (f"Line matching against {lm.po_number}: {lm.mode.replace('_', ' ')} ("
         + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in counts.items() if n) + ")."
         + (" One bundled line against an itemised PO." if lm.bundled_hint else "")) if lm.po_id is not None
        else f"Line matching not evaluated: {lm.reason}.",
        {**lm.model_dump(mode="json"), "counts": counts}))
    return StageResult(
        stage=MATCH_STAGE, status=StageStatus.OK if matched else StageStatus.FLAGGED,
        outputs={"match_status": result.status.value, "matched_po": top.po_number if matched and top else None,
                 "vendor_id": vm.vendor_id, "candidate_count": len(ranked)},
        events=events,
    )
