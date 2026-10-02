"""CSV: UTF-8 with a BOM (Excel opens it correctly), CRLF, RFC 4180 quoting. Money as plain 1500.00 for machines.

Summary: the header lines as Label,Value rows, a blank row, then the one table.
Detail:  the header lines, then stacked sections; each starts with a marker row [Section name], then its header row
         (Field,Value for key/value sections), its rows ("None" when empty), and a blank row.
Every user-derived text goes through `safe_cell` (formula injection); numbers are written as numbers.
"""
import csv
import io
from decimal import Decimal
from typing import Any

from app.po.export.model import ExportDoc, KeyValues, Table
from app.po.export.safety import safe_cell


def plain(value: Any, kind: str) -> str:
    if value is None:
        return ""
    if kind == "money":
        return f"{Decimal(value):.2f}"
    if kind in ("qty", "price"):
        return str(value)                                                  # the view's own decimal text (e.g. 4, 2.5, 1893.30)
    if kind == "int":
        return str(int(value))
    return safe_cell(str(value))


def render(doc: ExportDoc) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    for label, value in doc.meta:
        w.writerow([safe_cell(label), safe_cell(value)])
    for section in doc.sections:
        w.writerow([])
        if doc.kind == "detail":
            w.writerow([f"[{section.title}]"])
        if isinstance(section, KeyValues):
            w.writerow(["Field", "Value"])
            for label, value, kind in section.items:
                w.writerow([safe_cell(label), plain(value, kind)])
        elif isinstance(section, Table):
            w.writerow([c.label for c in section.columns])
            if not section.rows:
                w.writerow(["None"])
            for row in section.rows:
                w.writerow([plain(v, c.kind) for v, c in zip(row, section.columns)])
            if section.note:
                w.writerow([section.note])
    return ("﻿" + buf.getvalue()).encode("utf-8")
