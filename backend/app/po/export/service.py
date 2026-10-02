"""Format registry and the two entry points the routes call. Read-only."""
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from app.po.export import csv_render, docx_render, pdf_render, xlsx_render
from app.po.export.model import ExportDoc, ExportError, detail_doc, parse_ids, summary_doc
from app.po.export.safety import content_disposition

FORMATS: dict[str, tuple[str, str, Callable[[ExportDoc], bytes] | None]] = {
    "pdf": ("application/pdf", "pdf", pdf_render.render),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx", docx_render.render),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx", xlsx_render.render),
    "csv": ("text/csv; charset=utf-8", "csv", csv_render.render),
}


def _renderer(fmt: str):
    if fmt not in FORMATS:
        raise ExportError(422, "bad_format", "format must be pdf, docx, xlsx or csv.")
    media, ext, render = FORMATS[fmt]
    if render is None:
        raise ExportError(422, "not_available", f"{fmt.upper()} export is not available yet.")
    return media, ext, render


def _file(doc: ExportDoc, fmt: str) -> tuple[bytes, str, dict]:
    media, ext, render = _renderer(fmt)
    filename = f"{doc.filename_stem}.{ext}"
    return render(doc), media, {"Content-Disposition": content_disposition(filename), "Cache-Control": "no-store"}


def export_summary(conn: sqlite3.Connection, *, fmt: str, q: str | None, status: str | None, currency: str | None, ids: str | None,
                   generated: datetime | None = None) -> tuple[bytes, str, dict]:
    _renderer(fmt)                                                         # refuse a bad format before any work
    doc = summary_doc(conn, q=q, status=status, currency=currency, ids=parse_ids(ids), generated=generated or datetime.now(timezone.utc))
    return _file(doc, fmt)


def export_detail(conn: sqlite3.Connection, po_id: int, *, fmt: str, level: str,
                  generated: datetime | None = None) -> tuple[bytes, str, dict]:
    _renderer(fmt)
    doc = detail_doc(conn, po_id, level=level, generated=generated or datetime.now(timezone.utc))
    return _file(doc, fmt)
