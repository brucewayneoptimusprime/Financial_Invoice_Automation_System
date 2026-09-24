"""Grounding: is each extracted value supported by the document's own text? Deterministic, no LLM.

The model's confidence is one signal, not truth. For every non-null evidenced field, line item and adjustment:

  G0  no source_text                                   -> no_source       cap no_source (0.50)
  G1  value disagrees with its OWN source_text         -> value_mismatch  cap value_mismatch (0.30)
  G2  source_text found on the page                    -> exact / normalized (no cap) / fuzzy (cap 0.75)
  G2b snippet NOT found but the VALUE is on the page   -> value_present   cap value_present (0.85)
      (real PDFs often list all the values in one block and all the labels in another, so "Subtotal: $5,141.76" is
      not a substring of the text layer although the number is there)
  --  neither snippet nor value on the page            -> not_found       cap not_found (0.40)
  G3  snippet/value found on a different page          -> the page number is corrected and noted
  --  no usable text layer for that page               -> unavailable     no cap (G0 and G1 still apply)

effective = min(current confidence, cap). Grounding NEVER raises a confidence. The model's own score stays in
`model_confidence`. Caps are config (`Settings.grounding`).
"""
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from app.config import Settings, get_settings
from app.enums import GroundingStatus as G
from app.extraction.parsing import date_candidates, find_amounts
from app.models.extraction import ExtractedAdjustment, ExtractedInvoice, ExtractedLineItem

_KINDS = {"vendor_name": "string", "vendor_tax_id": "string", "vendor_address": "string", "invoice_number": "string",
          "po_reference": "string", "document_type": "doctype", "invoice_date": "date", "currency": "currency",
          "subtotal": "amount", "tax": "amount", "total": "amount"}
_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—―−﹣－"})
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"'})
_WS = re.compile(r"\s+")
_HYPHEN_BREAK = re.compile(r"(?<=\w)-\s*\n\s*(?=\w)")
_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)
_FUZZY_MIN_LEN = 8                       # shorter snippets must match exactly: fuzzy matching on 5 characters proves nothing


@dataclass
class GroundingResult:
    counts: dict[str, int] = field(default_factory=dict)      # status -> number of items
    notes: list[str] = field(default_factory=list)            # one summary line plus a line per problem
    checked: int = 0


# ------------------------------------------------------------------------------------------- normalisation

def normalize_text(text: str) -> str:
    """Case, whitespace, ligatures (NFKC), Unicode minus/dash/quote variants, soft hyphens and hyphenated line breaks."""
    text = unicodedata.normalize("NFKC", text).replace("­", "")
    text = _HYPHEN_BREAK.sub("", text).translate(_DASHES).translate(_QUOTES)
    return _WS.sub(" ", text).strip().casefold()


def alnum(text: str) -> str:
    """Letters and digits only, casefolded: 'GB-123 456' and 'gb123456' compare equal."""
    return _NON_ALNUM.sub("", unicodedata.normalize("NFKC", text)).casefold()


