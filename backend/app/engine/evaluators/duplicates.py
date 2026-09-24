"""duplicate_exact and duplicate_fuzzy.

Both exclude the current run. A second invoice against the same PO from the same vendor is NORMAL and is
never a duplicate by itself: exact needs the same file hash or the same vendor + normalised number, fuzzy
needs the same vendor + same amount + close date under a different number.

Only priors whose effective status is in `counted_statuses` (default approved / in_review / pending) can be
duplicated - a rejected or awaiting-info invoice was never paid, so a corrected resend with the same NUMBER
is fine. A resend of the byte-identical FILE, however, is flagged as a resubmission (severity 1).
Null never equals null: a missing hash or number matches nothing.
"""
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator

from app.config import get_settings
from app.engine.evaluators.base import flag, money, not_evaluable, ok
from app.engine.evaluators.registry import BaseParams, register
from app.engine.facts import PriorInvoiceFact
from app.engine.normalize import is_missing, normalize_identifier
from app.models.run import RunContext
from app.money import to_minor

EXACT_SEVERITY_DEFAULTS = {
    "same_file_hash": 3,
    "same_vendor_number_same_total": 3,
    "same_vendor_number_different_total": 1,
    "resubmission": 1,
}
_HARD_KEYS = ("same_file_hash", "same_vendor_number_same_total")  # reported as `fail`
_PRECEDENCE = ["same_file_hash", "same_vendor_number_same_total", "same_vendor_number_different_total", "resubmission"]


class DuplicateExactParams(BaseParams):
    severity_by_outcome: dict[str, int] = Field(default_factory=lambda: dict(EXACT_SEVERITY_DEFAULTS))
    counted_statuses: list[str] = Field(default_factory=lambda: list(get_settings().duplicate_counted_statuses))

    @field_validator("severity_by_outcome")
    @classmethod
    def _merge_defaults(cls, v: dict[str, int]) -> dict[str, int]:
        """A partial override must not drop the other outcomes' defaults (e.g. resubmission stays 1)."""
        return {**EXACT_SEVERITY_DEFAULTS, **v}


class DuplicateFuzzyParams(BaseParams):
    days: int = Field(default_factory=lambda: get_settings().duplicate_fuzzy_days, ge=0)
    amount_tolerance: float = Field(default_factory=lambda: get_settings().duplicate_fuzzy_amount_tolerance, ge=0.0)
    counted_statuses: list[str] = Field(default_factory=lambda: list(get_settings().duplicate_counted_statuses))


def _priors(ctx: RunContext) -> list[PriorInvoiceFact]:
    """Prior invoices, never including this run's own rows (the loader excludes them; this is a second guard)."""
    if ctx.facts is None:
        return []
    return [p for p in ctx.facts.prior_invoices if p.run_id is None or p.run_id != ctx.run_id]


def _total_minor(value: Decimal | None) -> int | None:
    try:
        return None if value is None else to_minor(value)
    except (ValueError, ArithmeticError):
        return None


