"""Orchestrate one extraction: choose the path, build the request, call the model, validate, repair once, or
DEGRADE to an all-null extraction with a reason. It never raises for a failure of the document, the model, the
network, the key or the budget: the run must flow on (to `review` / `request_info`), not crash.

Failure kinds (SPEC section 11):
  vendor_side  the document is the problem (password-protected, blank)  -> the vendor is asked to resend
  system_side  ours (renderer, config, API, schema, cost ceiling)       -> a human reviews
"""
import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.enums import Outcome
from app.extraction.grounding import ground_invoice
from app.extraction.injection import scan_pages
from app.extraction.postprocess import postprocess
from app.extraction.prompts import PROMPT_VERSION, PagePayload, build_user_parts, repair_part, system_prompt_for
from app.extraction.wire import from_wire, wire_schema
from app.llm.client import build_llm_client
from app.llm.errors import LLMConfigError, LLMError, LLMRefusedError, LLMTruncatedError
from app.llm.types import LLMClient, LLMRequest, LLMResponse
from app.models.audit import AuditEvent
from app.models.extraction import ExtractedInvoice
from app.models.extraction_meta import ExtractionMeta, ExtractionPath, IngestInfo, LLMCallRecord

logger = logging.getLogger("app.extraction")
EXTRACT_STAGE = "extract"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_MAX_BRACE_TRIES = 50
_BOM = "\ufeff"


@dataclass
class ExtractionOutcome:
    invoice: ExtractedInvoice
    meta: ExtractionMeta
    events: list[AuditEvent] = field(default_factory=list)
    raw_replies: list[str] = field(default_factory=list)        # what the model actually said, one per attempt


# ----------------------------------------------------------------------------------------------- helpers

def _event(event_type: str, outcome: Outcome, message: str, detail: dict[str, Any]) -> AuditEvent:
    return AuditEvent(stage=EXTRACT_STAGE, event_type=event_type, outcome=outcome, message=message, detail=detail)


def choose_path(mode: str, text_usable: bool) -> tuple[ExtractionPath, str | None]:
    """(path, note). `auto` sends both text and images when a usable text layer exists, images only otherwise."""
    if mode == "vision":
        return "vision_only", None
    if mode == "text":
        return ("text_only", None) if text_usable else ("vision_only", "extraction_mode=text needs a usable text layer; used vision instead")
    if mode == "text_and_vision":
        return ("text_and_vision", None) if text_usable else ("vision_only", "no usable text layer; used vision only")
    return ("text_and_vision", None) if text_usable else ("vision_only", None)          # auto


def degraded_invoice(code: str, reason: str, issues: list[str] | None = None) -> ExtractedInvoice:
    invoice = ExtractedInvoice()
    invoice.document_quality.issues = list(dict.fromkeys([code, *(issues or [])]))
    invoice.extraction_notes = f"[system] Extraction failed ({code}): {reason}"
    return invoice


def load_pages(ingest: IngestInfo, path: ExtractionPath) -> list[PagePayload]:
    """Read the stored page images / text for the chosen path."""
    pages: list[PagePayload] = []
    for index, page in enumerate(ingest.pages):
        text_path = ingest.text_paths[index] if index < len(ingest.text_paths) else None
        text = Path(text_path).read_text(encoding="utf-8") if text_path and path != "vision_only" else None
        image = None if path == "text_only" else Path(page.path).read_bytes()
        pages.append(PagePayload(number=page.number, image_media_type=None if image is None else page.media_type,
                                 image_data=image, text=text))
    return pages


