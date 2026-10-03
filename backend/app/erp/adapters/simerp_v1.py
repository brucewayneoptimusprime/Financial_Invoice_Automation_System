"""Adapter for the simulated ERP format `simerp.po-feed/v1` (the bundled demo feed; SAP/Coupa/Oracle-style header + lines)."""
from decimal import Decimal
from typing import Any

from app.erp.adapters.base import FeedPO, FeedVendor
from app.erp.source import FeedError
from app.extraction.parsing import normalize_amount_string
from app.po.models import POCreate, POIssue, POLineIn

RELEASED = ("RELEASED", "APPROVED")          # importable ERP statuses; anything else is "not released in the ERP"

# The PO form's own length limits (models.POCreate / POLineIn / NewVendorIn), so a long ERP value is a named problem, not a crash.
_LIMITS = {"po_number": 64, "currency": 10, "total": 40, "issued_date": 20, "description": 500, "quantity": 40, "unit_price": 40,
           "amount": 40, "vendor_name": 200, "vendor_tax_id": 60, "country": 60, "buyer_reference": 200, "unit_of_measure": 20}


class SimErpV1:
    key = "simerp-v1"
    formats = ("simerp.po-feed/v1",)

    def parse(self, doc: dict[str, Any], *, max_lines: int) -> list[FeedPO]:
        items = doc.get("purchase_orders")
        if not isinstance(items, list):
            raise FeedError("no_purchase_orders", "The feed has no \"purchase_orders\" list.")
        return [self._po(i, item, max_lines) for i, item in enumerate(items)]

    def _po(self, index: int, item: Any, max_lines: int) -> FeedPO:
        problems: list[POIssue] = []
        if not isinstance(item, dict):
            problems.append(POIssue("po", "error", "not_an_object", f"Entry {index + 1} in the feed is not a purchase order object."))
            return FeedPO(index=index, create=POCreate(), vendor=FeedVendor(None, None, None), problems=problems)

        def text(raw: Any, where: str, limit_key: str, label: str) -> str | None:
            if raw is None:
                return None
            if isinstance(raw, bool) or not isinstance(raw, (str, int, Decimal)):
                problems.append(POIssue(where, "error", "not_a_value", f"{label} is not a plain value in the feed."))
                return None
            value = str(raw).strip()
            limit = _LIMITS[limit_key]
            if len(value) > limit:
                problems.append(POIssue(where, "error", "too_long", f"{label} is longer than {limit} characters."))
                return value[:limit]
            return value or None

        vendor_raw = item.get("vendor")
        if vendor_raw is not None and not isinstance(vendor_raw, dict):
            problems.append(POIssue("vendor", "error", "not_an_object", "The vendor block is not an object."))
            vendor_raw = None
        vendor_raw = vendor_raw or {}
        address = vendor_raw.get("address") if isinstance(vendor_raw.get("address"), dict) else None
        vendor = FeedVendor(name=text(vendor_raw.get("name"), "vendor", "vendor_name", "Vendor name"),
                            tax_id=text(vendor_raw.get("tax_id"), "vendor", "vendor_tax_id", "Vendor tax ID"),
                            country=text((address or {}).get("country"), "vendor", "country", "Vendor country"),
                            address={k: v for k, v in (address or {}).items() if isinstance(v, str)} or None)

        lines_raw = item.get("lines")
        if lines_raw is None:
            lines_raw = []
        if not isinstance(lines_raw, list):
            problems.append(POIssue("lines", "error", "not_a_list", "The lines are not a list."))
            lines_raw = []
        if len(lines_raw) > max_lines:
            problems.append(POIssue("lines", "error", "too_many", f"At most {max_lines} lines (the feed has {len(lines_raw)})."))
            lines_raw = lines_raw[:max_lines]
        lines, numbers, uoms = [], [], []
        for i, ln in enumerate(lines_raw):
            where = f"lines[{i}]"
            if not isinstance(ln, dict):
                problems.append(POIssue(where, "error", "not_an_object", f"Line {i + 1} is not an object."))
                continue
            qty = text(ln.get("quantity"), f"{where}.quantity", "quantity", f"Line {i + 1} quantity")
            if qty is not None:
                norm = normalize_amount_string(qty)            # the form's own number reading ("1,000", "-2")
                try:
                    q = Decimal(norm) if norm is not None else None
                except ArithmeticError:
                    q = None                                   # not a number: validate_po names it
                if q is not None and q.is_finite() and q <= 0:  # the ERP contract: quantity > 0 (owner decision 1)
                    problems.append(POIssue(f"{where}.quantity", "error", "negative" if q < 0 else "not_positive",
                                            f"Line {i + 1}: quantity cannot be {'negative' if q < 0 else 'zero'} ({qty})."))
            lines.append(POLineIn(description=text(ln.get("description"), f"{where}.description", "description", f"Line {i + 1} description"),
                                  quantity=qty,
                                  unit_price=text(ln.get("unit_price"), f"{where}.unit_price", "unit_price", f"Line {i + 1} unit price"),
                                  amount=text(ln.get("line_amount"), f"{where}.amount", "amount", f"Line {i + 1} amount")))
            numbers.append(ln.get("line_number") if isinstance(ln.get("line_number"), (int, str)) else None)
            uoms.append(text(ln.get("unit_of_measure"), f"{where}.unit_of_measure", "unit_of_measure", f"Line {i + 1} unit of measure"))

        status = text(item.get("erp_status"), "erp_status", "buyer_reference", "ERP status")
        if status is None:
            problems.append(POIssue("erp_status", "error", "required", "The ERP status is missing."))
        elif status.upper() not in RELEASED:
            problems.append(POIssue("erp_status", "error", "not_released",
                                    f"The PO is {status} in the ERP, not released: only RELEASED or APPROVED orders are imported."))

        create = POCreate(po_number=text(item.get("po_number"), "po_number", "po_number", "PO number"),
                          currency=text(item.get("currency"), "currency", "currency", "Currency"),
                          total=text(item.get("total_amount"), "total", "total", "Total"),
                          issued_date=text(item.get("issued_date"), "issued_date", "issued_date", "Issue date"),
                          lines=lines)
        return FeedPO(index=index, create=create, vendor=vendor,
                      buyer_reference=text(item.get("buyer_reference"), "buyer_reference", "buyer_reference", "Buyer reference"),
                      erp_status=status, erp_line_numbers=numbers, line_uom=uoms, problems=problems)
