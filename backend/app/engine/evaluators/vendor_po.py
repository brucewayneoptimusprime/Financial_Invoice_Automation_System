"""Evaluators about who the invoice is from and which PO it belongs to:
vendor_status, po_found, po_ambiguity, vendor_po_mismatch, currency_mismatch, po_status."""
from typing import Any

from app.engine.evaluators.base import extracted_value, flag, money, not_evaluable, ok, system_side_failure
from app.engine.evaluators.registry import BaseParams, register
from app.engine.normalize import is_missing
from app.engine.po_status import derive_po_status
from app.enums import MatchStatus, POStatus, VendorStatus
from app.models.run import RunContext
from app.money import to_minor


def _candidate_summary(ctx: RunContext, n: int = 3) -> list[dict]:
    return [{"po_number": c.po_number, "score": c.score, "breakdown": c.breakdown, "reasons": c.reasons}
            for c in ctx.candidates[:n]]


def _matched_po_fact(ctx: RunContext):
    if ctx.matched_po is None or ctx.facts is None:
        return None
    return ctx.facts.po_by_id(ctx.matched_po.po_id)


# ------------------------------------------------------------------------------------ vendor_status

@register("vendor_status")
def vendor_status(ctx: RunContext, params: dict[str, Any]):
    name = extracted_value(ctx, "vendor_name")
    vm = ctx.matched_vendor
    if is_missing(name) and (vm is None or vm.vendor_id is None):
        return not_evaluable("missing:vendor_name", "the invoice has no vendor name")
    if is_missing(name):
        name = "(no name printed; identified by tax id)"
    detail = {"vendor_name": name,
              "match": None if vm is None else vm.model_dump(mode="json")}
    if vm is None:
        return flag(params, "unknown", f"Vendor '{name}' has not been resolved against known vendors.", detail)
    if vm.ambiguous:
        tied = [v for v in (ctx.facts.vendor_by_id(i) for i in vm.candidate_vendor_ids) if v is not None] \
            if ctx.facts is not None else []
        blocked = [v for v in tied if v.status == VendorStatus.BLOCKED]
        detail.update(blocked_candidate=bool(blocked),
                      candidate_vendors=[{"id": v.id, "name": v.name, "status": v.status.value} for v in tied])
        text = (f"Vendor '{name}' matches more than one known vendor (scores {vm.score:.2f} and "
                f"{vm.runner_up_score if vm.runner_up_score is not None else 'n/a'})")
        if blocked:                                      # stays severity 1 (a human decides), but the risk is spelled out
            text += ", including BLOCKED vendor " + " and ".join(f"'{v.name}'" for v in blocked)
        return flag(params, "ambiguous", text + ".", detail)
    vendor = ctx.facts.vendor_by_id(vm.vendor_id) if (ctx.facts is not None and vm.vendor_id is not None) else None
    if vendor is None:
        return flag(params, "unknown", f"Vendor '{name}' is not a known vendor.", detail)
    detail.update(vendor_id=vendor.id, vendor_status=vendor.status.value, matched_name=vendor.name)
    if vendor.status == VendorStatus.APPROVED:
        return ok(f"Vendor '{vendor.name}' is approved.", detail, "approved")
    if vendor.status == VendorStatus.BLOCKED:
        return flag(params, "blocked", f"Vendor '{vendor.name}' is BLOCKED.", detail, hard=True)
    return flag(params, "new", f"Vendor '{vendor.name}' is new (not yet approved).", detail)


# ------------------------------------------------------------------------------------ po_found

def _reference_hit(ctx: RunContext) -> bool:
    hits = ("reference:exact", "reference:contained", "reference:fuzzy")
    return any(r.startswith(hits) for c in ctx.candidates for r in c.reasons)


@register("po_found")
def po_found(ctx: RunContext, params: dict[str, Any]):
    status = ctx.match_status
    ref = ctx.extracted.po_reference if ctx.extracted is not None else None
    ref_value = None if ref is None else ref.value
    detail = {"match_status": status.value if status else None, "po_reference": ref_value,
              "reference_explicit": None if ref is None else ref.explicit, "candidates": _candidate_summary(ctx)}
    if status is None:
        return not_evaluable("match_not_run", "PO matching has not been run", detail)
    if status in (MatchStatus.MATCHED, MatchStatus.AMBIGUOUS):
        top = ctx.candidates[0] if ctx.candidates else None
        reasons = [] if top is None else top.reasons
        if is_missing(ref_value):
            how = "by other signals (no PO reference on the invoice)"
        elif "reference:exact" in reasons:
            how = "by its explicit reference" if ref.explicit else "by an inferred reference"
        elif any(r.startswith(("reference:contained", "reference:fuzzy")) for r in reasons):
            how = f"by a reference that only resembles it ('{ref_value}')"
        else:
            how = f"by other signals (the stated reference '{ref_value}' matches no PO)"
        detail["top_score"] = None if top is None else top.score
        return ok(f"A purchase order was found ({top.po_number if top else 'n/a'}) {how}.", detail, "found")
    # NO_CANDIDATES / LOW_SCORE
    if (code := system_side_failure(ctx)) is not None:
        return not_evaluable("extraction_failed_system_side",
                             f"the extraction failed on our side ({code}); the PO reference is unknown, not absent", detail)
    if is_missing(ref_value):
        return flag(params, "no_reference", "The invoice has no PO reference and no purchase order matches on other signals.", detail)
    if status == MatchStatus.NO_CANDIDATES or not _reference_hit(ctx):
        return flag(params, "reference_not_found", f"PO reference '{ref_value}' does not match any purchase order.", detail)
    return flag(params, "no_confident_match", f"PO reference '{ref_value}' resembles a purchase order but no candidate "
                "scored high enough to be a confident match.", detail)