@register("duplicate_exact", DuplicateExactParams)
def duplicate_exact(ctx: RunContext, params: dict[str, Any]):
    if ctx.facts is None:
        return not_evaluable("no_facts", "no procurement snapshot is available")
    counted = set(params["counted_statuses"])
    priors = _priors(ctx)
    number = None if ctx.extracted is None else ctx.extracted.invoice_number.value
    total = None if ctx.extracted is None else ctx.extracted.total.value
    vendor_id = None if ctx.matched_vendor is None else ctx.matched_vendor.vendor_id
    norm_number = None if is_missing(number) else normalize_identifier(number)

    skipped: list[str] = []
    if is_missing(ctx.file_hash):
        skipped.append("hash_check: missing:file_hash")
    if not norm_number or vendor_id is None:
        skipped.append("number_check: " + ("missing:invoice_number" if not norm_number else "vendor_unresolved"))
    if len(skipped) == 2:
        return not_evaluable("no_checkable_identifiers", "neither a file hash nor a vendor + invoice number is available",
                             {"skipped": skipped})

    matches: list[dict] = []
    for p in priors:
        counts = p.status.value in counted
        if not is_missing(ctx.file_hash) and not is_missing(p.file_hash) and p.file_hash == ctx.file_hash:
            matches.append({"prior_id": p.id, "kind": "same_file_hash" if counts else "resubmission",
                            "prior_status": p.status.value, "prior_invoice_number": p.invoice_number})
        if norm_number and vendor_id is not None and counts and p.vendor_id == vendor_id \
                and p.invoice_number and normalize_identifier(p.invoice_number) == norm_number:
            same_total = _total_minor(total) is not None and _total_minor(total) == _total_minor(p.total)
            matches.append({"prior_id": p.id,
                            "kind": "same_vendor_number_same_total" if same_total else "same_vendor_number_different_total",
                            "prior_status": p.status.value, "prior_invoice_number": p.invoice_number,
                            "prior_total": p.total, "invoice_total": total})
    detail = {"file_hash": ctx.file_hash, "normalized_invoice_number": norm_number, "vendor_id": vendor_id,
              "counted_statuses": sorted(counted), "skipped": skipped, "matches": matches}
    if not matches:
        return ok("No exact duplicate of a prior invoice was found.", detail, "no_duplicate")

    best = min(matches, key=lambda m: _PRECEDENCE.index(m["kind"]))["kind"]
    ids = sorted({m["prior_id"] for m in matches if m["kind"] == best})
    text = {
        "same_file_hash": "This is the same file as prior invoice(s) {ids}.",
        "same_vendor_number_same_total": "Same vendor, same invoice number and same total as prior invoice(s) {ids}.",
        "same_vendor_number_different_total": "Same vendor and invoice number as prior invoice(s) {ids} but a different "
                                             "total (possibly a revised invoice).",
        "resubmission": "Resubmission of a previously rejected / awaiting-info invoice (same file as prior invoice(s) {ids}).",
    }[best]
    return flag(params, best, text.format(ids=", ".join(map(str, ids))), detail, hard=best in _HARD_KEYS)


@register("duplicate_fuzzy", DuplicateFuzzyParams)
def duplicate_fuzzy(ctx: RunContext, params: dict[str, Any]):
    if ctx.facts is None or ctx.extracted is None:
        return not_evaluable("no_facts", "no procurement snapshot or extraction is available")
    vendor_id = None if ctx.matched_vendor is None else ctx.matched_vendor.vendor_id
    inv_date: date | None = ctx.extracted.invoice_date.value
    total = ctx.extracted.total.value
    total_minor = _total_minor(total)
    missing = [n for n, v in (("vendor", vendor_id), ("invoice_date", inv_date), ("total", total_minor)) if v is None]
    if missing:
        return not_evaluable("missing:" + ",".join(missing), "cannot compare: " + ", ".join(missing) + " unavailable")

    number = ctx.extracted.invoice_number.value
    norm_number = None if is_missing(number) else normalize_identifier(number)
    counted = set(params["counted_statuses"])
    tol_minor = to_minor(Decimal(str(params["amount_tolerance"])))
    currency = ctx.extracted.currency.value
    matches: list[dict] = []
    for p in _priors(ctx):
        if p.status.value not in counted or p.vendor_id != vendor_id or p.invoice_date is None:
            continue
        p_total = _total_minor(p.total)
        if p_total is None or abs(p_total - total_minor) > tol_minor:
            continue
        if currency and p.currency and currency.upper() != p.currency.upper():
            continue
        days_apart = abs((p.invoice_date - inv_date).days)
        if days_apart > params["days"]:
            continue
        if not is_missing(ctx.file_hash) and p.file_hash == ctx.file_hash:
            continue                                   # same file: the exact rule owns it
        if norm_number and p.invoice_number and normalize_identifier(p.invoice_number) == norm_number:
            continue                                   # same number: the exact rule owns it
        matches.append({"prior_id": p.id, "prior_invoice_number": p.invoice_number, "prior_date": p.invoice_date,
                        "prior_total": p.total, "days_apart": days_apart})
    detail = {"vendor_id": vendor_id, "invoice_total": total, "invoice_date": inv_date, "window_days": params["days"],
              "amount_tolerance": params["amount_tolerance"], "matches": matches}
    if not matches:
        return ok("No near-duplicate (same vendor, amount and close date) was found.", detail, "no_near_duplicate")
    first = matches[0]
    return flag(params, "near_duplicate",
                f"Possible duplicate: same vendor and amount ({money(total, currency)}) as prior invoice {first['prior_id']} "
                f"({first['prior_invoice_number'] or 'no number'}) dated {first['days_apart']} day(s) apart, under a different "
                f"invoice number." + (f" {len(matches)} similar prior invoices in total." if len(matches) > 1 else ""), detail)
