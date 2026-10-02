"""Word (python-docx): a landscape page; Heading 1 title, a paragraph per header line, then Heading 2 + a table per section ("Light Grid
Accent 1"; the header row repeats on every page; money right-aligned as 1,500.00). Every character is kept (control characters
removed, since Word's XML forbids them)."""
import io

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt

from app.po.export.model import ExportDoc, KeyValues, Table
from app.po.export.textsafe import clean, display, header

STYLE = "Light Grid Accent 1"


def _repeat_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    flag = OxmlElement("w:tblHeader")
    flag.set(qn("w:val"), "true")
    tr_pr.append(flag)


def _cell(cell, text: str, *, bold: bool = False, right: bool = False) -> None:
    cell.text = ""
    para = cell.paragraphs[0]
    run = para.add_run(text)
    run.bold = bold
    run.font.size = Pt(8.5)
    if right:
        para.alignment = WD_ALIGN_PARAGRAPH.RIGHT


def _table(document, section: Table, currency: str | None) -> None:
    if not section.rows:
        document.add_paragraph("None")
        return
    t = document.add_table(rows=1, cols=len(section.columns))
    t.style = STYLE
    for cell, col in zip(t.rows[0].cells, section.columns):
        _cell(cell, header(col.label, col.kind, currency), bold=True)
    _repeat_header(t.rows[0])
    for row in section.rows:
        cells = t.add_row().cells
        for cell, value, col in zip(cells, row, section.columns):
            _cell(cell, display(value, col.kind), right=col.kind in ("money", "qty", "price", "int"))
    if section.note:
        document.add_paragraph(section.note).runs[0].italic = True


def _kv(document, section: KeyValues, currency: str | None) -> None:
    t = document.add_table(rows=0, cols=2)
    t.style = STYLE
    for label, value, kind in section.items:
        cells = t.add_row().cells
        _cell(cells[0], clean(label), bold=True)
        _cell(cells[1], display(value, kind) + (f" {currency}" if kind == "money" and currency else ""))


def render(doc: ExportDoc) -> bytes:
    document = Document()
    sec = document.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = Mm(297), Mm(210)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Mm(15))
    document.core_properties.title = clean(doc.title)
    document.core_properties.author = "Invoice Agent"
    document.add_heading(clean(doc.title), level=1)
    for label, value in doc.meta:
        para = document.add_paragraph()
        para.add_run(f"{clean(label)}: ").bold = True
        para.add_run(clean(value))
    for section in doc.sections:
        if doc.kind == "detail":
            document.add_heading(clean(section.title), level=2)
        if isinstance(section, KeyValues):
            _kv(document, section, doc.currency)
        else:
            _table(document, section, doc.currency)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()
