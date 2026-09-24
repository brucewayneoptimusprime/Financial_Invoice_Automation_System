"""Builtin rule rows seeded into the `rules` table on init.

Tolerance and required-field values come from config. Severities and the remaining params are
PROVISIONAL defaults for the rule families listed in SPEC 6.3 - the M1 rules engine will tune
them. After seeding, the DB rows are the source of truth (rules are data, not code).
"""
from app.config import Settings
from app.models.rules import Rule

# (id, name, type, severity_on_trigger)
_FAMILIES: list[tuple[str, str, str, int]] = [
    ("r_vendor_status", "Vendor is approved (not blocked)", "vendor_status", 3),
    ("r_po_found", "Invoice matches a purchase order", "po_found", 1),
    ("r_po_ambiguity", "PO match is unambiguous", "po_ambiguity", 1),
    ("r_tolerance_pct", "Invoice within tolerance of PO balance", "amount_tolerance", 1),
    ("r_arithmetic", "Lines, tax and totals are consistent", "arithmetic_consistency", 1),
    ("r_duplicate_exact", "Not an exact duplicate of a prior invoice", "duplicate_exact", 3),
    ("r_duplicate_fuzzy", "Not a near-duplicate of a prior invoice", "duplicate_fuzzy", 1),
    ("r_required_fields", "Required fields are present", "required_fields", 2),
    ("r_extraction_confidence", "Extraction confidence is sufficient", "extraction_confidence", 1),
    ("r_po_status", "PO is not already fully billed", "po_status", 1),
]


def builtin_rules(settings: Settings) -> list[Rule]:
    params: dict[str, dict] = {
        "r_tolerance_pct": {"pct": settings.tolerance_pct, "abs": settings.tolerance_abs},
        "r_required_fields": {"fields": list(settings.required_fields)},
    }
    return [
        Rule(
            id=rule_id,
            name=name,
            type=rule_type,
            params=params.get(rule_id, {}),
            severity_on_trigger=severity,
            source="builtin",
            enabled=True,
        )
        for rule_id, name, rule_type, severity in _FAMILIES
    ]
