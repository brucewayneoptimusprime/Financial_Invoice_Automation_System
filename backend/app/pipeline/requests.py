"""One vendor-safe sentence for each vendor-facing fact: what we ask of the vendor, with no internal ids, rule ids or severities.
Used by the template draft, as the only text the drafter model is given, and by the drafter's checker."""
from app.pipeline.digest import Fact
from app.pipeline.templates import CHECK_LABELS, DUPLICATE_LABELS, FAILURE_LABELS, FIELD_LABELS, join_labels


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
