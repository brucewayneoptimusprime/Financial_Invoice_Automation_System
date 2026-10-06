"""One cross-check analysis: the uploaded files -> a report. REPORT ONLY and stateless: the report exists in the return value and
nowhere else. Nothing here writes to the database (the caller hands in facts read from a read-only connection), and every file
this touches lives in the per-analysis work folder the caller removes.

Each file succeeds or fails on its own: a rejected file, an unreadable one, a model failure, a replay miss or a per-document
ceiling hit becomes that document's `failed` entry with a clear message, and the other documents still report. The one refusal
for the whole analysis is the budget pre-check: when the worst-case cost of all the requests together exceeds what the session
ceiling still allows, nothing is sent at all.
"""
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config import Settings
from app.crosscheck.compare import compare_document
from app.crosscheck.facts import POContext
from app.crosscheck.prompts import CROSSCHECK_PROMPT_VERSION, document_parts
from app.crosscheck.reader import build_request, read_document
from app.ingest.validate import IngestRejected, validate_file
from app.llm.budget import CostTracker
from app.llm.errors import PriceNotConfigured
from app.llm.pricing import price_for, worst_case_cost
from app.llm.types import LLMClient
from app.po.readers import PODocRejected, read_po_document

REPORT_ONLY_LABEL = "Report only: nothing here changes the PO, its invoices, the ledger or any decision."
OFFLINE_MESSAGE = "Offline mode: no model is available to read documents."
REPLAY_NOTE = "Replay mode: only a document with a recorded answer can be analysed."
REPLAY_MISS = "Replay mode: no recorded answer exists for this document."


class BudgetRefused(Exception):
    def __init__(self, message: str, projected: Decimal, remaining: Decimal):
        super().__init__(message)
        self.message, self.projected, self.remaining = message, projected, remaining


@dataclass
class Incoming:
    """One uploaded file as the route saved it: a path, or the reason it was not kept."""
    name: str
    path: Path | None = None
    rejection: tuple[str, str] | None = None        # (code, message)


def usd(value: Decimal) -> str:
    return f"{Decimal(value):.6f}"


def info(settings: Settings, mode: str, tracker: CostTracker | None) -> dict[str, Any]:
    """What the screen shows BEFORE Analyze. No model, no file, no cost."""
    offline = mode == "offline"
    return {
        "enabled": True, "mode": mode, "available": not offline,
        "message": OFFLINE_MESSAGE if offline else REPLAY_NOTE if mode == "replay" else None,
        "label": REPORT_ONLY_LABEL, "max_documents": settings.crosscheck_max_documents,
        "max_file_mb": round(settings.max_file_bytes / 1_048_576, 1), "accepted": ["PDF", "PNG", "JPG"],
        "typical_cost_per_document_usd": str(settings.crosscheck_typical_cost_usd),
        "ceiling_per_document_usd": str(settings.cost_ceiling_per_run_usd),
        "budget_remaining_usd": None if tracker is None else str(tracker.remaining().quantize(Decimal("0.01"))),
    }


def _failed(name: str, code: str, message: str) -> dict[str, Any]:
    return {"file_name": name, "status": "failed", "failure": {"code": code, "message": message}, "cost_usd": usd(Decimal(0))}


def analyse(po: POContext, files: list[Incoming], work: Path, *, client: LLMClient, tracker: CostTracker | None,
            settings: Settings, mode: str) -> dict[str, Any]:
    analysis_id = uuid.uuid4().hex
    doc_settings = settings.model_copy(update={"po_drafts_dir": Path(work) / "pages"})
    documents: list[dict[str, Any] | None] = []
    ready: list[tuple[int, Any, Any, str]] = []                          # (index, document, parts, run key)
    for n, f in enumerate(files, start=1):
        documents.append(None)
        if f.rejection is not None or f.path is None:
            code, message = f.rejection or ("no_file", f"{f.name} could not be saved.")
            documents[-1] = _failed(f.name, code, message)
            continue
        try:
            validate_file(f.path, settings)                              # PDF / PNG / JPG by magic bytes; empty; size
            doc = read_po_document(f.path, f.name, f"doc{n}", doc_settings)   # the invoice ingest: render, text layer, page cap
        except (IngestRejected, PODocRejected) as exc:
            documents[-1] = _failed(f.name, exc.code, exc.message.replace(f.path.name, f.name))
            continue
        if doc.failure_code is not None:                                 # e.g. password-protected or blank: no model call
            documents[-1] = _failed(f.name, doc.failure_code, doc.failure_message or "The document could not be read.")
            continue
        ready.append((n - 1, doc, document_parts(doc.pages, doc.total_pages), f"crosscheck-{analysis_id}-{n}"))

    if tracker is not None and ready:                                    # all or nothing, before any call
        try:
            price = price_for(settings.model_name, settings.llm_prices)
            projected = sum((worst_case_cost(build_request(parts, settings, key), price) for _, _, parts, key in ready), Decimal(0))
        except PriceNotConfigured:
            projected = None                                             # each read then fails with that clear message
        if projected is not None and projected > tracker.remaining():
            remaining = tracker.remaining()
            raise BudgetRefused(f"The model budget left for this server session (${remaining:.2f}) does not cover this analysis "
                                f"(it could cost up to ${projected:.2f}). Nothing was sent.", projected, remaining)

    cost, tokens_in, tokens_out, model = Decimal(0), 0, 0, settings.model_name
    for index, doc, parts, key in ready:
        facts = read_document(parts, doc.page_texts, doc.text_usable, client=client, settings=settings, run_key=key)
        cost, tokens_in, tokens_out, model = cost + facts.cost_usd, tokens_in + facts.tokens_in, tokens_out + facts.tokens_out, facts.model or model
        if facts.status != "ok":
            message = REPLAY_MISS if facts.failure_code == "replay_miss" else facts.failure_message or "The document could not be read."
            entry = _failed(doc.file_name, facts.failure_code or "failed", message)
        else:
            entry = {"file_name": doc.file_name, "status": "analysed", "failure": None, **compare_document(facts, po, settings)}
            entry["notices"] = [*entry["notices"], *(n.replace("[system] ", "").capitalize() for n in doc.notes)]
        entry.update(cost_usd=usd(facts.cost_usd), pages=doc.total_pages)
        documents[index] = entry

    return {
        "analysis": {"po_id": po.po_id, "po_number": po.po_number, "mode": mode, "documents": len(files),
                     "analysed": sum(1 for d in documents if d["status"] == "analysed"), "cost_usd": usd(cost),
                     "tokens_in": tokens_in, "tokens_out": tokens_out, "model": model, "prompt_version": CROSSCHECK_PROMPT_VERSION,
                     "estimated_before_usd": str(settings.crosscheck_typical_cost_usd * len(files))},
        "label": REPORT_ONLY_LABEL,
        "documents": documents,
    }
