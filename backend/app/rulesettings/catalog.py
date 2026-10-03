"""The editable values: where each global lives, its per-PO column, its range, and which direction is "looser than default".

ONE definition used by the loader (effective values), the API (validation) and the UI (via GET /api/settings). Values travel as
JSON-friendly types: percent / threshold as numbers, money as exact decimal strings ("50.00"), days as integers, mode as a string.
"""
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.config import LOCKED_RULE_IDS, Settings, get_settings
from app.money import from_minor, to_minor

FLOORS = (("engine_floor", "Engine floor",
           "Always on: the system's own minimum checks (confident PO match, comparable amount, required fields), not a rule."),
          ("engine_floor_reference", "Reference floor",
           "Always on: a PO reached through a resembling or unmatched printed reference is never auto-approved."))
LOCK_REASONS = {"r_duplicate_exact": "Locked: an exact duplicate must always be caught.",
                "r_vendor_status": "Locked: a blocked or unknown vendor must always be caught."}
SWITCHABLE_RULES = ("r_po_found", "r_po_ambiguity", "r_vendor_po_mismatch", "r_currency_mismatch", "r_tolerance_pct", "r_arithmetic",
                    "r_duplicate_fuzzy", "r_required_fields", "r_extraction_confidence", "r_document_type", "r_po_status",
                    "r_po_line_price")
MODES = ("lesser_of", "greater_of")


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    kind: str                 # percent | money | mode | threshold | days
    column: str               # po_settings column
    rule_id: str | None       # the rule whose params hold the global (None: the settings table)
    param: str | None
    minimum: Decimal | None
    maximum: Decimal | None
    step: Decimal | None
    looser_when: str          # higher | lower | greater_of
    help: str


DEFS: tuple[SettingDef, ...] = (
    SettingDef("tolerance_pct", "Tolerance over the PO balance (percent)", "percent", "tolerance_pct", "r_tolerance_pct", "pct",
               Decimal("0"), Decimal("25"), Decimal("0.01"), "higher", "How far above the remaining PO balance an invoice may go, as a percentage of that balance."),
    SettingDef("tolerance_abs", "Tolerance over the PO balance (amount)", "money", "tolerance_abs_minor", "r_tolerance_pct", "abs",
               Decimal("0"), Decimal("1000000"), Decimal("0.01"), "higher", "The same limit as an amount, in the invoice's / PO's currency (the global default has no currency)."),
    SettingDef("tolerance_mode", "How the two tolerance limits combine", "mode", "tolerance_mode", "r_tolerance_pct", "mode",
               None, None, None, "greater_of", "Both limits (the smaller one applies, stricter) or either limit (the larger one applies)."),
    SettingDef("confidence_threshold", "Required-field confidence threshold", "threshold", "confidence_threshold", None, None,
               Decimal("0.5"), Decimal("0.99"), Decimal("0.01"), "lower", "A required field read with less confidence than this sends the invoice to review."),
    SettingDef("duplicate_days", "Near-duplicate window (days)", "days", "duplicate_days", "r_duplicate_fuzzy", "days",
               Decimal("0"), Decimal("90"), Decimal("1"), "lower", "Same vendor and amount, a different number, and invoice dates within this many days count as a near-duplicate."),
    SettingDef("duplicate_amount", "Near-duplicate amount tolerance", "money", "duplicate_amount_minor", "r_duplicate_fuzzy", "amount_tolerance",
               Decimal("0"), Decimal("10000"), Decimal("0.01"), "lower", "How far apart two amounts may be and still count as the same (0 = exact)."),
)
BY_KEY = {d.key: d for d in DEFS}


