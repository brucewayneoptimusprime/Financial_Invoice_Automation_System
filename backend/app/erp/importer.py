"""Import the POs a person ticked in the preview: the ONLY place ERP data is written, and only through `save_po` (the single PO writer).

Each pick, in feed order, is classified again against the CURRENT database (so a PO created meanwhile, or a vendor created by an
earlier pick of this import, is seen) and saved only if it is still "new". Nothing is ever updated: an existing PO is skipped.
One pick failing never stops the others (owner decision 5). No model is involved.
"""
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.erp import LABEL
from app.erp.preview import build_preview, classify, duplicate_keys, parse
from app.erp.source import FeedFile
from app.po.store import DuplicatePONumber, save_po


class ImportRefused(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_pos(conn: sqlite3.Connection, settings: Settings, feed: FeedFile, feed_sha256: str, po_numbers: list[str]) -> dict[str, Any]:
    """All refusals happen before anything is written. Returns {imported, skipped, refused, results: [...]}."""
    if feed_sha256 != feed.sha256:
        raise ImportRefused(409, "feed_changed", "The ERP feed changed since the preview; check the new preview and pick again.",
                            preview=build_preview(conn, settings, feed))
    picks = [str(n).strip() for n in po_numbers]
    if not picks:
        raise ImportRefused(422, "nothing_picked", "Tick at least one new purchase order to import.")
    if len(picks) > settings.erp_max_import_per_action:
        raise ImportRefused(422, "too_many", f"At most {settings.erp_max_import_per_action} purchase orders per import.")
    if len(set(picks)) != len(picks):
        raise ImportRefused(422, "duplicate_pick", "The same purchase order was picked twice.")
    adapter_key, pos = parse(feed, settings)
    shown = pos[: settings.erp_max_pos_per_sync]
    in_feed = {fp.po_number for fp in shown if fp.po_number}
    unknown = [n for n in picks if n not in in_feed]
    if unknown:
        raise ImportRefused(422, "not_in_feed", f"Not in the feed preview: {', '.join(unknown[:10])}.", po_numbers=unknown)

    dups = duplicate_keys(pos)
    synced_at = _now()
    wanted = set(picks)
    results = []
    done: set[str] = set()
    for fp in shown:
        number = fp.po_number
        if number not in wanted or number in done:          # a repeated number is a problem anyway; handle its first copy only
            continue
        done.add(number)
        c = classify(conn, settings, fp, dups)                # against the current database, vendors re-read each time
        if c.row["class"] == "exists":
            results.append({"po_number": number, "outcome": "skipped_exists", "existing_po": c.row["existing_po"]})
            continue
        if c.row["class"] != "new" or c.parsed is None:
            results.append({"po_number": number, "outcome": "refused", "issues": c.row["issues"]})
            continue
        provenance = {"source": "erp", "erp_label": LABEL, "adapter": adapter_key, "feed_file": feed.name, "feed_sha256": feed.sha256,
                      "synced_at": synced_at, "buyer_reference": fp.buyer_reference, "erp_status": fp.erp_status,
                      "erp_vendor": {"name": fp.vendor.name, "tax_id": fp.vendor.tax_id, "address": fp.vendor.address},
                      "erp_line_numbers": fp.erp_line_numbers, "line_uom": fp.line_uom}
        try:
            po_id, vendor_id = save_po(conn, c.parsed, provenance=provenance, new_vendor=c.new_vendor)
        except DuplicatePONumber:                             # created by someone else between the check and the save
            row = conn.execute("SELECT id, po_number FROM purchase_orders WHERE po_number = ?", (number,)).fetchone()
            results.append({"po_number": number, "outcome": "skipped_exists",
                            "existing_po": None if row is None else {"id": row["id"], "po_number": row["po_number"]}})
            continue
        results.append({"po_number": number, "outcome": "imported", "po_id": po_id, "vendor_id": vendor_id,
                        "new_vendor": c.new_vendor is not None,
                        "warnings": [i for i in c.row["issues"] if i["level"] == "warning"]})
    return {"label": LABEL, "synced_at": synced_at, "feed": {"name": feed.name, "sha256": feed.sha256},
            "imported": sum(r["outcome"] == "imported" for r in results),
            "skipped": sum(r["outcome"] == "skipped_exists" for r in results),
            "refused": sum(r["outcome"] == "refused" for r in results), "results": results}
