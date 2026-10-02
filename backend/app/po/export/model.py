"""ONE neutral document model for every export format, built from the screens' read models.

`summary_doc` reads `po_list` (with the list screen's filters, or the ticked ids); `detail_doc` reads `po_detail`. Values keep their
types: money and quantities are exact `Decimal` (from the views' decimal strings, which come from integer cents via
`money.from_minor`), counts are `int`, text is `str`. Renderers only format; they never compute a number.
"""
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.enums import POStatus
from app.po.export.safety import MAX_SECTION_ROWS, MAX_SUMMARY_POS, TYPED_TEXT_CAP, safe_filename
from app.po.views import po_detail, po_list

LEVELS = ("financial", "full")
NOTE = "A read-only snapshot. Nothing was sent anywhere."
_ENTERED_BY = {"manual": "Form", "text": "Typed text, drafted by the model, confirmed by a person",
               "document": "Uploaded document, drafted by the model, confirmed by a person", "seed": "Demo dataset"}
_MATCHED_BY = {"auto": "automatic", "manual_reviewer": "reviewer", "legacy": "before line tracking"}


class ExportError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Column:
    label: str
    kind: str = "text"              # text | money | qty | price (a unit price: a quantity-like decimal in the currency) | int


@dataclass
class Table:
    title: str
    columns: list[Column]
    rows: list[list[Any]]
    note: str | None = None


@dataclass
class KeyValues:
    title: str
    items: list[tuple[str, Any, str]]   # (label, value, kind)


@dataclass
class ExportDoc:
    kind: str                       # summary | detail
    title: str
    filename_stem: str
    meta: list[tuple[str, str]]
    sections: list[Table | KeyValues] = field(default_factory=list)
    currency: str | None = None     # one PO: its currency (money column headers say it); the summary mixes currencies (a column)


def humanize(key: str | None) -> str:
    """The screens' `humanize`: underscores to spaces, first letter upper case."""
    if not key:
        return ""
    s = key.replace("_", " ")
    return s[:1].upper() + s[1:]


def dec(value: str | None) -> Decimal | None:
    return None if value is None or value == "" else Decimal(str(value))


def _stamp(generated: datetime) -> tuple[str, str]:
    return generated.strftime("%Y-%m-%d %H:%M"), generated.strftime("%Y%m%d-%H%M")


# ------------------------------------------------------------------------------------------ the summary (entry point 1)

SUMMARY_COLUMNS = [Column("PO number"), Column("Vendor"), Column("Currency"), Column("Total", "money"), Column("Balance", "money"),
                   Column("Status"), Column("Invoices", "int"), Column("Entered")]


def _filter_text(q: str | None, status: str | None, currency: str | None) -> str:
    parts = []
    if q:
        parts.append(f'search "{q}"')
    if status in {s.value for s in POStatus}:
        parts.append(f"status {humanize(status).lower()}")
    if currency:
        parts.append(f"currency {currency.strip().upper()}")
    return "; ".join(parts)


def parse_ids(raw: str | None) -> list[int] | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise ExportError(422, "bad_ids", "ids must be a comma-separated list of purchase order ids.") from None
    if len(ids) > MAX_SUMMARY_POS:
        raise ExportError(422, "too_many", f"At most {MAX_SUMMARY_POS:,} purchase orders can be exported at once; tick fewer.")
    return list(dict.fromkeys(ids))


def summary_doc(conn: sqlite3.Connection, *, q: str | None, status: str | None, currency: str | None, ids: list[int] | None,
                generated: datetime) -> ExportDoc:
    shown = po_list(conn, q=q or None, status=status or None, currency=currency or None)
    filt = _filter_text(q, status, currency)
    if ids is not None:
        everything = {p["id"]: p for p in po_list(conn)}
        unknown = [i for i in ids if i not in everything]
        if unknown:
            raise ExportError(404, "not_found", f"No purchase order with id {', '.join(map(str, unknown[:10]))}.")
        wanted = set(ids)
        rows = [p for p in po_list(conn) if p["id"] in wanted]                  # the list's own order
        scope = f"Ticked: {len(rows)} of {len(shown)} shown" + (f" (filter: {filt})" if filt else "")
    else:
        rows = shown
        scope = f"Filter: {filt}" if filt else "All purchase orders"
    if len(rows) > MAX_SUMMARY_POS:
        raise ExportError(422, "too_many", f"{len(rows):,} purchase orders match; narrow the search or tick at most {MAX_SUMMARY_POS:,}.")
    human, compact = _stamp(generated)
    table = Table("Purchase orders", SUMMARY_COLUMNS, [
        [p["po_number"], p["vendor"], p["currency"], dec(p["total"]), dec(p["balance"]), humanize(p["status"]), p["invoice_count"],
         humanize(p["source"]) if p["source"] else "—"] for p in rows])
    meta = [("Exported from", "Invoice Agent"), ("Generated (UTC)", human), ("What", "Purchase orders (summary)"), ("Scope", scope),
            ("Purchase orders", str(len(rows))), ("Note", NOTE)]
    return ExportDoc("summary", "Purchase orders", f"purchase-orders-{compact}", meta, [table])


# ------------------------------------------------------------------------------------------ one PO (entry points 2 and 3)

def _capped(rows: list[list[Any]]) -> tuple[list[list[Any]], str | None]:
    if len(rows) <= MAX_SECTION_ROWS:
        return rows, None
    return rows[:MAX_SECTION_ROWS], f"(truncated: the first {MAX_SECTION_ROWS:,} of {len(rows):,} rows)"


