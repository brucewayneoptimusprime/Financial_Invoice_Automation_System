"""PO export, stage E1: the document model, CSV and Excel, the two routes. Every exported value is compared with the screens' own JSON
(GET /api/pos, GET /api/pos/{id}). Offline; no model; no database write."""
import time
from datetime import datetime, timezone

import pytest

from app.po.export import model as model_mod
from app.po.export.csv_render import render as render_csv
from app.po.export.model import ExportError, detail_doc, summary_doc
from app.po.export.safety import safe_cell, safe_filename
from tests.api.helpers import api, api_settings
from tests.gmail.helpers import table_counts
from tests.po.export_helpers import (PO_SS_001, approved_api, csv_rows, csv_sections, db, insert_po, money, xlsx_book)

T = datetime(2026, 10, 2, 14, 15, tzinfo=timezone.utc)
SECTIONS_FIN = ["PO", "Totals", "Invoices matched to this PO", "Lines", "Ledger"]
SECTIONS_FULL = SECTIONS_FIN + ["How the commits are allocated", "Where this PO came from"]


def humanize(s):
    return s.replace("_", " ")[:1].upper() + s.replace("_", " ")[1:] if s else ""


# ------------------------------------------------------------------------------------------ the summary: CSV

def test_the_summary_csv_equals_the_list_screen(tmp_path):
    with approved_api(tmp_path) as c:
        shown = c.get("/api/pos").json()["pos"]
        r = c.get("/api/pos/export", params={"format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"] == "text/csv; charset=utf-8" and r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"].startswith('attachment; filename="purchase-orders-') and "filename*=UTF-8''" in r.headers["content-disposition"]
    assert b"\r\n" in r.content
    rows = csv_rows(r.content)
    meta = {row[0]: row[1] for row in rows[:6]}
    assert meta["Scope"] == "All purchase orders" and meta["Purchase orders"] == str(len(shown)) and "Nothing was sent" in meta["Note"]
    header_at = rows.index(["PO number", "Vendor", "Currency", "Total", "Balance", "Status", "Invoices", "Entered"])
    table = rows[header_at + 1:]
    assert len(table) == len(shown)
    for row, p in zip(table, shown):
        assert row[:3] == [p["po_number"], p["vendor"], p["currency"]]
        assert money(row[3]) == money(p["total"]) and money(row[4]) == money(p["balance"])
        assert row[5] == humanize(p["status"]) and int(row[6]) == p["invoice_count"] and row[7] == (humanize(p["source"]) if p["source"] else "—")


@pytest.mark.parametrize("params, scope", [
    ({"status": "partially_billed"}, "Filter: status partially billed"),
    ({"currency": "inr"}, "Filter: currency INR"),
    ({"q": "SS-00"}, 'Filter: search "SS-00"'),
    ({"q": "SS-00", "status": "open", "currency": "USD"}, 'Filter: search "SS-00"; status open; currency USD'),
])
def test_the_summary_respects_the_list_filter_and_says_so(tmp_path, params, scope):
    with approved_api(tmp_path) as c:
        shown = c.get("/api/pos", params=params).json()["pos"]
        rows = csv_rows(c.get("/api/pos/export", params={"format": "csv", **params}).content)
    meta = {row[0]: row[1] for row in rows[:6]}
    assert meta["Scope"] == scope
    header_at = [r[0] if r else "" for r in rows].index("PO number")
    assert [r[0] for r in rows[header_at + 1:]] == [p["po_number"] for p in shown] or (not shown and rows[header_at + 1] == ["None"])


def test_ticked_ids_export_only_those_in_the_list_order_and_the_scope_says_so(tmp_path):
    with approved_api(tmp_path) as c:
        everything = c.get("/api/pos").json()["pos"]
        pick = [everything[3]["id"], everything[0]["id"]]
        rows = csv_rows(c.get("/api/pos/export", params={"format": "csv", "ids": f"{pick[0]},{pick[1]}", "status": ""}).content)
        filtered = csv_rows(c.get("/api/pos/export", params={"format": "csv", "ids": str(pick[0]), "currency": "USD"}).content)
    meta = {row[0]: row[1] for row in rows[:6]}
    assert meta["Scope"] == f"Ticked: 2 of {len(everything)} shown"
    header_at = [r[0] if r else "" for r in rows].index("PO number")
    assert [r[0] for r in rows[header_at + 1:]] == [everything[0]["po_number"], everything[3]["po_number"]]
    assert {row[0]: row[1] for row in filtered[:6]}["Scope"].endswith("(filter: currency USD)")


@pytest.mark.parametrize("params, status, code", [
    ({"format": "csv", "ids": "1,999"}, 404, "not_found"),
    ({"format": "csv", "ids": "1,x"}, 422, "bad_ids"),
    ({"format": "zip"}, 422, "bad_format"),
])
def test_bad_summary_requests(tmp_path, params, status, code):
    with api(tmp_path) as c:
        r = c.get("/api/pos/export", params=params)
    assert r.status_code == status and r.json()["error"] == code


def test_the_summary_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(model_mod, "MAX_SUMMARY_POS", 2)
    with api(tmp_path) as c:
        r = c.get("/api/pos/export", params={"format": "csv"})
        r_ids = c.get("/api/pos/export", params={"format": "csv", "ids": "1,2,3"})
    assert r.status_code == 422 and "narrow the search or tick at most 2" in r.json()["message"]
    assert r_ids.status_code == 422 and r_ids.json()["error"] == "too_many"


# ------------------------------------------------------------------------------------------ one PO: CSV

def test_the_financial_csv_equals_the_po_page(tmp_path):
    with approved_api(tmp_path) as c:
        d = c.get(f"/api/pos/{PO_SS_001}").json()
        r = c.get(f"/api/pos/{PO_SS_001}/export", params={"format": "csv", "level": "financial"})
    assert r.headers["content-disposition"].startswith('attachment; filename="PO-SS-001-financial-')
    meta, sections = csv_sections(r.content)
    assert list(sections) == SECTIONS_FIN and meta["What"] == "Purchase order PO-SS-001: Financial"
    po = dict(sections["PO"][1:])
    assert po == {"PO number": "PO-SS-001", "Vendor": f"{d['po']['vendor']} ({d['po']['vendor_status']})", "Currency": d["po"]["currency"],
                  "Issued": d["po"]["issued_date"] or "—", "Status": humanize(d["po"]["status"])}
    totals = {k: money(v) for k, v in sections["Totals"][1:]}
    a = d["amounts"]
    assert totals == {"Total": money(a["total"]), "Committed (approved invoices)": money(a["committed"]), "Balance": money(a["balance"]),
                      "Awaiting review": money(a["awaiting_review"]), "Consumed, not assigned to a line": money(a["consumed_without_line"])}
    assert totals["Balance"] == money("661.92")                                   # the approval's effect, as on the page
    lines = sections["Lines"][1:]
    assert len(lines) == len(d["lines"])
    for row, ln in zip(lines, d["lines"]):
        assert int(row[0]) == ln["line_no"] and row[1] == ln["description"]
        assert [money(x) for x in row[2:]] == [money(ln[k]) for k in ("quantity", "unit_price", "amount", "consumed_quantity",
                                                                     "consumed_amount", "remaining_quantity", "remaining_amount")]
    assert [(r[0], money(r[1])) for r in sections["Ledger"][1:]] == [(humanize(e["type"]), money(e["amount"])) for e in d["ledger"]]
    inv = sections["Invoices matched to this PO"][1:]
    assert [(r[0], money(r[2]), r[4]) for r in inv] == [(i["invoice_number"], money(i["total"]), humanize(i["status"])) for i in d["invoices"]]


def test_the_full_csv_adds_allocations_and_provenance(tmp_path):
    with approved_api(tmp_path) as c:
        d = c.get(f"/api/pos/{PO_SS_001}").json()
        meta, sections = csv_sections(c.get(f"/api/pos/{PO_SS_001}/export", params={"format": "csv", "level": "full"}).content)
    assert list(sections) == SECTIONS_FULL and meta["What"] == "Purchase order PO-SS-001: Full (with metadata)"
    alloc = sections["How the commits are allocated"][1:]
    assert [(money(r[2]), r[1]) for r in alloc] == [(money(x["amount"]), f"Line {x['po_line_no']}" if x["po_line_no"]
                                                     else "PO total (no specific line)") for x in d["allocations"]]
    assert dict(sections["Where this PO came from"][1:])["Entered by"] == "Demo dataset"


# ------------------------------------------------------------------------------------------ Excel

def test_the_summary_xlsx_is_one_numeric_table_with_the_scope(tmp_path):
    with approved_api(tmp_path) as c:
        shown = c.get("/api/pos").json()["pos"]
        r = c.get("/api/pos/export", params={"format": "xlsx", "status": "open"})
        shown_open = c.get("/api/pos", params={"status": "open"}).json()["pos"]
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
    wb = xlsx_book(r.content)
    assert wb.sheetnames == ["Purchase orders"]
    ws = wb["Purchase orders"]
    assert ws["A4"].value == "Scope" and ws["B4"].value == "Filter: status open"
    header_row = next(i for i in range(1, 20) if ws.cell(i, 1).value == "PO number")
    assert ws.freeze_panes == f"A{header_row + 1}" and ws.auto_filter.ref.startswith(f"A{header_row}:")
    for i, p in enumerate(shown_open, start=header_row + 1):
        assert ws.cell(i, 1).value == p["po_number"]
        assert ws.cell(i, 4).data_type == "n" and money(ws.cell(i, 4).value) == money(p["total"])
        assert ws.cell(i, 4).number_format == "#,##0.00" and money(ws.cell(i, 5).value) == money(p["balance"])
    assert ws.cell(header_row + len(shown_open) + 1, 1).value is None and len(shown) > len(shown_open)


def test_the_detail_xlsx_has_one_sheet_per_section_with_equal_values(tmp_path):
    with approved_api(tmp_path) as c:
        d = c.get(f"/api/pos/{PO_SS_001}").json()
        fin = xlsx_book(c.get(f"/api/pos/{PO_SS_001}/export", params={"format": "xlsx", "level": "financial"}).content)
        full = xlsx_book(c.get(f"/api/pos/{PO_SS_001}/export", params={"format": "xlsx", "level": "full"}).content)
    assert fin.sheetnames == ["About", "PO", "Totals", "Invoices", "Lines", "Ledger"]
    assert full.sheetnames == ["About", "PO", "Totals", "Invoices", "Lines", "Ledger", "Allocations", "Provenance"]
    totals = {fin["Totals"].cell(i, 1).value: fin["Totals"].cell(i, 2).value for i in range(2, 10) if fin["Totals"].cell(i, 1).value}
    assert money(totals["Balance"]) == money(d["amounts"]["balance"]) and money(totals["Total"]) == money(d["amounts"]["total"])
    lines = fin["Lines"]
    assert [lines.cell(1, j).value for j in range(1, 10)][:5] == ["Line", "Description", "Quantity", "Unit price", "Amount"]
    assert money(lines.cell(2, 5).value) == money(d["lines"][0]["amount"]) and money(lines.cell(2, 7).value) == money(d["lines"][0]["consumed_amount"])
    alloc = full["Allocations"]
    assert [money(alloc.cell(i, 3).value) for i in range(2, 2 + len(d["allocations"]))] == [money(x["amount"]) for x in d["allocations"]]


# ------------------------------------------------------------------------------------------ formula injection, file names

HOSTILE = {"po_number": '=HYPERLINK("http://evil","x")', "vendor": "+cmd|' /C calc'!A0",
           "lines": [("@SUM(A1:A9)", "1", "1.00", 100), ("-2+3", "1", "0.50", 50), ("\tTabbed", "1", "1", 100)]}


def test_csv_and_xlsx_escape_formula_starts_but_keep_negative_amounts_numeric(tmp_path):
    with api(tmp_path) as c:
        pid = insert_po(c, **HOSTILE, ledger=[("commit", 300), ("reversal", -300)])
        csv_body = c.get(f"/api/pos/{pid}/export", params={"format": "csv", "level": "full"}).content
        summary_csv = c.get("/api/pos/export", params={"format": "csv", "ids": str(pid)}).content
        book = xlsx_book(c.get(f"/api/pos/{pid}/export", params={"format": "xlsx", "level": "full"}).content)
        summary_book = xlsx_book(c.get("/api/pos/export", params={"format": "xlsx", "ids": str(pid)}).content)
    meta, sections = csv_sections(csv_body)
    assert dict(sections["PO"][1:])["PO number"] == "'" + HOSTILE["po_number"]
    assert dict(sections["PO"][1:])["Vendor"].startswith("'+cmd")
    assert [r[1] for r in sections["Lines"][1:]] == ["'@SUM(A1:A9)", "'-2+3", "'\tTabbed"]
    assert [r[1] for r in sections["Ledger"][1:]] == ["3.00", "-3.00"]                       # money is a number, never escaped
    assert any(row[:2] == ["'" + HOSTILE["po_number"], "'" + HOSTILE["vendor"]] for row in csv_rows(summary_csv))
    for wb in (book, summary_book):
        cells = [cell for ws in wb.worksheets for row in ws.iter_rows() for cell in row if cell.value is not None]
        assert not any(cell.data_type == "f" for cell in cells)                                    # no formula anywhere
        texts = [cell.value for cell in cells if cell.data_type == "s"]
        assert not any(t.startswith(("=", "+", "@", "\t")) for t in texts)
    ledger = book["Ledger"]
    assert ledger.cell(3, 2).data_type == "n" and money(ledger.cell(3, 2).value) == money("-3.00")


@pytest.mark.parametrize("text, safe", [("=1+1", "'=1+1"), ("+1", "'+1"), ("-x", "'-x"), ("@x", "'@x"), ("\tx", "'\tx"), ("\rx", "'\rx"),
                                        ("plain", "plain"), ("", ""), (None, "")])
def test_safe_cell(text, safe):
    assert safe_cell(text) == safe


def test_file_names_are_sanitised(tmp_path):
    with api(tmp_path) as c:
        pid = insert_po(c, po_number="../evil/<PO> № 7.pdf", vendor="V")
        r = c.get(f"/api/pos/{pid}/export", params={"format": "csv", "level": "financial"})
    disposition = r.headers["content-disposition"]
    filename = disposition.split('filename="')[1].split('"')[0]
    assert filename.startswith("evil-PO-7.pdf-financial-") and filename.endswith(".csv") and "/" not in filename and ".." not in filename
    assert safe_filename("////", "po-9") == "po-9" and len(safe_filename("x" * 500, "f")) <= 80


# ------------------------------------------------------------------------------------------ errors, access, no writes

def test_bad_detail_requests(tmp_path):
    with api(tmp_path) as c:
        assert c.get("/api/pos/999/export", params={"format": "csv"}).status_code == 404
        assert c.get("/api/pos/1/export", params={"format": "csv", "level": "everything"}).json()["error"] == "bad_level"
        assert c.get("/api/pos/1/export", params={"format": "exe"}).json()["error"] == "bad_format"
        assert c.get("/api/pos/1/export").status_code == 422                       # format is required


def test_exports_are_behind_the_access_token(tmp_path):
    settings = api_settings(tmp_path, access_token="tok-export")
    with api(tmp_path, settings=settings) as c:
        assert c.get("/api/pos/export", params={"format": "csv"}).status_code == 401
        assert c.get("/api/pos/1/export", params={"format": "csv"}).status_code == 401
        ok = c.get("/api/pos/1/export", params={"format": "csv"}, headers={"Authorization": "Bearer tok-export"})
        assert ok.status_code == 200 and ok.content.startswith("﻿".encode())


def test_exports_write_nothing(tmp_path):
    with approved_api(tmp_path) as c:
        before = table_counts(c.db_path)
        for fmt in ("csv", "xlsx"):
            c.get("/api/pos/export", params={"format": fmt})
            for level in ("financial", "full"):
                c.get(f"/api/pos/{PO_SS_001}/export", params={"format": fmt, "level": level})
        assert table_counts(c.db_path) == before


# ------------------------------------------------------------------------------------------ empty and very long

def test_an_empty_database_and_an_empty_po_say_none(conn):
    doc = summary_doc(conn, q=None, status=None, currency=None, ids=None, generated=T)
    rows = csv_rows(render_csv(doc))
    assert rows[-1] == ["None"] and {r[0]: r[1] for r in rows[:6]}["Purchase orders"] == "0"
    with conn:
        vid = conn.execute("INSERT INTO vendors (name, status) VALUES ('Empty Co', 'new')").lastrowid
        pid = conn.execute("INSERT INTO purchase_orders (po_number, vendor_id, currency, total_amount, status, meta) "
                           "VALUES ('PO-EMPTY', ?, 'EUR', 0, 'open', '{}')", (vid,)).lastrowid
    meta, sections = csv_sections(render_csv(detail_doc(conn, pid, level="full", generated=T)))
    for title in ("Invoices matched to this PO", "Lines", "Ledger", "How the commits are allocated"):
        assert sections[title][1:] == [["None"]]
    assert dict(sections["Where this PO came from"][1:]) == {"Entered by": "Unknown"}
    with pytest.raises(ExportError) as err:
        detail_doc(conn, 999, level="financial", generated=T)
    assert err.value.status == 404


def test_a_very_long_po_exports_quickly(tmp_path):
    long_lines = [(f"Item {n} " + "very long description " * 45, "3", "12.34", 3702) for n in range(200)]
    with api(tmp_path) as c:
        pid = insert_po(c, po_number="PO-LONG", vendor="Long Supplies", lines=long_lines, total=200 * 3702)
        started = time.perf_counter()
        csv_body = c.get(f"/api/pos/{pid}/export", params={"format": "csv", "level": "full"}).content
        book = xlsx_book(c.get(f"/api/pos/{pid}/export", params={"format": "xlsx", "level": "full"}).content)
        elapsed = time.perf_counter() - started
    _, sections = csv_sections(csv_body)
    assert len(sections["Lines"]) == 201 and len(sections["Lines"][1][1]) > 900
    assert book["Lines"].max_row == 201 and elapsed < 5
