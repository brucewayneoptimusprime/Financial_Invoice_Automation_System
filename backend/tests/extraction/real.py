"""The two REAL invoices (SuperStore 10963 and 24429): their PDFs and the model replies recorded from the live check.

Nothing here calls the API. The reply is fed to the real extractor through a scripted client; the PDF goes through the
real ingest stage, so the grounding tests see the real text layer (labels and values in separate blocks).
"""
import json
from pathlib import Path

from app.extraction.extractor import extract_invoice
from app.ingest.stage import run_ingest_stage
from app.models import RunContext
from tests.extraction.helpers import settings
from tests.llm.fakes import FakeLLMClient, ok_response

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
NAMES = ("superstore_10963", "superstore_24429")           # the label-separated native PDFs
IQ = "iq_electronics"                                      # a scanned photo: no text layer, Indian GST, amounts in words


def real_reply(name: str) -> dict:
    return json.loads((REAL / f"{name}.reply.json").read_text(encoding="utf-8"))


def real_pdf(name: str) -> Path:
    """The real document for `name`: a PDF, or a photo (.jpg) for the scanned IQ Electronics invoice."""
    return next(p for p in (REAL / f"{name}.pdf", REAL / f"{name}.jpg") if p.is_file())


def real_ingest(tmp_path, name: str, **kw):
    """(ctx with ingest set, settings) for one real PDF, using the real ingest stage."""
    ctx = RunContext(run_id=f"real-{name[-5:]}", source_file=real_pdf(name).name)
    cfg = settings(tmp_path, **kw)
    run_ingest_stage(ctx, real_pdf(name), cfg)
    return ctx, cfg


def real_extraction(tmp_path, name: str, **kw):
    """(ctx, cfg, ExtractionOutcome): the recorded reply run through ingest, extraction, post-processing and grounding."""
    ctx, cfg = real_ingest(tmp_path, name, **kw)
    reply = json.dumps(real_reply(name), ensure_ascii=False)
    outcome = extract_invoice(ctx.ingest, client=FakeLLMClient(ok_response(reply, input_tokens=6800, output_tokens=920)),
                              settings=cfg, run_id=ctx.run_id)
    return ctx, cfg, outcome
