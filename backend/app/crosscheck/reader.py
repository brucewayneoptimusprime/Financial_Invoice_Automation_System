"""The document reader: one supporting document -> the facts printed on it, each with page, source text and a grounding status.

Same conventions as the PO drafter: the server's one metered client (the run key makes the per-run ceiling apply per document),
a strict union-free schema (or the schema as prompt text when `llm_structured_output = prompt_json`), one repair retry, the
existing amount / date / currency normalising, the existing grounding check and reader-instruction scan. A failure never raises:
the result comes back `failed` with a code and a message. Nothing here reads or writes the database.

The code check on the model's output:
  * only the schema's keys are read (app/crosscheck/wire.py);
  * a value that does not parse (an amount, a quantity, a date, a currency) is dropped and noted;
  * every value is grounded against the document's own text. `value_mismatch`, `not_found` and `no_source` mark it NOT confirmed,
    and the comparison ignores it. `unavailable` (a scan with no text layer) stays usable and is marked as read from the image.
"""
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.crosscheck.prompts import CROSSCHECK_PROMPT_VERSION, system_and_schema
from app.crosscheck.wire import FIELDS, from_crosscheck_wire
from app.enums import GroundingStatus as G
from app.extraction.extractor import parse_reply
from app.extraction.grounding import ground_item
from app.extraction.injection import scan_text
from app.extraction.parsing import map_currency, normalize_amount_string, normalize_date_string
from app.extraction.prompts import repair_part
from app.llm.errors import LLMError, LLMRefusedError, LLMTruncatedError
from app.llm.types import LLMClient, LLMPart, LLMRequest

logger = logging.getLogger("app.crosscheck")

PURPOSE = "crosscheck"
UNCONFIRMED = frozenset({G.VALUE_MISMATCH.value, G.NOT_FOUND.value, G.NO_SOURCE.value})
_FIELD_KIND = {"document_type": "string", "vendor_name": "string", "currency": "currency", "total": "amount"}
_MENTION_KIND = {"po_number": "string", "invoice_number": "string", "date": "date"}
_LINE_NUMBERS = ("quantity", "unit_price", "amount")


@dataclass
class DocumentFacts:
    status: str = "ok"                              # ok | failed
    failure_code: str | None = None
    failure_message: str | None = None
    document_kind: str = "unknown"
    fields: dict[str, dict[str, Any] | None] = field(default_factory=lambda: {n: None for n in FIELDS})
    mentions: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    model_notes: str = ""                           # the model's free text, shown as the model's words, never acted on
    notes: list[str] = field(default_factory=list)  # ours: dropped values, ignored keys
    injection_suspected: bool = False
    injection_evidence: list[str] = field(default_factory=list)
    model: str | None = None
    prompt_version: str = CROSSCHECK_PROMPT_VERSION
    attempts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)


def build_request(parts: tuple[LLMPart, ...], settings: Settings, run_key: str) -> LLMRequest:
    system, schema = system_and_schema(settings.llm_structured_output)
    return LLMRequest(system=system, parts=parts, model=settings.model_name, max_output_tokens=settings.crosscheck_max_output_tokens,
                      schema=schema, run_id=run_key, purpose=PURPOSE, cache_system=settings.llm_cache_system_prompt)


def _number(value: str | None, label: str, notes: list[str]) -> str | None:
    if value is None:
        return None
    text = normalize_amount_string(value)
    if text is None:
        notes.append(f"{label} {value[:40]!r} is not a number and was left out")
    return text


def _postprocess(content: dict[str, Any], settings: Settings, notes: list[str]) -> None:
    f = content["fields"]
    if f["total"] is not None:
        f["total"]["value"] = _number(f["total"]["value"], "the total", notes)
        if f["total"]["value"] is None:
            f["total"] = None
    if f["currency"] is not None:
        code, note = map_currency(f["currency"]["value"], settings.currency_symbol_map)
        if note:
            notes.append(note)
        if code is None:
            f["currency"] = None
        else:
            f["currency"]["value"] = code
    kept = []
    for m in content["mentions"]:
        if m["kind"] == "date":
            iso = normalize_date_string(m["value"])
            if iso is None:
                notes.append(f"the date {m['value'][:40]!r} could not be read as one date and was left out")
                continue
            m["value"] = iso
        kept.append(m)
    content["mentions"] = kept
    for i, line in enumerate(content["lines"], start=1):
        for k in _LINE_NUMBERS:
            line[k] = _number(line[k], f"line {i} {k.replace('_', ' ')}", notes)


