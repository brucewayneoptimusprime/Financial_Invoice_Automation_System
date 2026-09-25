"""Fixed wording for the deterministic explanation and drafts. These templates are what runs when no model is available, and what
replaces a model reply that fails the claim check; they say only what the digest says."""
from app.enums import Decision

DECISION_MEANING = {
    Decision.APPROVE: "All checks passed. The invoice is ready for payment and the amount has been committed against the purchase order.",
    Decision.REVIEW: "A person needs to look at this invoice before it can be paid. It has been placed in the review queue.",
    Decision.REQUEST_INFO: "Information is missing or unclear, so the vendor is being asked for it. A draft email has been prepared and nothing has been sent.",
    Decision.REJECT: "The invoice cannot be processed. It has been marked rejected and a draft has been prepared; nothing has been sent.",
}
NEXT_STEP = {
    Decision.APPROVE: "No action is needed; the invoice can be paid.",
    Decision.REVIEW: "A reviewer should check the flagged items in the review queue and approve or reject the invoice.",
    Decision.REQUEST_INFO: "Review the drafted email and send it to the vendor yourself; nothing is sent automatically.",
    Decision.REJECT: "Review the draft and tell the vendor yourself if that is appropriate; nothing is sent automatically.",
}

FIELD_LABELS = {
    "vendor_name": "the seller's name", "vendor_tax_id": "the seller's tax registration number", "vendor_address": "the seller's address",
    "document_type": "the document type", "invoice_number": "the invoice number", "invoice_date": "the invoice date",
    "currency": "the currency", "po_reference": "the purchase order number", "subtotal": "the subtotal", "tax": "the tax amount",
    "total": "the total amount", "line_items": "the line items",
}
CHECK_LABELS = {
    "line_math": "a line's quantity times unit price", "lines_vs_subtotal": "the sum of the lines against the subtotal",
    "subtotal_plus_tax_equals_total": "the subtotal plus tax against the total",
    "subtotal_plus_adjustments_equals_total": "the subtotal plus shipping and discounts against the total",
    "total_equals_subtotal_tax_included": "the subtotal against the total",
}
FAILURE_LABELS = {
    "password_protected": "the file is password-protected and cannot be opened", "blank_document": "the document appears to be blank",
}
DUPLICATE_LABELS = {
    "same_file_hash": "we have already received this exact file",
    "same_vendor_number_same_total": "we have already received an invoice with this number and amount",
    "same_vendor_number_different_total": "we have already received an invoice with this number but a different amount",
    "resubmission": "a previous invoice with this number was returned to you and this looks like a resubmission",
}

SIGNATURE = "Kind regards,\nAccounts Payable"


def join_labels(labels: list[str]) -> str:
    labels = [x for x in labels if x]
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]
