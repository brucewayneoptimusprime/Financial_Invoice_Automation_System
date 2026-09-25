"""Builtin rule rows seeded into the `rules` table on init.

Tolerance, rounding, duplicate-window and required-field values come from config. Severities are the M1
defaults (see SPEC section 11). After seeding, the DB rows are the source of truth (rules are data, not
code); re-running init never overwrites edits (INSERT OR IGNORE).

Per-outcome severities live in params.severity_by_outcome; `severity_on_trigger` is the default for any
outcome key without an override. Rules in config.LOCKED_RULE_IDS cannot be disabled (params stay editable).
"""
from app.config import Settings
from app.models.rules import Rule


def builtin_rules(settings: Settings) -> list[Rule]:
    s = settings
    # (id, name, type, default severity, params)
    specs: list[tuple[str, str, str, int, dict]] = [
        ("r_vendor_status", "Vendor is approved (not blocked)", "vendor_status", 1,
         {"severity_by_outcome": {"new": 1, "blocked": 3, "unknown": 1, "ambiguous": 1}}),
        ("r_po_found", "Invoice matches a purchase order", "po_found", 1,
         {"severity_by_outcome": {"no_reference": 2, "reference_not_found": 2, "no_confident_match": 1, "matched_without_reference": 1}}),
        ("r_po_ambiguity", "PO match is unambiguous", "po_ambiguity", 1, {}),
        ("r_vendor_po_mismatch", "Invoice vendor matches the PO's vendor", "vendor_po_mismatch", 1, {}),
        ("r_currency_mismatch", "Invoice currency matches the PO's currency", "currency_mismatch", 1, {}),
        ("r_tolerance_pct", "Invoice within tolerance of PO balance", "amount_tolerance", 1,
         {"pct": s.tolerance_pct, "abs": s.tolerance_abs, "mode": s.tolerance_mode, "compare_field": s.amount_compare_field,
          "severity_by_outcome": {"over_tolerance": 1, "non_positive_total": 1, "invalid_amount": 1}}),
        ("r_arithmetic", "Lines, tax and totals are consistent", "arithmetic_consistency", 1,
         {"rounding_per_term": s.arithmetic_rounding_per_term, "unit_price_rounding": s.arithmetic_unit_price_rounding}),
        ("r_duplicate_exact", "Not an exact duplicate of a prior invoice", "duplicate_exact", 3,
         {"counted_statuses": list(s.duplicate_counted_statuses),
          "severity_by_outcome": {"same_file_hash": 3, "same_vendor_number_same_total": 3,
                                  "same_vendor_number_different_total": 1, "resubmission": 1}}),
        ("r_duplicate_fuzzy", "Not a near-duplicate of a prior invoice", "duplicate_fuzzy", 1,
         {"days": s.duplicate_fuzzy_days, "amount_tolerance": s.duplicate_fuzzy_amount_tolerance,
          "counted_statuses": list(s.duplicate_counted_statuses)}),
        ("r_required_fields", "Required fields are present", "required_fields", 2,
         {"fields": list(s.required_fields)}),
        ("r_extraction_confidence", "Extraction confidence is sufficient", "extraction_confidence", 1,
         {"fields": list(s.required_fields)}),
        ("r_document_type", "The document is an invoice", "document_type", 1,
         {"allowed": ["invoice"], "severity_by_outcome": {"not_an_invoice": 1}}),
        ("r_po_status", "PO is not already fully billed", "po_status", 1,
         {"severity_by_outcome": {"fully_billed": 1, "closed": 3}}),
    ]
    return [
        Rule(id=rid, name=name, type=rtype, params=params, severity_on_trigger=severity, source="builtin", enabled=True)
        for rid, name, rtype, severity, params in specs
    ]