# ------------------------------------------------------------------------------------ po_ambiguity

@register("po_ambiguity")
def po_ambiguity(ctx: RunContext, params: dict[str, Any]):
    if ctx.match_status is None:
        return not_evaluable("match_not_run", "PO matching has not been run")
    top = ctx.candidates[:2]
    detail = {"candidates": _candidate_summary(ctx, 2),
              "score_gap": round(top[0].score - top[1].score, 6) if len(top) == 2 and None not in (top[0].score, top[1].score) else None}
    if ctx.match_status == MatchStatus.AMBIGUOUS:
        names = " and ".join(c.po_number for c in top)
        gap = detail["score_gap"]
        return flag(params, "ambiguous", f"The PO match is ambiguous: {names} score too close together"
                    + (f" (gap {gap:.3f})." if gap is not None else "."), detail)
    return ok("The PO match is not ambiguous.", detail, "unambiguous")


# ------------------------------------------------------------------------------------ vendor_po_mismatch

@register("vendor_po_mismatch")
def vendor_po_mismatch(ctx: RunContext, params: dict[str, Any]):
    po = _matched_po_fact(ctx)
    if po is None:
        return not_evaluable("no_matched_po", "no purchase order is matched")
    name = extracted_value(ctx, "vendor_name")
    if is_missing(name):
        return not_evaluable("missing:vendor_name", "the invoice has no vendor name")
    vm = ctx.matched_vendor
    po_vendor = ctx.facts.vendor_by_id(po.vendor_id)
    detail = {"invoice_vendor": name, "po_number": po.po_number, "po_vendor_id": po.vendor_id,
              "po_vendor": None if po_vendor is None else po_vendor.name,
              "invoice_vendor_id": None if vm is None else vm.vendor_id}
    if vm is None or vm.vendor_id is None or vm.ambiguous:
        return flag(params, "vendor_unresolved", f"Invoice vendor '{name}' could not be resolved, so it cannot be "
                    f"confirmed as the vendor on {po.po_number}.", detail)
    if vm.vendor_id != po.vendor_id:
        return flag(params, "mismatch", f"Invoice vendor '{name}' differs from the vendor on {po.po_number} "
                    f"('{detail['po_vendor']}').", detail)
    return ok(f"Invoice vendor matches the vendor on {po.po_number}.", detail, "match")


# ------------------------------------------------------------------------------------ currency_mismatch

@register("currency_mismatch")
def currency_mismatch(ctx: RunContext, params: dict[str, Any]):
    po = _matched_po_fact(ctx)
    if po is None:
        return not_evaluable("no_matched_po", "no purchase order is matched")
    currency = extracted_value(ctx, "currency")
    if is_missing(currency):
        return not_evaluable("missing:currency", "the invoice has no currency")
    detail = {"invoice_currency": currency, "po_currency": po.currency, "po_number": po.po_number}
    if currency.upper() != po.currency.upper():
        return flag(params, "mismatch", f"Invoice currency {currency} differs from {po.po_number}'s currency "
                    f"{po.currency}; amounts are not comparable (no FX conversion).", detail)
    return ok(f"Invoice and PO are both in {po.currency}.", detail, "match")


# ------------------------------------------------------------------------------------ po_status

@register("po_status")
def po_status(ctx: RunContext, params: dict[str, Any]):
    po = _matched_po_fact(ctx)
    if po is None:
        return not_evaluable("no_matched_po", "no purchase order is matched")
    total_minor, net_minor = to_minor(po.total_amount), to_minor(po.net_committed)
    derived = derive_po_status(total_minor, net_minor, po.status)
    detail = {"po_number": po.po_number, "stored_status": po.status.value, "derived_status": derived.value,
              "total": po.total_amount, "net_committed": po.net_committed, "balance": po.balance, "currency": po.currency}
    if po.status == POStatus.CLOSED:
        return flag(params, "closed", f"{po.po_number} is closed; no further invoices should be billed against it.", detail)
    if po.status == POStatus.FULLY_BILLED or po.balance <= 0:
        return flag(params, "fully_billed", f"{po.po_number} is already fully billed (committed "
                    f"{money(po.net_committed, po.currency)} of {money(po.total_amount, po.currency)}).", detail)
    return ok(f"{po.po_number} still has {money(po.balance, po.currency)} available.", detail, "open")