class SettingsError(ValueError):
    def __init__(self, problems: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = problems


# ------------------------------------------------------------------------------------------ values: normalise, validate, compare

def _dec(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("not a number")
    return Decimal(str(value))


def normalise(key: str, value: Any) -> Any:
    """The canonical JSON-friendly value, or ValueError with a plain reason (the range is part of the message)."""
    d = BY_KEY[key]
    if d.kind == "mode":
        if value not in MODES:
            raise ValueError("must be lesser_of (both limits) or greater_of (either limit)")
        return value
    try:
        num = _dec(value)
    except Exception:
        raise ValueError("must be a number") from None
    if not num.is_finite() or num < d.minimum or num > d.maximum:
        raise ValueError(f"must be between {d.minimum} and {d.maximum}")
    if (num / d.step) != (num / d.step).to_integral_value():
        raise ValueError(f"must be a multiple of {d.step}")
    if d.kind == "days":
        return int(num)
    if d.kind == "money":
        return str(num.quantize(Decimal("0.01")))
    return float(num.quantize(d.step))


def validate(values: dict[str, Any]) -> dict[str, Any]:
    out, problems = {}, {}
    for key, value in values.items():
        if key not in BY_KEY:
            problems[key] = "is not an editable setting"
            continue
        try:
            out[key] = normalise(key, value)
        except ValueError as exc:
            problems[key] = str(exc)
    if problems:
        raise SettingsError(problems)
    return out


def is_looser(key: str, value: Any, default: Any) -> bool:
    d = BY_KEY[key]
    if value is None or default is None:
        return False
    if d.looser_when == "greater_of":
        return value == "greater_of" and default != "greater_of"
    v, g = _dec(value), _dec(default)
    return v > g if d.looser_when == "higher" else v < g


# ------------------------------------------------------------------------------------------ reading the layers

def confidence_threshold_global(conn: sqlite3.Connection, settings: Settings | None = None) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key = 'confidence_threshold'").fetchone()
    return json.loads(row["value"]) if row else (settings or get_settings()).confidence_threshold


def _rule_params(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["id"]: json.loads(r["params"]) for r in conn.execute("SELECT id, params FROM rules")}


def global_values(conn: sqlite3.Connection, settings: Settings | None = None) -> dict[str, Any]:
    """The global default of every editable value, as stored today (rules.params / the settings table)."""
    s = settings or get_settings()
    params = _rule_params(conn)
    tol, dup = params.get("r_tolerance_pct", {}), params.get("r_duplicate_fuzzy", {})
    return {"tolerance_pct": float(tol.get("pct", s.tolerance_pct)),
            "tolerance_abs": str(_dec(tol.get("abs", s.tolerance_abs)).quantize(Decimal("0.01"))),
            "tolerance_mode": tol.get("mode", s.tolerance_mode),
            "confidence_threshold": float(confidence_threshold_global(conn, s)),
            "duplicate_days": int(dup.get("days", s.duplicate_fuzzy_days)),
            "duplicate_amount": str(_dec(dup.get("amount_tolerance", s.duplicate_fuzzy_amount_tolerance)).quantize(Decimal("0.01")))}


def builtin_values(settings: Settings | None = None) -> dict[str, Any]:
    """The built-in defaults (what a fresh database is seeded with), for "restore built-in default"."""
    s = settings or get_settings()
    return {"tolerance_pct": float(s.tolerance_pct), "tolerance_abs": str(_dec(s.tolerance_abs).quantize(Decimal("0.01"))),
            "tolerance_mode": s.tolerance_mode, "confidence_threshold": float(s.confidence_threshold),
            "duplicate_days": int(s.duplicate_fuzzy_days),
            "duplicate_amount": str(_dec(s.duplicate_fuzzy_amount_tolerance).quantize(Decimal("0.01")))}


def global_switches(conn: sqlite3.Connection) -> dict[str, bool]:
    return {r["id"]: bool(r["enabled"]) for r in conn.execute("SELECT id, enabled FROM rules ORDER BY id")}


def po_override_values(conn: sqlite3.Connection, po_id: int) -> dict[str, Any]:
    """Only the keys this PO overrides (canonical values)."""
    row = conn.execute("SELECT * FROM po_settings WHERE po_id = ?", (po_id,)).fetchone()
    if row is None:
        return {}
    out = {}
    for d in DEFS:
        raw = row[d.column]
        if raw is None:
            continue
        out[d.key] = str(from_minor(raw)) if d.kind == "money" else raw
    return out


def po_override_switches(conn: sqlite3.Connection, po_id: int) -> dict[str, bool]:
    return {r["rule_id"]: bool(r["enabled"]) for r in conn.execute(
        "SELECT rule_id, enabled FROM po_rule_switches WHERE po_id = ? ORDER BY rule_id", (po_id,))}


def to_column(key: str, value: Any) -> Any:
    """A canonical value as stored in po_settings (money as integer cents)."""
    return to_minor(Decimal(value)) if BY_KEY[key].kind == "money" else value


def is_locked(rule_id: str) -> bool:
    return rule_id in LOCKED_RULE_IDS