def _changed_fields(fields: list[str]) -> str:
    if not fields:
        return "nothing (the draft was saved as proposed)"
    out = []
    for f in fields:
        if "[" in f and f.endswith("]"):
            name, _, n = f[:-1].partition("[")
            out.append(f"{humanize(name)} {int(n) + 1}" if n.isdigit() else humanize(f))
        else:
            out.append(humanize(f))
    return ", ".join(out)


def provenance_items(p: dict) -> list[tuple[str, Any, str]]:
    source = str(p.get("source") or ("seed" if p.get("demo") else "unknown"))
    items: list[tuple[str, Any, str]] = [("Entered by", _ENTERED_BY.get(source, humanize(source)), "text")]
    if isinstance(p.get("entered_at"), str):
        items.append(("Entered", p["entered_at"], "text"))
    if isinstance(p.get("file_name"), str):
        items.append(("Document", p["file_name"], "text"))
    if isinstance(p.get("model"), str):
        cost = f" (${p['cost_usd']})" if p.get("cost_usd") else ""
        items.append(("Model", f"{p['model']}{cost}", "text"))
    if "draft_id" in p:
        items.append(("Changed by the person", _changed_fields(list(p.get("edited_fields") or [])), "text"))
    if isinstance(p.get("text"), str):
        text = p["text"]
        items.append(("Typed text", text if len(text) <= TYPED_TEXT_CAP else text[:TYPED_TEXT_CAP] + " (truncated)", "text"))
    return items


def detail_doc(conn: sqlite3.Connection, po_id: int, *, level: str, generated: datetime) -> ExportDoc:
    if level not in LEVELS:
        raise ExportError(422, "bad_level", "level must be financial or full.")
    d = po_detail(conn, po_id)
    if d is None:
        raise ExportError(404, "not_found", "No such purchase order.")
    po, a = d["po"], d["amounts"]
    human, compact = _stamp(generated)
    level_name = "Full (with metadata)" if level == "full" else "Financial"
    meta = [("Exported from", "Invoice Agent"), ("Generated (UTC)", human), ("What", f"Purchase order {po['po_number']}: {level_name}"),
            ("Note", NOTE)]
    sections: list[Table | KeyValues] = [
        KeyValues("PO", [("PO number", po["po_number"], "text"), ("Vendor", f"{po['vendor']} ({po['vendor_status']})", "text"),
                         ("Currency", po["currency"], "text"), ("Issued", po["issued_date"] or "—", "text"),
                         ("Status", humanize(po["status"]), "text")]),
        KeyValues("Totals", [("Total", dec(a["total"]), "money"), ("Committed (approved invoices)", dec(a["committed"]), "money"),
                             ("Balance", dec(a["balance"]), "money"), *([("Over-billed", "yes", "text")] if a["over_billed"] else []),
                             ("Awaiting review", dec(a["awaiting_review"]), "money"),
                             ("Consumed, not assigned to a line", dec(a["consumed_without_line"]), "money")]),
        Table("Invoices matched to this PO",
              [Column("Invoice"), Column("File"), Column("Total", "money"), Column("Decision at run time"), Column("Status now"),
               Column("When"), Column("Historic")],
              [[i["invoice_number"] or "(no number)", i["source_file"] or "—", dec(i["total"]),
                "—" if i["historic"] else humanize(i["decision"]), humanize(i["status"]), i["started_at"] or "—",
                "yes" if i["historic"] else "no"] for i in d["invoices"]]),
        Table("Lines",
              [Column("Line", "int"), Column("Description"), Column("Quantity", "qty"), Column("Unit price", "price"), Column("Amount", "money"),
               Column("Consumed quantity", "qty"), Column("Consumed amount", "money"), Column("Remaining quantity", "qty"),
               Column("Remaining amount", "money")],
              [[ln["line_no"], ln["description"] or "", dec(ln["quantity"]), dec(ln["unit_price"]), dec(ln["amount"]),
                dec(ln["consumed_quantity"]), dec(ln["consumed_amount"]), dec(ln["remaining_quantity"]), dec(ln["remaining_amount"])]
               for ln in d["lines"]]),
    ]
    ledger_rows, ledger_note = _capped([[humanize(e["type"]), dec(e["amount"]), e["invoice_id"], e["created_at"]] for e in d["ledger"]])
    sections.append(Table("Ledger", [Column("Type"), Column("Amount", "money"), Column("Invoice id", "int"), Column("When")],
                          ledger_rows, ledger_note))
    if level == "full":
        alloc_rows, alloc_note = _capped([
            [x["invoice_number"] or "(no number)", f"Line {x['po_line_no']}" if x["po_line_no"] else "PO total (no specific line)",
             dec(x["amount"]), dec(x["quantity"]), _MATCHED_BY.get(x["matched_by"], x["matched_by"]), x["created_at"]]
            for x in d["allocations"]])
        sections.append(Table("How the commits are allocated",
                              [Column("Invoice"), Column("To"), Column("Amount", "money"), Column("Quantity", "qty"), Column("How"),
                               Column("When")], alloc_rows, alloc_note))
        sections.append(KeyValues("Where this PO came from", provenance_items(d["provenance"] or {})))
    stem = safe_filename(f"{po['po_number']}-{level}-{compact}", f"po-{po_id}-{level}-{compact}")
    return ExportDoc("detail", f"Purchase order {po['po_number']}", stem, meta, sections, currency=po["currency"])