def _fuzzy_similarity(snippet: str, text: str) -> float:
    """Best similarity of `snippet` to any window of `text` (both already normalised)."""
    n = len(snippet)
    if n == 0 or not text:
        return 0.0
    if len(text) <= n:
        return SequenceMatcher(None, snippet, text, autojunk=False).ratio()
    best, step = 0.0, max(1, n // 6)
    for start in range(0, len(text) - n + step, step):
        matcher = SequenceMatcher(None, snippet, text[start:start + n + 2], autojunk=False)
        if matcher.real_quick_ratio() < best or matcher.quick_ratio() < best:
            continue
        best = max(best, matcher.ratio())
        if best >= 0.999:
            break
    return best


# ---------------------------------------------------------------------------------------------- per-page facts

class _Pages:
    """The text layer, page by page, with lazily computed normalised text, numbers and dates."""

    def __init__(self, texts: dict[int, str | None], usable: bool):
        self.known = set(texts)                                # pages that exist (with or without a text layer)
        self.texts = {n: t for n, t in texts.items() if t and t.strip()} if usable else {}
        self._norm: dict[int, str] = {}
        self._amounts: dict[int, set[Decimal]] = {}
        self._dates: dict[int, set[date]] = {}
        self._alnum: dict[int, str] = {}

    def order(self, claimed: int | None) -> list[int] | None:
        """Pages to search, claimed page first; None = this item cannot be checked (its page has no text layer)."""
        if not self.texts:
            return None
        if claimed is not None and claimed in self.texts:
            return [claimed] + [n for n in sorted(self.texts) if n != claimed]
        if claimed is not None and claimed in self.known:
            return None                                        # a page that exists but has no text (a scanned page)
        return sorted(self.texts)

    def norm(self, n: int) -> str:
        return self._norm.setdefault(n, normalize_text(self.texts[n]))

    def amounts(self, n: int) -> set[Decimal]:
        if n not in self._amounts:
            self._amounts[n] = find_amounts(self.texts[n])
        return self._amounts[n]

    def dates(self, n: int) -> set[date]:
        if n not in self._dates:
            self._dates[n] = date_candidates(self.texts[n])
        return self._dates[n]

    def alnum(self, n: int) -> str:
        return self._alnum.setdefault(n, alnum(self.texts[n]))


# ----------------------------------------------------------------------------------------------- the checks

def _amount_in(value: Decimal, readings: set[Decimal]) -> bool:
    return abs(value) in readings


def _snippet_status(snippet: str, pages: _Pages, order: list[int], min_similarity: float) -> tuple[G, int] | None:
    """(status, page) of the best place `snippet` occurs, or None. Exact beats normalised beats fuzzy, on any page."""
    stripped = snippet.strip()
    for n in order:
        if stripped and stripped in pages.texts[n]:
            return G.EXACT, n
    target = normalize_text(snippet)
    for n in order:
        if target and target in pages.norm(n):
            return G.NORMALIZED, n
    if len(target) >= _FUZZY_MIN_LEN:
        best: tuple[float, int] | None = None
        for n in order:
            similarity = _fuzzy_similarity(target, pages.norm(n))
            if similarity >= min_similarity and (best is None or similarity > best[0]):
                best = (similarity, n)
        if best:
            return G.FUZZY, best[1]
    return None


def _currency_symbols(code: str, settings: Settings) -> list[str]:
    return [symbol for symbol, mapped in settings.currency_symbol_map.items() if mapped == code]


def _value_agrees_with_source(kind: str, value: Any, source: str, settings: Settings) -> bool:
    """G1: does the value follow from the item's own snippet?"""
    if kind == "amount":
        return _amount_in(value, find_amounts(source))
    if kind == "date":
        return value in date_candidates(source)
    if kind == "currency":
        if re.search(rf"(?<![A-Za-z]){re.escape(value)}(?![A-Za-z])", source, re.IGNORECASE):
            return True
        return any(symbol in source for symbol in _currency_symbols(value, settings))
    if kind == "doctype":
        return True                                            # a classification, not text copied from the page
    a, b = alnum(str(value)), alnum(source)
    return bool(a and b and (a in b or b in a))


def _value_on_page(kind: str, value: Any, pages: _Pages, n: int, settings: Settings) -> bool:
    """G2b: is the VALUE itself on page n?"""
    if kind == "amount":
        return _amount_in(value, pages.amounts(n))
    if kind == "date":
        return value in pages.dates(n)
    if kind == "currency":
        text = pages.texts[n]
        if re.search(rf"(?<![A-Za-z]){re.escape(value)}(?![A-Za-z])", text, re.IGNORECASE):
            return True
        return any(symbol in text for symbol in _currency_symbols(value, settings))
    needle = alnum(str(value))
    return bool(needle) and needle in pages.alnum(n)


def _cap_for(status: G, settings: Settings) -> float | None:
    caps = settings.grounding
    return {G.NO_SOURCE: caps.no_source, G.VALUE_MISMATCH: caps.value_mismatch, G.FUZZY: caps.fuzzy,
            G.VALUE_PRESENT: caps.value_present, G.NOT_FOUND: caps.not_found}.get(status)


# ------------------------------------------------------------------------------------- a uniform view of items

@dataclass
class _Claim:
    """What is checked about one field / line / adjustment."""
    label: str
    item: Any                                # EvidencedField, ExtractedLineItem or ExtractedAdjustment
    kind: str                                # string | doctype | date | currency | amount | line | adjustment
    value: Any = None                        # the value G1 and G2b look for (line/adjustment: a list of Decimals)
    checkable: bool = True


def _claims(invoice: ExtractedInvoice) -> list[_Claim]:
    claims: list[_Claim] = []
    for name, kind in _KINDS.items():
        f = getattr(invoice, name)
        if f.value is not None:
            claims.append(_Claim(name, f, kind, f.value))
    for i, line in enumerate(invoice.line_items, start=1):
        numbers = [v for v in (line.unit_price, line.amount) if v is not None]
        claims.append(_Claim(f"line_items[{i}]", line, "line", (numbers, line.quantity, line.description)))
    for i, adj in enumerate(invoice.adjustments, start=1):
        magnitude = adj.printed_amount if adj.printed_amount is not None else adj.amount
        claims.append(_Claim(f"adjustments[{i}]", adj, "adjustment", magnitude))
    return claims


def _line_agrees(value: tuple, source: str) -> bool:
    numbers, quantity, _ = value
    readings = find_amounts(source)
    return all(_amount_in(n, readings) for n in [*numbers, *([quantity] if quantity is not None else [])])


def _line_on_page(value: tuple, pages: _Pages, n: int) -> bool:
    numbers, _, description = value
    if numbers:                                               # quantity is too common a number to prove anything
        return all(_amount_in(x, pages.amounts(n)) for x in numbers)
    return bool(description) and bool(alnum(description)) and alnum(description) in pages.alnum(n)


def _adjustment_agrees(value: Decimal | None, source: str) -> bool:
    return value is None or _amount_in(value, find_amounts(source))


def _adjustment_on_page(value: Decimal | None, pages: _Pages, n: int) -> bool:
    return value is not None and _amount_in(value, pages.amounts(n))


def _agrees(claim: _Claim, source: str, settings: Settings) -> bool:
    if claim.kind == "line":
        return _line_agrees(claim.value, source)
    if claim.kind == "adjustment":
        return _adjustment_agrees(claim.value, source)
    return _value_agrees_with_source(claim.kind, claim.value, source, settings)


def _on_page(claim: _Claim, pages: _Pages, n: int, settings: Settings) -> bool:
    if claim.kind == "line":
        return _line_on_page(claim.value, pages, n)
    if claim.kind == "adjustment":
        return _adjustment_on_page(claim.value, pages, n)
    return _value_on_page(claim.kind, claim.value, pages, n, settings)


# ------------------------------------------------------------------------------------------------- the driver

def _classify(claim: _Claim, pages: _Pages, settings: Settings) -> tuple[G, int | None]:
    """(status, page where the evidence was found or None)."""
    item = claim.item
    source = (item.source_text or "").strip()
    if not source:
        return G.NO_SOURCE, None
    if not _agrees(claim, source, settings):
        return G.VALUE_MISMATCH, None
    order = pages.order(item.page)
    if order is None:
        return G.UNAVAILABLE, None
    found = _snippet_status(source, pages, order, settings.grounding.fuzzy_min_similarity)
    if found and found[0] is not G.FUZZY:
        return found
    if found:                                                  # an approximate snippet whose VALUE is on the page is value_present
        if _on_page(claim, pages, found[1], settings):
            return G.VALUE_PRESENT, found[1]
        return found
    for n in order:
        if _on_page(claim, pages, n, settings):
            return G.VALUE_PRESENT, n
    return G.NOT_FOUND, None


def _describe(claim: _Claim, status: G, before: float, after: float, page: int | None) -> str | None:
    """A note for problems worth a reader's attention (not for exact / normalized / value_present / unavailable)."""
    if status == G.NO_SOURCE:
        return f"grounding: {claim.label} has a value but no source_text (confidence {before:g} -> {after:g})"
    if status == G.VALUE_MISMATCH:
        return f"grounding: {claim.label} does not agree with its own source_text (confidence {before:g} -> {after:g})"
    if status == G.NOT_FOUND:
        return (f"grounding: neither the source_text nor the value of {claim.label} was found in the text layer "
                f"(confidence {before:g} -> {after:g})")
    if status == G.FUZZY:
        return (f"grounding: {claim.label} source_text matches the text layer only approximately and its value was not "
                f"found there (confidence {before:g} -> {after:g})")
    return None


def ground_invoice(invoice: ExtractedInvoice, page_texts: dict[int, str | None], text_usable: bool = True,
                   settings: Settings | None = None) -> GroundingResult:
    """Check every non-null field, line item and adjustment against the text layer and cap its confidence.
    Mutates `invoice` (grounding, confidence, page repairs, a summary appended to extraction_notes)."""
    settings = settings or get_settings()
    pages = _Pages(page_texts, text_usable)
    result = GroundingResult()
    for claim in _claims(invoice):
        item = claim.item
        if item.model_confidence is None:
            item.model_confidence = item.confidence
        status, found_page = _classify(claim, pages, settings)
        before = item.confidence
        cap = _cap_for(status, settings)
        if cap is not None:
            item.confidence = min(item.confidence, cap)         # never raised
        item.grounding = status
        result.counts[status.value] = result.counts.get(status.value, 0) + 1
        result.checked += 1
        if found_page is not None and item.page != found_page:
            if item.page is not None:
                result.notes.append(f"grounding: {claim.label} was on page {found_page}, not page {item.page}; page corrected")
            item.page = found_page
        if (note := _describe(claim, status, before, item.confidence, found_page)):
            result.notes.append(note)
    if result.counts:
        summary = ", ".join(f"{count} {name}" for name, count in sorted(result.counts.items(), key=lambda kv: (-kv[1], kv[0])))
        result.notes.insert(0, f"grounding: {result.checked} item(s) checked: {summary}")
        system = "\n".join(f"[system] {n}" for n in result.notes)
        invoice.extraction_notes = f"{invoice.extraction_notes}\n{system}" if invoice.extraction_notes else system
    return result
