"""Classify each PO of the feed as new / exists / problem. READ-ONLY: nothing here writes (tested by comparing the database file).

The same `classify` serves the preview and, PO by PO against the then-current database, the import. Rules (owner-approved ERP_PLAN):
- exists: the exact PO number is already stored. Skipped and never compared or updated.
- problem: a number repeated in the feed (every copy); a look-alike of an existing PO number (decision 6, with a link); an ambiguous
  vendor (decision 3); an adapter problem (a negative quantity, a PO not released in the ERP, ...); any error of the PO form's own
  `validate_po`; and, for feed POs, its line-math and lines-vs-total warnings (decision 2).
- new: everything else. The form's other warnings (new / blocked / similar vendor, a future date, ...) are shown and the PO can be
  ticked, as on the form.
"""
import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.engine.facts import VendorFact
from app.engine.normalize import normalize_identifier
from app.engine.vendor_match import resolve_vendor
from app.erp import LABEL
from app.erp.adapters import FeedPO, adapter_for
from app.erp.source import FeedFile
from app.po.models import NewVendorIn, ParsedPO, POIssue
from app.po.validate import load_vendor_facts, validate_po

BLOCKING_WARNINGS = {("line_math", None), ("lines_sum", "total"), ("similar", "po_number")}   # (code, field or None = any field)
METHOD = {"tax_id": "tax ID", "exact_name": "name", "alias": "alias", "fuzzy": "similar name"}


def _blocks(issue: POIssue) -> bool:
    return issue.level == "error" or (issue.code, None) in BLOCKING_WARNINGS or (issue.code, issue.field) in BLOCKING_WARNINGS


@dataclass
class Classified:
    row: dict[str, Any]
    parsed: ParsedPO | None            # set only for class "new"
    new_vendor: NewVendorIn | None


def duplicate_keys(pos: list[FeedPO]) -> set[str]:
    counts = Counter(normalize_identifier(p.po_number) for p in pos if p.po_number)
    return {k for k, n in counts.items() if n > 1}


def _existing_by_norm(conn: sqlite3.Connection, number: str) -> dict | None:
    norm = normalize_identifier(number)
    for r in conn.execute("SELECT id, po_number FROM purchase_orders ORDER BY id"):
        if normalize_identifier(r["po_number"]) == norm:
            return {"id": r["id"], "po_number": r["po_number"]}
    return None


def _lines(fp: FeedPO) -> list[dict]:
    return [{"line_number": fp.erp_line_numbers[i] if i < len(fp.erp_line_numbers) else None, "description": ln.description,
             "quantity": ln.quantity, "unit_price": ln.unit_price, "amount": ln.amount,
             "unit_of_measure": fp.line_uom[i] if i < len(fp.line_uom) else None} for i, ln in enumerate(fp.create.lines)]