def _typed(kind: str, value: str) -> Any:
    if kind == "amount":
        return Decimal(value)
    if kind == "date":
        return date.fromisoformat(value)
    return value


def _mark(item: dict[str, Any], status: G, page: int | None) -> None:
    item["grounding"] = status.value
    item["confirmed"] = status.value not in UNCONFIRMED
    if page is not None:
        item["page"] = page


def _ground(content: dict[str, Any], page_texts: dict[int, str | None], usable: bool, settings: Settings) -> None:
    for name, item in content["fields"].items():
        if item is not None:
            status, page, _ = ground_item(_FIELD_KIND[name], _typed(_FIELD_KIND[name], item["value"]), item["source_text"],
                                          item["page"], page_texts, usable, settings)
            _mark(item, status, page)
    for m in content["mentions"]:
        kind = _MENTION_KIND[m["kind"]]
        status, page, _ = ground_item(kind, _typed(kind, m["value"]), m["source_text"], m["page"], page_texts, usable, settings)
        _mark(m, status, page)
    for line in content["lines"]:
        numbers = [Decimal(v) for v in (line["unit_price"], line["amount"]) if v is not None]
        qty = Decimal(line["quantity"]) if line["quantity"] is not None else None
        status, page, _ = ground_item("line", (numbers, qty, line["description"]), line["source_text"], line["page"], page_texts,
                                      usable, settings)
        _mark(line, status, page)


def read_document(parts: tuple[LLMPart, ...], page_texts: dict[int, str | None], text_usable: bool, *, client: LLMClient,
                  settings: Settings, run_key: str) -> DocumentFacts:
    facts = DocumentFacts(model=settings.model_name)
    attempts_allowed = 1 + settings.schema_repair_retries
    last_error = ""

    def fail(code: str, message: str) -> DocumentFacts:
        facts.status, facts.failure_code, facts.failure_message = "failed", code, message
        logger.warning("cross-check read failed key=%s code=%s", run_key, code)
        return facts

    for attempt in range(1, attempts_allowed + 1):
        request = build_request(parts + ((repair_part(last_error),) if attempt > 1 else ()), settings, run_key)
        facts.attempts = attempt
        try:
            response = client.complete(request)
        except LLMError as exc:
            return fail(exc.code, exc.message)
        facts.tokens_in += response.usage.total_input
        facts.tokens_out += response.usage.output_tokens
        facts.cost_usd += response.cost_usd or Decimal(0)
        facts.model = response.model or facts.model
        try:
            response.ensure_usable()
            content, notes = from_crosscheck_wire(parse_reply(response.text, expect_key="fields"))
        except LLMRefusedError as exc:
            return fail(exc.code, exc.message)
        except LLMTruncatedError:
            last_error = "the reply was cut off before the JSON was complete"
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:300]
        else:
            try:
                _postprocess(content, settings, notes)
                _ground(content, page_texts, text_usable, settings)
            except Exception as exc:                                     # never pass unchecked values on as if they were checked
                logger.exception("cross-check checks failed key=%s", run_key)
                return fail("grounding_error", f"The evidence check failed ({type(exc).__name__}).")
            facts.document_kind, facts.fields = content["document_kind"], content["fields"]
            facts.mentions, facts.lines = content["mentions"], content["lines"]
            facts.model_notes, facts.notes = content["notes"], notes
            evidence = ["the model reported text addressed to the reader"] if content["reader_instructions"] is True else []
            for n, t in page_texts.items():
                if t:
                    evidence += [f"page {h.page or n}: ...{h.snippet}..." for h in scan_text(t, settings.injection_patterns, page=n)]
            facts.injection_suspected, facts.injection_evidence = bool(evidence), evidence[:10]
            return facts
    return fail("schema_invalid", f"The model's reply was still not valid after {attempts_allowed} attempt(s): {last_error}")
