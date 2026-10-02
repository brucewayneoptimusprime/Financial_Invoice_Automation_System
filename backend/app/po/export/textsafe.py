"""Text clean-up for the document formats (Word, PDF).

- Control characters (except tab and newline) are removed: they are invalid in Word's XML and meaningless in a PDF.
- The PDF uses the built-in Helvetica (owner decision 3), which draws the Windows-1252 character set only; any other character
  (e.g. the rupee sign, CJK names) becomes "?" and the PDF footer says so. Word, Excel and CSV keep every character.
"""
import re
from decimal import Decimal
from typing import Any

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: Any) -> str:
    return _CONTROL.sub("", "" if text is None else str(text))


def latin(text: str) -> tuple[str, bool]:
    """(text drawable by Helvetica, whether anything was replaced)."""
    out, replaced = [], False
    for ch in text:
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            out.append("?")
            replaced = True
    return "".join(out), replaced


def display(value: Any, kind: str) -> str:
    """How a value reads in Word / PDF: money as 1,500.00 (exact Decimal formatting), quantities as given, counts as integers."""
    if value is None:
        return "—"
    if kind == "money":
        return f"{Decimal(value):,.2f}"
    if kind == "int":
        return str(int(value))
    return clean(value)


def header(label: str, kind: str, currency: str | None) -> str:
    return f"{label} ({currency})" if kind in ("money", "price") and currency else label
