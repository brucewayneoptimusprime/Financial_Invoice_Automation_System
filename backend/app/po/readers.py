"""One uploaded PO document -> what the model sees (PLAN "PO integration" section 2, owner decision 4).

PDF / PNG / JPG: the existing ingest (magic bytes, size, page cap, render, text layer) into the draft's folder, then the same
text-and-vision / vision-only choice as invoices. DOCX: stdlib zip + XML (paragraphs and table cells in order). XLSX: openpyxl,
read-only, cached values only (formulas are never evaluated, macros never run). CSV: stdlib csv with a sniffed delimiter.
Refused with a clear message: legacy .doc/.xls (and password-protected Office files, which share that container), macro-enabled
files, zip bombs, oversize and unsupported files. One PO per document: nothing here imports in bulk.
"""
import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from app.config import Settings
from app.extraction.extractor import choose_path, load_pages, read_page_texts
from app.extraction.prompts import PagePayload
from app.ingest.stage import run_ingest_stage
from app.ingest.validate import IngestRejected, sniff_media_type
from app.models.run import RunContext

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_ZIP_MAX_MEMBERS = 5000
_MAX_SHEETS = 10


class PODocRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class DocInput:
    kind: str                                   # rendered | text
    media_type: str
    file_name: str
    sha256: str
    size_bytes: int
    pages: list[PagePayload] = field(default_factory=list)
    page_texts: dict[int, str | None] = field(default_factory=dict)
    text_usable: bool = False
    total_pages: int = 1
    path: str | None = None                     # extraction path for rendered documents
    notes: list[str] = field(default_factory=list)
    failure_code: str | None = None             # the document could not be read (no model call)
    failure_message: str | None = None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cap_text(text: str, settings: Settings, notes: list[str]) -> str:
    if len(text) > settings.po_doc_text_max_chars:
        notes.append(f"[system] the document text was cut at {settings.po_doc_text_max_chars} characters")
        return text[: settings.po_doc_text_max_chars]
    return text


def _docx_text(zf: zipfile.ZipFile) -> str:
    root = ElementTree.fromstring(zf.read("word/document.xml"))
    body = root.find(f"{_W}body")
    out: list[str] = []

    def para(p) -> str:
        return "".join(t.text or "" for t in p.iter(f"{_W}t")).strip()

    for child in (body if body is not None else []):
        if child.tag == f"{_W}p":
            text = para(child)
            if text:
                out.append(text)
        elif child.tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells = [" ".join(filter(None, (para(p) for p in cell.iter(f"{_W}p")))) for cell in row.iter(f"{_W}tc")]
                if any(cells):
                    out.append(" | ".join(cells))
    return "\n".join(out)


def _xlsx_text(data: bytes, settings: Settings, notes: list[str]) -> str:
    import openpyxl                                                      # only needed for spreadsheets
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out: list[str] = []
    cells = 0
    try:
        for index, ws in enumerate(wb.worksheets):
            if index >= _MAX_SHEETS:
                notes.append(f"[system] only the first {_MAX_SHEETS} sheets were read")
                break
            out.append(f"Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                values = ["" if v is None else str(v) for v in row]
                cells += len(values)
                if cells > settings.po_sheet_max_cells:
                    notes.append(f"[system] the spreadsheet was cut at {settings.po_sheet_max_cells} cells")
                    return "\n".join(out)
                if any(v.strip() for v in values):
                    out.append(" | ".join(values).rstrip(" |"))
    finally:
        wb.close()
    return "\n".join(out)


def _csv_text(data: bytes) -> str:
    if b"\x00" in data:
        raise PODocRejected("unsupported_type", "The file is not a text CSV file.")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise PODocRejected("unsupported_type", "The CSV file could not be decoded as text.")
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:                                                   # uneven rows: the most frequent candidate wins
        delimiter = max(",;\t|", key=sample.count)
    rows = [" | ".join(c.strip() for c in row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return "\n".join(r for r in rows if r.strip(" |"))


def _open_zip(data: bytes, settings: Settings) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PODocRejected("unsupported_type", "The file looks like a damaged Office document.")
    infos = zf.infolist()
    if len(infos) > _ZIP_MAX_MEMBERS or sum(i.file_size for i in infos) > settings.po_zip_max_uncompressed_bytes:
        raise PODocRejected("too_large", "The Office document expands to more than the allowed size.")
    names = {i.filename for i in infos}
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise PODocRejected("macro_enabled", "Macro-enabled Office files are not accepted; save it as .docx, .xlsx or PDF.")
    return zf


def read_po_document(path: Path, file_name: str, draft_id: str, settings: Settings) -> DocInput:
    size = path.stat().st_size
    if size == 0:
        raise PODocRejected("empty_file", f"{file_name} is empty.")
    if size > settings.max_file_bytes:
        raise PODocRejected("too_large", f"{file_name} is larger than {settings.max_file_bytes / 1_048_576:.1f} MB.")
    data = path.read_bytes()
    digest = _sha256(data)
    head = data[:16]
    notes: list[str] = []

    media = sniff_media_type(head)
    if media is not None:                                                   # PDF / PNG / JPEG: the invoice ingest, unchanged
        ctx = RunContext(run_id=draft_id, source_file=file_name)
        try:
            run_ingest_stage(ctx, path, settings.model_copy(update={"runs_dir": Path(settings.po_drafts_dir)}))
        except IngestRejected as exc:
            raise PODocRejected(exc.code, exc.message)
        ingest = ctx.ingest
        doc = DocInput(kind="rendered", media_type=media, file_name=file_name, sha256=digest, size_bytes=size,
                       total_pages=ingest.pages_processed or 1, text_usable=ingest.text_layer.usable, notes=notes)
        if ingest.failure_kind is not None:
            doc.failure_code, doc.failure_message = ingest.failure_code, ingest.failure_reason
            return doc
        if ingest.truncated:
            notes.append(f"[system] only the first {ingest.pages_processed} of {ingest.pages_total} pages were read")
        doc.path, _ = choose_path(settings.extraction_mode, ingest.text_layer.usable)
        doc.pages = load_pages(ingest, doc.path)
        doc.page_texts = read_page_texts(ingest)
        return doc

    if head.startswith(OLE_MAGIC):
        raise PODocRejected("legacy_office", "This is an old-format .doc/.xls file (or a password-protected Office file). "
                                             "Save it as .docx, .xlsx or PDF and upload that.")
    if head.startswith(ZIP_MAGIC):
        zf = _open_zip(data, settings)
        names = set(zf.namelist())
        if "word/document.xml" in names:
            text, media = _docx_text(zf), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif "xl/workbook.xml" in names:
            text, media = _xlsx_text(data, settings, notes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            raise PODocRejected("unsupported_type", f"{file_name} is a zip file but not a Word or Excel document.")
    elif file_name.lower().endswith(".csv"):
        text, media = _csv_text(data), "text/csv"
    else:
        raise PODocRejected("unsupported_type", f"{file_name} is not a supported purchase-order document "
                                                "(PDF, PNG, JPG, DOCX, XLSX or CSV).")
    text = _cap_text(text.strip(), settings, notes)
    if not text:
        raise PODocRejected("blank_document", f"{file_name} contains no text.")
    folder = Path(settings.po_drafts_dir) / draft_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "document.txt").write_text(text, encoding="utf-8")
    return DocInput(kind="text", media_type=media, file_name=file_name, sha256=digest, size_bytes=size,
                    pages=[PagePayload(number=1, text=text)], page_texts={1: text}, text_usable=True, notes=notes)
