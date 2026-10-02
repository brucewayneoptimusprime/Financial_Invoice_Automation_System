"""Shared setup for the PO export tests: a demo database with a real approval (so a PO has a ledger commit, line consumption and
allocations), parsers for the four formats, and hand-made POs for the injection / empty / long cases."""
import csv
import io
from contextlib import closing, contextmanager
from decimal import Decimal

import openpyxl

from app.api.worker import open_db
from tests.api.helpers import api, run_and_wait
from tests.review.helpers import SS_10963, item_for, runs_with_scenarios

PO_SS_001 = 1


@contextmanager
def approved_api(tmp_path, **kw):
    """The demo database where real invoice 10963 was approved on PO-SS-001: one commit, line 1 consumed, the rest on the PO total."""
    with api(tmp_path, run_fn=runs_with_scenarios(), **kw) as c:
        rid = run_and_wait(c, SS_10963)
        iid = item_for(c, rid)
        token = c.get(f"/api/review-queue/{iid}").json()["approve"]["state_token"]
        r = c.post(f"/api/review-queue/{iid}/approve", json={"confirm": True, "state_token": token, "allocations": []})
        assert r.status_code == 200, r.text
        yield c


def db(c):
    return closing(open_db(c.db_path, c.settings))


def get_export(c, path: str, **params):
    return c.get(path, params=params)


def csv_rows(body: bytes) -> list[list[str]]:
    assert body.startswith("﻿".encode("utf-8"))
    return list(csv.reader(io.StringIO(body.decode("utf-8-sig"), newline="")))


def csv_sections(body: bytes) -> tuple[dict[str, str], dict[str, list[list[str]]]]:
    """(header lines, {section title: [header row, *rows]}) of a stacked detail CSV."""
    rows = csv_rows(body)
    meta, sections, current = {}, {}, None
    for row in rows:
        if not row:
            continue
        if len(row) == 1 and row[0].startswith("[") and row[0].endswith("]"):
            current = row[0][1:-1]
            sections[current] = []
        elif current is None:
            meta[row[0]] = row[1]
        else:
            sections[current].append(row)
    return meta, sections


def xlsx_book(body: bytes):
    return openpyxl.load_workbook(io.BytesIO(body))


def money(value) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def insert_po(c, *, po_number: str, vendor: str, lines: list[tuple[str, str, str, int]] = (), currency="USD", total=100_00,
              ledger: list[tuple[str, int]] = ()) -> int:
    """A hand-made PO (vendor, PO, lines (description, qty, unit price, amount cents), optional ledger rows on a historic invoice)."""
    with db(c) as conn:
        with conn:
            vid = conn.execute("INSERT INTO vendors (name, status) VALUES (?, 'approved')", (vendor,)).lastrowid
            pid = conn.execute("INSERT INTO purchase_orders (po_number, vendor_id, currency, total_amount, status, meta) "
                               "VALUES (?, ?, ?, ?, 'open', '{\"source\": \"manual\"}')", (po_number, vid, currency, total)).lastrowid
            for n, (desc, qty, price, amount) in enumerate(lines, start=1):
                conn.execute("INSERT INTO po_lines (po_id, line_no, description, quantity, unit_price, amount) VALUES (?, ?, ?, ?, ?, ?)",
                             (pid, n, desc, qty, price, amount))
            if ledger:
                inv = conn.execute("INSERT INTO invoices (vendor_id, invoice_number, currency, total, po_id, status) "
                                   "VALUES (?, 'HIST-X', ?, 100, ?, 'approved')", (vid, currency, pid)).lastrowid
                for type_, amount in ledger:
                    conn.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (?, ?, ?, ?)", (pid, inv, amount, type_))
    return pid
