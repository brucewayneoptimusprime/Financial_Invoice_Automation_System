"""The PO drafter: typed text or one document -> a DRAFT of the PO's fields, each with evidence and confidence.

Same conventions as invoice extraction: the server's one metered client and cost tracker (the draft id is the per-run key, so
the per-run ceiling applies per draft), thinking disabled + effort low (the client's defaults), a strict union-free schema (or the
schema as prompt text when `llm_structured_output = prompt_json`), one repair retry, grounding against the input text (which
only lowers confidence), the reader-instruction scan. A failure never raises: the draft comes back `failed` with a system-side
code and the person can still use the empty form. Nothing here writes to the database.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.extraction.extractor import parse_reply
from app.extraction.grounding import ground_item
from app.extraction.injection import scan_text
from app.extraction.parsing import is_ambiguous_dmy, map_currency, normalize_amount_string, normalize_date_string
from app.extraction.prompts import PROMPT_JSON_SUFFIX, repair_part
from app.llm.errors import LLMError, LLMRefusedError, LLMTruncatedError
from app.llm.types import LLMClient, LLMPart, LLMRequest
from app.po.prompts import PO_PROMPT_VERSION, PO_SYSTEM_PROMPT
from app.po.wire import PO_FIELDS, from_po_wire, po_wire_schema

logger = logging.getLogger("app.po.drafter")

_KIND = {"vendor_name": "string", "vendor_tax_id": "string", "po_number": "string", "issued_date": "date", "currency": "currency",
         "total": "amount"}


@dataclass
class POModelDraft:
    status: str = "ok"                              # ok | failed
    failure_code: str | None = None
    failure_message: str | None = None
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)      # name -> value, page, source_text, confidence, model_confidence, grounding
    lines: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    other_pos_present: bool = False
    injection_suspected: bool = False
    injection_evidence: list[str] = field(default_factory=list)
    model: str | None = None
    prompt_version: str = PO_PROMPT_VERSION
    attempts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)


def _system(settings: Settings) -> tuple[str, dict | None]:
    if settings.llm_structured_output == "prompt_json":
        return PO_SYSTEM_PROMPT + PROMPT_JSON_SUFFIX.format(schema=json.dumps(po_wire_schema(), sort_keys=True)), None
    return PO_SYSTEM_PROMPT, po_wire_schema()


def _money(value: str | None, label: str, notes: list[str]) -> str | None:
    if value is None:
        return None
    text = normalize_amount_string(value)
    if text is None:
        notes.append(f"[system] {label} {value!r} is not an amount and was left empty")
        return None
    return text


def _postprocess(content: dict[str, Any], settings: Settings, notes: list[str]) -> None:
    f = content["fields"]
    for name in ("total",):
        f[name]["value"] = _money(f[name]["value"], name, notes)
    if f["issued_date"]["value"] is not None:
        iso = normalize_date_string(f["issued_date"]["value"])
        if iso is None:
            notes.append(f"[system] issue date {f['issued_date']['value']!r} could not be read as one date and was left empty")
            f["issued_date"]["value"] = None
        else:
            f["issued_date"]["value"] = iso
            src = f["issued_date"]["source_text"] or ""
            if is_ambiguous_dmy(src) and f["issued_date"]["confidence"] > 0.5:
                f["issued_date"]["confidence"] = 0.5
                notes.append("[system] the issue date's day/month order is ambiguous; confidence capped at 0.5")
    cur = f["currency"]
    if cur["value"] is not None:
        code, note = map_currency(cur["value"], settings.currency_symbol_map)
        if note:
            notes.append(f"[system] {note}")
        if code is None:
            cur["value"] = None
        else:
            if note and cur["model_confidence"] > 0:                   # mapped from a symbol: a configured confidence (SPEC item 46)
                cur["confidence"] = settings.currency_symbol_confidence
            cur["value"] = code
    for i, line in enumerate(content["lines"], start=1):
        for k in ("quantity", "unit_price", "amount"):
            line[k] = _money(line[k], f"line {i} {k.replace('_', ' ')}", notes)
    for name in PO_FIELDS:
        if f[name]["value"] is None:
            f[name].update(page=None, source_text=None, confidence=0.0)


def _typed(kind: str, value: str) -> Any:
    try:
        if kind == "amount":
            return Decimal(value)
        if kind == "date":
            return date.fromisoformat(value)
    except (InvalidOperation, ValueError):
        return None
    return value


def _ground(content: dict[str, Any], page_texts: dict[int, str | None], usable: bool, settings: Settings, notes: list[str]) -> None:
    for name, item in content["fields"].items():
        item["grounding"] = None
        if item["value"] is None:
            continue
        value = _typed(_KIND[name], item["value"])
        status, page, cap = ground_item(_KIND[name], value, item["source_text"], item["page"], page_texts, usable, settings)
        item["grounding"] = status.value
        if page is not None and item["page"] != page:
            item["page"] = page
        if cap is not None and item["confidence"] > cap:
            notes.append(f"[system] {name}: evidence {status.value}, confidence {item['confidence']:g} -> {cap:g}")
            item["confidence"] = cap
    for i, line in enumerate(content["lines"], start=1):
        numbers = [Decimal(v) for v in (line["unit_price"], line["amount"]) if v is not None]
        qty = Decimal(line["quantity"]) if line["quantity"] is not None else None
        status, page, cap = ground_item("line", (numbers, qty, line["description"]), line["source_text"], line["page"], page_texts,
                                        usable, settings)
        line["grounding"] = status.value
        if page is not None:
            line["page"] = page
        if cap is not None and line["confidence"] > cap:
            notes.append(f"[system] line {i}: evidence {status.value}, confidence {line['confidence']:g} -> {cap:g}")
            line["confidence"] = cap


def draft_po(parts: tuple[LLMPart, ...], page_texts: dict[int, str | None], text_usable: bool, *, client: LLMClient,
             settings: Settings, draft_id: str) -> POModelDraft:
    draft = POModelDraft(model=settings.model_name)
    system, schema = _system(settings)
    attempts_allowed = 1 + settings.schema_repair_retries
    last_error = ""

    def fail(code: str, message: str) -> POModelDraft:
        draft.status, draft.failure_code, draft.failure_message = "failed", code, message
        logger.warning("PO draft failed draft=%s code=%s", draft_id, code)
        return draft

    for attempt in range(1, attempts_allowed + 1):
        request = LLMRequest(system=system, parts=parts + ((repair_part(last_error),) if attempt > 1 else ()), model=settings.model_name,
                             max_output_tokens=settings.po_max_output_tokens, schema=schema, run_id=draft_id, purpose="po_draft",
                             cache_system=settings.llm_cache_system_prompt)
        draft.attempts = attempt
        try:
            response = client.complete(request)
        except LLMError as exc:
            return fail(exc.code, exc.message)
        draft.tokens_in += response.usage.total_input
        draft.tokens_out += response.usage.output_tokens
        draft.cost_usd += response.cost_usd or Decimal(0)
        draft.model = response.model or draft.model
        try:
            response.ensure_usable()
            content, notes = from_po_wire(parse_reply(response.text, expect_key="fields"))
        except LLMRefusedError as exc:
            return fail(exc.code, exc.message)
        except LLMTruncatedError:
            last_error = "the reply was cut off before the JSON was complete"
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:300]
        else:
            try:
                _postprocess(content, settings, notes)
                _ground(content, page_texts, text_usable, settings, notes)
            except Exception as exc:                                     # never pass unchecked values as confident
                logger.exception("PO draft checks failed draft=%s", draft_id)
                return fail("grounding_error", f"The evidence check failed ({type(exc).__name__}).")
            draft.fields, draft.lines = content["fields"], content["lines"]
            draft.notes = ([content["notes"]] if content["notes"] else []) + notes
            draft.other_pos_present = content["other_pos_present"] is True
            evidence = ["the model reported text addressed to the reader"] if content["reader_instructions"] is True else []
            for n, t in page_texts.items():
                if t:
                    evidence += [f"page {h.page or n}: ...{h.snippet}..." for h in scan_text(t, settings.injection_patterns, page=n)]
            draft.injection_suspected, draft.injection_evidence = bool(evidence), evidence[:10]
            return draft
    return fail("schema_invalid", f"The model's reply was still not valid after {attempts_allowed} attempt(s): {last_error}")
