"""Pure text helpers shared by the evaluators and matchers. Deterministic, stdlib only."""
import difflib
import re
import unicodedata
from typing import Any, Iterable

_SEPARATORS = re.compile(r"[\W_]+", re.UNICODE)
_LEADING_ZEROS = re.compile(r"(?<![0-9])0+(?=[0-9])")


def is_missing(value: Any) -> bool:
    """Null (or blank text) is missing, whatever the confidence attached to it. Zero is NOT missing."""
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def field_present(field: Any) -> bool:
    return field is not None and not is_missing(getattr(field, "value", None))


def normalize_text(text: str) -> str:
    """Casefold, strip accents, collapse every run of punctuation/whitespace to one space."""
    decomposed = unicodedata.normalize("NFKD", text).casefold()
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _SEPARATORS.sub(" ", stripped).strip()


def normalize_name(text: str, drop_tokens: Iterable[str] = ()) -> str:
    """normalize_text with generic filler tokens (legal suffixes) removed. Never returns empty
    if the input had any content: falls back to the un-dropped text."""
    drop = {t.casefold() for t in drop_tokens}
    full = normalize_text(text)
    kept = [t for t in full.split() if t not in drop]
    return " ".join(kept) if kept else full


def normalize_identifier(text: str) -> str:
    """Invoice numbers / PO references: 'INV-0001', 'inv 1' and 'Inv/1' all become 'inv1'."""
    compact = normalize_text(text).replace(" ", "")
    return _LEADING_ZEROS.sub("", compact)


_TAX_ID_SEPARATORS = re.compile(r"[\s\-.]+")


def normalize_tax_id(text: str) -> str:
    """Tax IDs are compared ignoring case, spaces, hyphens and dots ('gb 123-456.789' == 'GB123456789')."""
    return _TAX_ID_SEPARATORS.sub("", text).upper()


def similarity(a: str, b: str) -> float:
    """0..1 edit-based similarity of two already-normalised strings (0.0 if either is empty)."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def token_similarity(a: str, b: str) -> float:
    """max(edit similarity, token-set Jaccard) of two already-normalised strings."""
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb)
    return max(similarity(a, b), jaccard)
