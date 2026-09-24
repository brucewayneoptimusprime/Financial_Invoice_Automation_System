"""Deterministic scan for text addressed to an AI reader ("ignore previous instructions", "approve this invoice"...).

The document is DATA. Even a successful injection cannot approve anything (the rules decide), but a document that
talks to the reader is suspicious in itself, so a hit sends the run to review through the engine floor
(`reader_instructions_detected`). The patterns are config (`injection_patterns`); this module only applies them.
"""
import re
import unicodedata
from dataclasses import dataclass

_WHITESPACE = re.compile(r"\s+")
_SNIPPET_RADIUS = 40


@dataclass(frozen=True)
class InjectionHit:
    page: int | None
    pattern: str
    snippet: str          # the matched text with a little context, for the audit trail


def _flatten(text: str) -> str:
    """NFKC (folds full-width letters and ligatures) and collapsed whitespace, so a payload split over lines still matches."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text))


def scan_text(text: str, patterns: tuple[str, ...], page: int | None = None) -> list[InjectionHit]:
    flat = _flatten(text)
    hits: list[InjectionHit] = []
    for pattern in patterns:
        match = re.search(pattern, flat, re.IGNORECASE)
        if match:
            start, end = max(0, match.start() - _SNIPPET_RADIUS), min(len(flat), match.end() + _SNIPPET_RADIUS)
            hits.append(InjectionHit(page=page, pattern=pattern, snippet=flat[start:end].strip()))
    return hits


def scan_pages(page_texts: dict[int, str], patterns: tuple[str, ...]) -> list[InjectionHit]:
    hits: list[InjectionHit] = []
    for number in sorted(page_texts):
        hits.extend(scan_text(page_texts[number], patterns, page=number))
    return hits
