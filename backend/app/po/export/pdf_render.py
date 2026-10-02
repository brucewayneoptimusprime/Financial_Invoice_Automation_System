"""PDF (reportlab platypus): A4 landscape; title, the header lines, then each section as a heading and a table. Table headers repeat
on every page, long text wraps, money is right-aligned as 1,500.00, and every page has the footer "Page N of M". Built-in Helvetica:
characters it cannot draw become "?" and the footer says so (owner decision 3).
"""
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table as RLTable, TableStyle

from app.po.export.model import ExportDoc, KeyValues, Table
from app.po.export.textsafe import display, header, latin

PAGE = landscape(A4)
MARGIN = 14 * mm
WIDTH = PAGE[0] - 2 * MARGIN
REPLACED_NOTE = 'Some characters could not be drawn in this PDF font and are shown as "?". The Word, Excel and CSV exports keep them.'

_base = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_base["Heading1"], fontName="Helvetica-Bold", fontSize=16, spaceAfter=4)
H2 = ParagraphStyle("h2", parent=_base["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, spaceBefore=10, spaceAfter=4)
CELL = ParagraphStyle("cell", parent=_base["Normal"], fontName="Helvetica", fontSize=8, leading=10)
CELL_B = ParagraphStyle("cellb", parent=CELL, fontName="Helvetica-Bold")
CELL_R = ParagraphStyle("cellr", parent=CELL, alignment=TA_RIGHT)
META = ParagraphStyle("meta", parent=CELL, fontSize=8.5, leading=11)
_WEIGHT = {"money": 1.15, "qty": 1.0, "price": 1.1, "int": 0.95, "text": 2.2}


class _State:
    replaced = False


def _p(text: str, style, state: _State) -> Paragraph:
    safe, replaced = latin(text)
    state.replaced |= replaced
    return Paragraph(escape(safe).replace("\n", "<br/>"), style)


def _widths(section: Table) -> list[float]:
    weights = [4.5 if c.label == "Description" else _WEIGHT.get(c.kind, 2.0) for c in section.columns]
    total = sum(weights)
    return [WIDTH * w / total for w in weights]


_GRID = TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cfd4dc")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef0ff")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)])


def _table(section: Table, doc: ExportDoc, state: _State) -> list:
    data = [[_p(header(c.label, c.kind, doc.currency), CELL_B, state) for c in section.columns]]
    if not section.rows:
        return [_p("None", CELL, state)]
    for row in section.rows:
        data.append([_p(display(v, c.kind), CELL_R if c.kind in ("money", "qty", "price", "int") else CELL, state)
                     for v, c in zip(row, section.columns)])
    t = RLTable(data, colWidths=_widths(section), repeatRows=1)
    t.setStyle(_GRID)
    out = [t]
    if section.note:
        out.append(_p(section.note, CELL, state))
    return out


def _kv(section: KeyValues, doc: ExportDoc, state: _State) -> list:
    data = [[_p(label, CELL_B, state), _p(display(value, kind) + (f" {doc.currency}" if kind == "money" and doc.currency else ""),
                                           CELL, state)] for label, value, kind in section.items]
    t = RLTable(data, colWidths=[WIDTH * 0.28, WIDTH * 0.72])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cfd4dc")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f3f6"))]))
    return [t]


def _numbered_canvas(footer: str, state: _State):
    """A canvas that knows the page count, so every page can say "Page N of M" (and the replacement note if needed)."""
    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for page in self._pages:
                self.__dict__.update(page)
                self.setFont("Helvetica", 7.5)
                self.setFillColor(colors.HexColor("#6b7280"))
                self.drawString(MARGIN, 8 * mm, latin(footer)[0])
                self.drawRightString(PAGE[0] - MARGIN, 8 * mm, f"Page {self._pageNumber} of {total}")
                if state.replaced:
                    self.drawString(MARGIN, 4.5 * mm, REPLACED_NOTE)
                super().showPage()
            super().save()
    return NumberedCanvas


def render(doc: ExportDoc) -> bytes:
    state = _State()
    story = [_p(doc.title, H1, state)]
    story += [_p(f"{label}: {value}", META, state) for label, value in doc.meta]
    story.append(Spacer(1, 4))
    for section in doc.sections:
        if doc.kind == "detail":
            story.append(_p(section.title, H2, state))
        story += _kv(section, doc, state) if isinstance(section, KeyValues) else _table(section, doc, state)
    buf = io.BytesIO()
    generated = dict(doc.meta).get("Generated (UTC)", "")
    pdf = SimpleDocTemplate(buf, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=16 * mm,
                            title=latin(doc.title)[0], author="Invoice Agent", subject="Purchase order export")
    pdf.build(story, canvasmaker=_numbered_canvas(f"Exported from Invoice Agent  ·  {generated} UTC  ·  {doc.title}", state))
    return buf.getvalue()
