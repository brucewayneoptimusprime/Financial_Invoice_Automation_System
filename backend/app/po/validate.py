"""Deterministic checks for a PO before it is saved (shared by the form, text and document paths; PLAN "PO integration" section 2).

Errors block Save. Warnings are shown and may be saved anyway once a person has seen them. Nothing here fixes a value.
"""
import json
import re
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

from app.config import Settings
from app.engine.facts import VendorFact
from app.engine.normalize import normalize_identifier
from app.engine.vendor_match import resolve_vendor, tax_ids_match
from app.enums import VendorStatus
from app.extraction.parsing import normalize_amount_string
from app.money import from_minor, to_minor
from app.po.models import NewVendorIn, ParsedLine, ParsedPO, POCreate, POIssue

_CURRENCY = re.compile(r"^[A-Z]{3}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_vendor_facts(conn: sqlite3.Connection) -> list[VendorFact]:
    rows = conn.execute("SELECT id, name, aliases, tax_id, country, status FROM vendors ORDER BY id").fetchall()
    return [VendorFact(id=r["id"], name=r["name"], aliases=tuple(json.loads(r["aliases"] or "[]")), tax_id=r["tax_id"],
                       country=r["country"], status=VendorStatus(r["status"])) for r in rows]


def _money(raw: str | None, where: str, issues: list[POIssue], *, required: bool, label: str) -> int | None:
    if raw is None or raw == "":
        if required:
            issues.append(POIssue(where, "error", "required", f"{label} is required."))
        return None
    text = normalize_amount_string(raw)
    if text is None:
        issues.append(POIssue(where, "error", "not_a_number", f"{label} '{raw}' is not an amount."))
        return None
    if text.startswith("-"):
        issues.append(POIssue(where, "error", "negative", f"{label} cannot be negative."))
        return None
    try:
        return to_minor(Decimal(text))
    except (ValueError, InvalidOperation):
        issues.append(POIssue(where, "error", "too_many_decimals", f"{label} '{raw}' has more than 2 decimal places."))
        return None


def _number(raw: str | None, where: str, issues: list[POIssue], label: str) -> Decimal | None:
    if raw is None or raw == "":
        return None
    text = normalize_amount_string(raw)
    try:
        value = Decimal(text) if text is not None else None
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite():
        issues.append(POIssue(where, "error", "not_a_number", f"{label} '{raw}' is not a number."))
        return None
    return value


def validate_po(po: POCreate, conn: sqlite3.Connection, settings: Settings, new_vendor: NewVendorIn | None = None
                ) -> tuple[list[POIssue], ParsedPO | None]:
    """(issues, parsed). `parsed` is None when any issue is an error."""
    issues: list[POIssue] = []

    # --- PO number
    number = (po.po_number or "").strip()
    if not number:
        issues.append(POIssue("po_number", "error", "required", "PO number is required."))
    else:
        if conn.execute("SELECT 1 FROM purchase_orders WHERE po_number = ?", (number,)).fetchone():
            issues.append(POIssue("po_number", "error", "duplicate", f"PO number {number} already exists."))
        else:
            norm = normalize_identifier(number)
            similar = [r["po_number"] for r in conn.execute("SELECT po_number FROM purchase_orders")
                       if normalize_identifier(r["po_number"]) == norm]
            if similar:
                issues.append(POIssue("po_number", "warning", "similar", f"PO number {number} looks like existing {similar[0]}."))

    # --- vendor: exactly one of an existing vendor or a new one
    vendors = load_vendor_facts(conn)
    if po.vendor_id is not None and new_vendor is not None:
        issues.append(POIssue("vendor", "error", "ambiguous", "Choose an existing vendor or create a new one, not both."))
    elif po.vendor_id is not None:
        vendor = next((v for v in vendors if v.id == po.vendor_id), None)
        if vendor is None:
            issues.append(POIssue("vendor", "error", "unknown", "The chosen vendor does not exist."))
        elif vendor.status is VendorStatus.BLOCKED:
            issues.append(POIssue("vendor", "warning", "blocked", f"{vendor.name} is blocked: its invoices will be rejected."))
        elif vendor.status is VendorStatus.NEW:
            issues.append(POIssue("vendor", "warning", "new_vendor", f"{vendor.name} is a new vendor: its invoices go to review."))
    elif new_vendor is not None:
        issues.append(POIssue("vendor", "warning", "new_vendor",
                              f"A new vendor '{new_vendor.name}' will be created with status new: its invoices go to review "
                              "until someone approves the vendor."))
        if new_vendor.tax_id:
            same = [v for v in vendors if tax_ids_match(new_vendor.tax_id, v.tax_id)]
            if same:
                issues.append(POIssue("vendor", "warning", "same_tax_id", f"Vendor {same[0].name} already has tax ID {new_vendor.tax_id}."))
        near = resolve_vendor(new_vendor.name, vendors, settings.match)
        if near.vendor_id is not None:
            name = next(v.name for v in vendors if v.id == near.vendor_id)
            issues.append(POIssue("vendor", "warning", "similar_vendor", f"'{new_vendor.name}' looks like existing vendor {name}."))
    else:
        issues.append(POIssue("vendor", "error", "required", "Vendor is required."))

    # --- currency
    currency = (po.currency or "").strip().upper()
    if not currency:
        issues.append(POIssue("currency", "error", "required", "Currency is required (it is never guessed)."))
    elif not _CURRENCY.match(currency):
        issues.append(POIssue("currency", "error", "invalid", f"Currency '{po.currency}' is not a 3-letter code such as USD."))
    elif currency in settings.unsupported_currencies:
        issues.append(POIssue("currency", "error", "unsupported", f"{currency} does not use 2 decimal places, which is not supported."))

    # --- total
    total = _money(po.total, "total", issues, required=True, label="Total")
    if total is not None:
        if total == 0:
            issues.append(POIssue("total", "warning", "zero", "The total is 0.00."))
        if from_minor(total) > settings.po_total_warning_above:
            issues.append(POIssue("total", "warning", "large", f"The total is above {settings.po_total_warning_above:,.2f}; please check it."))

    # --- issued date (optional)
    issued = (po.issued_date or "").strip() or None
    if issued is not None:
        try:
            if not _ISO_DATE.match(issued):
                raise ValueError
            d = date.fromisoformat(issued)
            if d > date.today():
                issues.append(POIssue("issued_date", "warning", "future", f"The issue date {issued} is in the future."))
        except ValueError:
            issues.append(POIssue("issued_date", "error", "invalid", f"Issue date '{issued}' is not a date (YYYY-MM-DD)."))

    # --- lines (optional)
    lines: list[ParsedLine] = []
    if len(po.lines) > settings.po_max_lines:
        issues.append(POIssue("lines", "error", "too_many", f"At most {settings.po_max_lines} lines."))
    for i, line in enumerate(po.lines):
        where = f"lines[{i}]"
        if not any([line.description, line.quantity, line.unit_price, line.amount]):
            issues.append(POIssue(where, "error", "empty", f"Line {i + 1} is empty; fill it in or remove it."))
            continue
        qty = _number(line.quantity, f"{where}.quantity", issues, "Quantity")
        price = _number(line.unit_price, f"{where}.unit_price", issues, "Unit price")
        amount = _money(line.amount, f"{where}.amount", issues, required=False, label="Amount")
        if qty is not None and price is not None and amount is not None:
            allowance = Decimal(str(settings.arithmetic_rounding_per_term)) + abs(qty) * Decimal(str(settings.arithmetic_unit_price_rounding))
            if abs(qty * price - from_minor(amount)) > allowance:
                issues.append(POIssue(f"{where}.amount", "warning", "line_math",
                                      f"Line {i + 1}: {qty} x {price} = {qty * price:.2f}, not {from_minor(amount):.2f}."))
        lines.append(ParsedLine(line_no=len(lines) + 1, description=line.description or None, quantity=qty, unit_price=price,
                                amount_minor=amount))
    if total is not None and lines and all(ln.amount_minor is not None for ln in lines):
        s = sum(ln.amount_minor for ln in lines)
        allowance = to_minor(Decimal(str(settings.arithmetic_rounding_per_term))) * len(lines)
        if abs(s - total) > allowance:
            issues.append(POIssue("total", "warning", "lines_sum",
                                  f"The lines add up to {from_minor(s):,.2f}, not the total {from_minor(total):,.2f}."))

    if any(i.level == "error" for i in issues):
        return issues, None
    return issues, ParsedPO(po_number=number, vendor_id=po.vendor_id, currency=currency, total_minor=total, issued_date=issued,
                            lines=lines)


def lines_sum(po: POCreate) -> str | None:
    """The sum of the line amounts, for the form's "use the sum of the lines" button (never applied automatically)."""
    total = Decimal(0)
    for line in po.lines:
        text = normalize_amount_string(line.amount) if line.amount else None
        if text is None:
            return None
        total += Decimal(text)
    return f"{total:.2f}" if po.lines else None