def parse_reply(text: str, expect_key: str | None = None) -> Any:
    """Parse the model's JSON object. Tolerates a BOM, a ```json code fence, and stray text before or after the
    object (needed when the reply was not grammar-constrained). When the whole text is not JSON, the first embedded
    object that (if `expect_key` is given) contains that key is returned, so a TRUNCATED reply is not mistaken for one of
    its own inner objects. Raises json.JSONDecodeError if no suitable object is found."""
    cleaned = _FENCE.sub("", text.strip().lstrip(_BOM)).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as first:
        decoder = json.JSONDecoder()
        for tries, match in enumerate(re.finditer(r"\{", cleaned)):
            if tries >= _MAX_BRACE_TRIES:
                break
            try:
                obj, _ = decoder.raw_decode(cleaned[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and (expect_key is None or expect_key in obj):
                return obj
        raise first


def _summarise(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        items = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]]
        return "; ".join(items)[:500]
    if isinstance(exc, json.JSONDecodeError):
        return f"not valid JSON ({exc.msg} at position {exc.pos})"
    return str(exc)[:300]


# --------------------------------------------------------------------------------------------- the extractor

def extract_invoice(ingest: IngestInfo, *, client: LLMClient | None = None, settings: Settings | None = None,
                    run_id: str | None = None) -> ExtractionOutcome:
    settings = settings or get_settings()
    meta = ExtractionMeta(
        mode_requested=settings.extraction_mode, model=settings.model_name, prompt_version=PROMPT_VERSION,
        pages_processed=ingest.pages_processed, truncated=ingest.truncated, text_layer_usable=ingest.text_layer.usable)
    outcome = ExtractionOutcome(invoice=ExtractedInvoice(), meta=meta)

    def degrade(kind: str, code: str, reason: str) -> ExtractionOutcome:
        meta.degraded, meta.failure_kind, meta.failure_code, meta.failure_reason = True, kind, code, reason
        outcome.invoice = degraded_invoice(code, reason, ingest.issues)
        outcome.events.append(_event("extraction_degraded", Outcome.FAIL, f"Extraction degraded ({kind}): {reason}",
                                     {"failure_kind": kind, "failure_code": code, "reason": reason,
                                      "tokens_in": meta.tokens_in, "tokens_out": meta.tokens_out, "cost_usd": meta.cost_usd}))
        logger.warning("extraction degraded run=%s kind=%s code=%s", run_id, kind, code)
        return outcome

    # 1. The document itself could not be read: no model call, $0.
    if ingest.failure_kind is not None:
        return degrade(ingest.failure_kind, ingest.failure_code or "unreadable_document",
                       ingest.failure_reason or "The document could not be read.")

    # 2. Path selection.
    path, path_note = choose_path(settings.extraction_mode, ingest.text_layer.usable)
    meta.path = path
    mode = settings.llm_structured_output
    meta.structured_output = mode
    outcome.events.append(_event("path_selected", Outcome.INFO,
                                 f"Extraction path: {path} (text layer: {ingest.text_layer.reason}; mode: {settings.extraction_mode})."
                                 + (f" {path_note}." if path_note else ""),
                                 {"path": path, "mode": settings.extraction_mode, "text_layer": ingest.text_layer.reason,
                                  "note": path_note}))

    # 3. The client (a missing key is a clear degrade, not a crash).
    if client is None:
        try:
            client = build_llm_client(settings)
        except LLMConfigError as exc:
            return degrade("system_side", exc.code, exc.message)

    # 4. The request.
    try:
        pages = load_pages(ingest, path)
    except OSError as exc:
        return degrade("system_side", "page_read_failed", f"Could not read the stored page files ({type(exc).__name__}).")
    base_parts = build_user_parts(pages, ingest.pages_processed, ingest.pages_total if ingest.truncated else None)
    system = system_prompt_for(mode)                          # prompt_json: the schema travels as prompt text
    schema = wire_schema() if mode == "json_schema" else None  # prompt_json: NO strict schema is sent to the API

    # 5. Call, validate, repair once.
    attempts_allowed = 1 + settings.schema_repair_retries
    last_error = ""
    for attempt in range(1, attempts_allowed + 1):
        parts = base_parts + ((repair_part(last_error),) if attempt > 1 else ())
        request = LLMRequest(system=system, parts=parts, model=settings.model_name,
                             max_output_tokens=settings.llm_max_output_tokens, schema=schema, run_id=run_id,
                             purpose="extract", cache_system=settings.llm_cache_system_prompt)
        meta.attempts = attempt
        try:
            response = client.complete(request)
        except LLMError as exc:
            meta.calls.append(LLMCallRecord(attempt=attempt, error_code=exc.code))
            return degrade("system_side", exc.code, exc.message)
        _record_call(meta, attempt, response)
        outcome.events.append(_event(
            "llm_call", Outcome.INFO,
            f"LLM call {attempt}: {response.usage.total_input} tokens in, {response.usage.output_tokens} out, "
            f"${response.cost_usd if response.cost_usd is not None else 0:.6f}, {response.latency_ms} ms.",
            {"attempt": attempt, "model": response.model, "tokens_in": response.usage.total_input,
             "tokens_out": response.usage.output_tokens, "cost_usd": response.cost_usd, "latency_ms": response.latency_ms,
             "stop_reason": response.stop_reason, "request_id": response.request_id, "thinking_mode": response.thinking_mode,
             "effort": response.effort, "param_fallback": response.param_fallback}))
        outcome.raw_replies.append(response.text)
        try:
            response.ensure_usable()
            contract, wire_notes = from_wire(parse_reply(response.text, expect_key="fields"))       # wire format -> internal contract
            result = postprocess(contract, settings, wire_notes)
        except LLMRefusedError as exc:
            return degrade("system_side", exc.code, exc.message)
        except LLMTruncatedError as exc:
            last_error = "the reply was cut off before the JSON was complete"
            meta.calls[-1].error_code = exc.code
        except (ValidationError, ValueError) as exc:           # JSONDecodeError is a ValueError
            last_error = _summarise(exc)
            meta.calls[-1].error_code = "schema_invalid"
        else:
            meta.schema_repair_used = attempt > 1
            outcome.invoice = result.invoice
            try:
                _check_against_document(outcome, ingest, settings)
            except Exception as exc:                            # a bug here must not pass unchecked values as confident
                logger.exception("grounding failed run=%s", run_id)
                return degrade("system_side", "grounding_error", f"The grounding check failed ({type(exc).__name__}).")
            outcome.events.append(_event(
                "extraction_complete", Outcome.PASS,
                f"Extraction complete via {path} in {attempt} attempt(s); cost ${meta.cost_usd:.6f}.",
                {"path": path, "attempts": attempt, "schema_repair_used": meta.schema_repair_used,
                 "tokens_in": meta.tokens_in, "tokens_out": meta.tokens_out, "cost_usd": meta.cost_usd,
                 "system_notes": result.notes}))
            return outcome
        if attempt < attempts_allowed:
            outcome.events.append(_event("schema_repair", Outcome.FLAG,
                                         f"The reply was not acceptable ({last_error}); retrying once with a correction.",
                                         {"attempt": attempt, "error": last_error}))
    return degrade("system_side", "schema_invalid",
                   f"The model's reply was still not valid after {attempts_allowed} attempt(s): {last_error}")


def read_page_texts(ingest: IngestInfo) -> dict[int, str | None]:
    """page number -> stored text layer (None = no text, or the file could not be read)."""
    texts: dict[int, str | None] = {}
    for index, page in enumerate(ingest.pages):
        path = ingest.text_paths[index] if index < len(ingest.text_paths) else None
        try:
            texts[page.number] = Path(path).read_text(encoding="utf-8") if path else None
        except OSError:
            texts[page.number] = None
    return texts


def _check_against_document(outcome: ExtractionOutcome, ingest: IngestInfo, settings: Settings) -> None:
    """Grounding (caps confidences the document does not support) and the reader-instruction scan."""
    invoice, meta = outcome.invoice, outcome.meta
    texts = read_page_texts(ingest)
    grounded = ground_invoice(invoice, texts, ingest.text_layer.usable, settings)
    meta.grounding = dict(grounded.counts)
    problems = [n for n in grounded.notes[1:]]
    outcome.events.append(_event(
        "grounding", Outcome.FLAG if problems else Outcome.INFO,
        grounded.notes[0] if grounded.notes else "grounding: nothing to check.",
        {"counts": grounded.counts, "text_layer_usable": ingest.text_layer.usable, "problems": problems}))

    evidence: list[str] = []
    if invoice.document_quality.contains_reader_instructions is True:
        evidence.append("the model reported text addressed to the reader")
    evidence.extend(f"page {hit.page}: ...{hit.snippet}..." for hit in scan_pages(
        {n: t for n, t in texts.items() if t}, settings.injection_patterns))
    if evidence:
        meta.injection_suspected, meta.injection_evidence = True, evidence[:10]
        note = "[system] The document appears to contain text addressed to an AI reader; treated as data and sent to review."
        invoice.extraction_notes = f"{invoice.extraction_notes}\n{note}" if invoice.extraction_notes else note
        outcome.events.append(_event("reader_instructions_detected", Outcome.FLAG,
                                     "The document contains text addressed to an AI reader.", {"evidence": meta.injection_evidence}))


def _record_call(meta: ExtractionMeta, attempt: int, response: LLMResponse) -> None:
    cost = response.cost_usd if response.cost_usd is not None else Decimal(0)
    meta.calls.append(LLMCallRecord(
        attempt=attempt, tokens_in=response.usage.total_input, tokens_out=response.usage.output_tokens, cost_usd=cost,
        latency_ms=response.latency_ms, stop_reason=response.stop_reason, request_id=response.request_id))
    meta.tokens_in += response.usage.total_input
    meta.tokens_out += response.usage.output_tokens
    meta.cost_usd += cost
    meta.latency_ms += response.latency_ms
    meta.thinking_mode, meta.effort = response.thinking_mode, response.effort      # None for replayed responses
    meta.param_fallback = response.param_fallback or meta.param_fallback
