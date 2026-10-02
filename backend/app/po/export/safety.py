"""Safety helpers for exported files: spreadsheet formula injection, file names, caps."""
import re
from urllib.parse import quote

MAX_SUMMARY_POS = 1000              # per summary export (owner decision 4)
MAX_SECTION_ROWS = 5000             # guard for ledger / allocation sections of one PO
TYPED_TEXT_CAP = 4000               # the typed text in "Where this PO came from" (owner decision 6)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_cell(text: str | None) -> str:
    """A user-derived text for a CSV / Excel cell: a leading = + - @ tab or carriage return gets a ' so no spreadsheet treats it as
    a formula. Money and quantities are numbers we produce and never go through here."""
    if text is None:
        return ""
    text = str(text)
    return "'" + text if text.startswith(_FORMULA_START) else text


def safe_filename(stem: str, fallback: str) -> str:
    """[A-Za-z0-9._-] only, no leading dots or dashes, at most 80 characters; the fallback when nothing is left."""
    cleaned = _UNSAFE_NAME.sub("-", stem or "").strip("-._")[:80].strip("-._")
    return cleaned or fallback


def content_disposition(filename: str) -> str:
    """`attachment` with an ASCII filename and the RFC 5987 filename* form."""
    return f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