def classify(conn: sqlite3.Connection, settings: Settings, fp: FeedPO, dup_keys: set[str],
             vendors: list[VendorFact] | None = None) -> Classified:
    vendors = load_vendor_facts(conn) if vendors is None else vendors
    number = fp.po_number
    row: dict[str, Any] = {"index": fp.index, "po_number": number, "class": "new", "vendor": None, "currency": fp.create.currency,
                           "total": fp.create.total, "issued_date": fp.create.issued_date, "buyer_reference": fp.buyer_reference,
                           "erp_status": fp.erp_status, "lines": _lines(fp), "issues": [], "existing_po": None}

    exact = conn.execute("SELECT id, po_number FROM purchase_orders WHERE po_number = ?", (number,)).fetchone() if number else None
    if exact is not None:                                       # never compared, never updated: skipped
        row.update({"class": "exists", "existing_po": {"id": exact["id"], "po_number": exact["po_number"]},
                    "issues": [POIssue("po_number", "warning", "exists",
                                       f"PO {number} already exists; it is skipped and never changed.").as_dict() | {"blocks": True}]})
        return Classified(row, None, None)

    issues: list[POIssue] = list(fp.problems)
    if any(i.field == "po" for i in issues):                    # not a PO at all: the form's checks would only repeat it
        row.update({"class": "problem", "issues": [i.as_dict() | {"blocks": True} for i in issues]})
        return Classified(row, None, None)
    if number and normalize_identifier(number) in dup_keys:
        issues.append(POIssue("po_number", "error", "duplicate_in_feed",
                              f"PO number {number} appears more than once in the feed; none of its copies is imported."))
    if number:
        look = _existing_by_norm(conn, number)
        if look is not None:
            row["existing_po"] = look

    vendor_id, new_vendor, ambiguous = None, None, False
    v = fp.vendor
    if v.name or v.tax_id:
        vm = resolve_vendor(v.name, vendors, settings.match, tax_id=v.tax_id)
        if vm.vendor_id is not None and not vm.ambiguous:
            vendor_id = vm.vendor_id
            rec = next(x for x in vendors if x.id == vendor_id)
            row["vendor"] = {"kind": "existing", "id": rec.id, "name": rec.name, "status": rec.status.value,
                             "matched_by": METHOD.get(vm.method, vm.method)}
        elif vm.ambiguous:
            ambiguous = True
            names = [x.name for x in vendors if x.id in (vm.candidate_vendor_ids or [vm.vendor_id])]
            issues.append(POIssue("vendor", "error", "ambiguous",
                                  f"The vendor matches more than one existing vendor ({', '.join(names)}); enter this PO with the form "
                                  "and choose the vendor."))
            row["vendor"] = {"kind": "ambiguous", "name": v.name, "candidates": names}
        elif v.name:
            new_vendor = NewVendorIn(name=v.name, tax_id=v.tax_id, country=v.country)
            row["vendor"] = {"kind": "new", "name": v.name, "tax_id": v.tax_id, "country": v.country, "status": "new"}

    create = fp.create.model_copy(update={"vendor_id": vendor_id})
    found, parsed = validate_po(create, conn, settings, new_vendor)
    if ambiguous:                                               # the ambiguity is the reason; "Vendor is required" would repeat it
        found = [i for i in found if not (i.field == "vendor" and i.code == "required")]
    issues += found
    row["issues"] = [i.as_dict() | {"blocks": _blocks(i)} for i in issues]
    if any(_blocks(i) for i in issues):
        row["class"] = "problem"
        return Classified(row, None, None)
    return Classified(row, parsed, new_vendor)


def parse(feed: FeedFile, settings: Settings) -> tuple[str, list[FeedPO]]:
    adapter = adapter_for(feed.doc)
    return adapter.key, adapter.parse(feed.doc, max_lines=settings.po_max_lines)


def build_preview(conn: sqlite3.Connection, settings: Settings, feed: FeedFile) -> dict[str, Any]:
    adapter_key, pos = parse(feed, settings)
    dups = duplicate_keys(pos)
    shown = pos[: settings.erp_max_pos_per_sync]
    vendors = load_vendor_facts(conn)
    rows = [classify(conn, settings, fp, dups, vendors).row for fp in shown]
    counts = Counter(r["class"] for r in rows)
    return {"label": LABEL,
            "feed": {"name": feed.name, "sha256": feed.sha256, "format": feed.doc.get("format"), "adapter": adapter_key,
                     "system": feed.doc.get("system") if isinstance(feed.doc.get("system"), str) else None,
                     "exported_at": feed.doc.get("exported_at") if isinstance(feed.doc.get("exported_at"), str) else None,
                     "count": len(pos), "shown": len(shown), "truncated": len(pos) - len(shown)},
            "counts": {"new": counts["new"], "exists": counts["exists"], "problem": counts["problem"]},
            "caps": {"max_pos_per_sync": settings.erp_max_pos_per_sync, "max_lines_per_po": settings.po_max_lines,
                     "max_import_per_action": settings.erp_max_import_per_action},
            "pos": rows}
