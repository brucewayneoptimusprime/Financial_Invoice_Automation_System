"""The ONLY writer of purchase_orders / po_lines (and of vendors created while entering a PO). Called only by the Save endpoint.

One transaction: a new vendor (always status `new`), the PO (always status `open`; afterwards derived from the ledger, `closed`
only by a person) and its lines. Provenance goes in `purchase_orders.meta` (owner decision 1: no schema change).
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.pipeline.persist import transaction
from app.po.models import NewVendorIn, ParsedPO


class DuplicatePONumber(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_po(conn: sqlite3.Connection, parsed: ParsedPO, *, provenance: dict[str, Any], new_vendor: NewVendorIn | None = None
            ) -> tuple[int, int]:
    """Returns (po_id, vendor_id). Raises DuplicatePONumber if the number was taken meanwhile (nothing is written)."""
    meta = {"entered_at": _now(), **provenance}
    try:
        with transaction(conn, immediate=True):
            vendor_id = parsed.vendor_id
            if new_vendor is not None:
                cur = conn.execute("INSERT INTO vendors (name, aliases, tax_id, country, status) VALUES (?, '[]', ?, ?, 'new')",
                                   (new_vendor.name, new_vendor.tax_id or None, new_vendor.country or None))
                vendor_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO purchase_orders (po_number, vendor_id, currency, total_amount, issued_date, status, meta) "
                "VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (parsed.po_number, vendor_id, parsed.currency, parsed.total_minor, parsed.issued_date,
                 json.dumps(meta, ensure_ascii=False, sort_keys=True, default=str)))
            po_id = cur.lastrowid
            for line in parsed.lines:
                conn.execute("INSERT INTO po_lines (po_id, line_no, description, quantity, unit_price, amount) VALUES (?,?,?,?,?,?)",
                             (po_id, line.line_no, line.description,
                              None if line.quantity is None else str(line.quantity),
                              None if line.unit_price is None else str(line.unit_price), line.amount_minor))
    except sqlite3.IntegrityError as exc:
        if "purchase_orders.po_number" in str(exc):
            raise DuplicatePONumber(parsed.po_number) from exc
        raise
    return po_id, vendor_id
