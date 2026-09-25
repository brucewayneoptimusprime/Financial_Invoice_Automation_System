"""Drafts for request_info and reject: a vendor email (never sent) or, when the vendor must not be written to, an internal note.

Stage 2 provides the deterministic template draft (also the Stage 3 fallback). Only facts with a vendor-facing category are ever
used in a vendor email; vendor status, internal ids, rule ids, severities and thresholds never appear in one.
"""
from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.enums import Decision
from app.pipeline.digest import Fact, TrailDigest
from app.pipeline.templates import (CHECK_LABELS, DUPLICATE_LABELS, FAILURE_LABELS, FIELD_LABELS, SIGNATURE, join_labels)


@dataclass(frozen=True)
class Draft:
    kind: str                                   # "vendor_email" | "notification"
    to: str | None
    subject: str
    body: str
    source: str                                 # "template" | "llm"
    requested: tuple[str, ...] = ()             # one line per thing asked of the vendor / listed as a reason
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    fallback_reason: str | None = None
    attempts: int = 0


def draft_kind(digest: TrailDigest, settings: Settings) -> str:
    """A vendor email, unless a triggered outcome forbids writing to the vendor (a blocked vendor) or nothing is vendor-facing."""
    for f in digest.triggered:
        if f.rule_id and f.outcome_key in settings.no_vendor_email_outcomes.get(f.rule_id, ()):
            return "notification"
    return "vendor_email" if digest.vendor_facing else "notification"


def _label(field_name: str) -> str:
    return FIELD_LABELS.get(field_name, field_name.replace("_", " "))


def request_line(f: Fact) -> str:
    """One vendor-safe sentence for a vendor-facing fact (no internal ids, rule ids or severities)."""
    cat, detail = f.category, f.data.get("detail", {})
    if cat == "missing_fields":
        return f"Missing or unreadable information: {join_labels([_label(i) for i in f.items])}."
    if cat == "unclear_fields":
        return f"We could not read these values with enough certainty; please confirm them: {join_labels([_label(i) for i in f.items])}."
    if cat == "po_reference":
        ref = detail.get("po_reference")
        if f.outcome_key == "reference_not_found" and ref:
            return f"The purchase order reference {ref} on the invoice does not match any purchase order in our records; please confirm the correct one."
        return "Please tell us which purchase order (PO) number this invoice relates to."
    if cat == "arithmetic":
        failed = [c for c in detail.get("checks", []) if not c.get("ok")]
        if failed:
            c = failed[0]
            what = CHECK_LABELS.get(c["check"], "the amounts")
            return (f"The amounts on the invoice do not add up: {what} gives {c['expected']} but the invoice shows {c['actual']}. "
                    "Please send a corrected invoice.")
        return "The amounts on the invoice do not add up. Please send a corrected invoice."
    if cat == "currency":
        return (f"The invoice currency ({detail.get('invoice_currency')}) differs from the currency of the purchase order "
                f"({detail.get('po_currency')}); please confirm the correct currency.")
    if cat == "document_type":
        return f"The document appears to be a {str(detail.get('document_type', 'document')).replace('_', ' ')} rather than an invoice; please send the invoice."
    if cat == "duplicate":
        return f"This looks like a duplicate: {DUPLICATE_LABELS.get(f.outcome_key or '', 'we appear to have received it before')}."
    if cat == "unreadable_file":
        code = f.items[0] if f.items else ""
        return f"We could not process the file: {FAILURE_LABELS.get(code, 'it could not be read')}. Please send it again."
    return f.text


def _greeting(digest: TrailDigest) -> tuple[str, str, str]:
    inv = next(iter(digest.kind("invoice")), None)
    data = inv.data if inv else {}
    name = data.get("vendor_name")
    number, date = data.get("number"), data.get("date")
    ref = "your invoice" + (f" {number}" if number else "") + (f" dated {date}" if date else "")
    return (f"Dear {name}," if name else "Dear Sir or Madam,"), ref, (number or "")


def template_draft(digest: TrailDigest, settings: Settings, fallback_reason: str | None = None, attempts: int = 0) -> Draft:
    kind = draft_kind(digest, settings)
    greeting, ref, number = _greeting(digest)
    if kind == "notification":
        requested = tuple(f.text for f in digest.triggered)
        subject = f"Invoice {number or '(number unknown)'}: {digest.decision.value.replace('_', ' ')} - internal note"
        body = ("Internal note (no email was drafted to the vendor).\n\n"
                f"The invoice was {'rejected' if digest.decision is Decision.REJECT else 'held'} automatically. Reasons:\n"
                + "\n".join(f"- {r}" for r in requested) + "\n\nNothing was sent.")
        return Draft("notification", None, subject, body, "template", requested, fallback_reason=fallback_reason, attempts=attempts)
    requested = tuple(request_line(f) for f in digest.vendor_facing)
    bullets = "\n".join(f"- {r}" for r in requested)
    if digest.decision is Decision.REJECT:
        subject = f"Invoice {number}: unable to process" if number else "Your invoice: unable to process"
        body = (f"{greeting}\n\nThank you for sending {ref}. We are unable to process it, for the following reason(s):\n\n{bullets}\n\n"
                f"If you believe this is a mistake, please reply with the details.\n\n{SIGNATURE}")
    else:
        subject = f"Invoice {number}: information needed" if number else "Your invoice: information needed"
        body = (f"{greeting}\n\nThank you for sending {ref}. Before we can process it we need the following:\n\n{bullets}\n\n"
                f"Please reply with the information or a corrected invoice.\n\n{SIGNATURE}")
    return Draft("vendor_email", None, subject, body, "template", requested, fallback_reason=fallback_reason, attempts=attempts)


def draft_for(digest: TrailDigest, settings: Settings, **_ignored) -> Draft:
    """Stage 2: the template. (Stage 3 adds the model call in front of this, with this as the fallback.)"""
    return template_draft(digest, settings)
