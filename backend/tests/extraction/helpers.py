"""Shared helpers for extraction tests: recorded-style reply fixtures and generated, ingested documents."""
import json
from pathlib import Path

from app.config import Settings
from app.ingest.stage import run_ingest_stage
from app.models import RunContext
from app.models.extraction_meta import IngestInfo
from tests.ingest import docs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"

NATIVE_PAGES = [
    ["Northwind Trading Co    INVOICE", "Invoice No: INV-2026-0042        Date: March 14, 2026", "Customer PO: PO-5001",
     "Widget A WID-A 10 60.00 600.00", "Widget B WID-B 5 80.00 400.00", "Subtotal: 1,000.00   Shipping: 25.00"],
    ["Tax (8%): 80.00", "Total Due: 1,105.00", "Thank you for your business - payment due within 30 days"],
]


def load_reply(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def reply_text(reply: dict | str) -> str:
    return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


def settings(tmp_path, **kw) -> Settings:
    return Settings(_env_file=None, runs_dir=tmp_path / "runs", **kw)


def make_source(tmp_path, kind: str) -> Path:
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    if kind == "native":
        return docs.make_native_pdf(src / "invoice.pdf", NATIVE_PAGES)
    if kind == "scanned":
        return docs.make_scanned_pdf(src / "scan.pdf", pages=2)
    if kind == "png":
        return docs.make_png(src / "photo.png")
    if kind == "locked":
        return docs.make_native_pdf(src / "locked.pdf", NATIVE_PAGES, password="pw")
    if kind == "blank":
        return docs.make_blank_pdf(src / "blank.pdf")
    if kind == "corrupt":
        path = src / "bad.pdf"
        path.write_bytes(b"%PDF-1.4\nnot really a pdf")
        return path
    raise ValueError(kind)


def ingest_of(tmp_path, kind: str = "native", run_id: str = "run-1", **kw) -> IngestInfo:
    """Run the real ingest stage on a generated document and return its IngestInfo."""
    path = make_source(tmp_path, kind)
    ctx = RunContext(run_id=run_id, source_file=path.name)
    run_ingest_stage(ctx, path, settings(tmp_path, **kw))
    return ctx.ingest
