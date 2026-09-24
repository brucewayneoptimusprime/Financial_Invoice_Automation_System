"""Deterministic parsers for amounts, dates and currencies as they appear on invoices.

Two kinds of amount parsing, because two different questions are asked:
  normalize_amount_string  "clean up what the MODEL returned": the model was told to use a dot as the decimal
                           point, so the wire reading is unambiguous ('1,234' is a thousands group).
  amount_candidates        "what could this number in the DOCUMENT mean": text is ambiguous ('1.234' may be 1234
                           or 1.234), so every plausible reading is returned. Used by the grounding check.
Both handle US (1,234.56), European (1.234,56) and Indian (1,00,000.00) digit grouping.
"""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

# ----------------------------------------------------------------------------------------------- amounts

_SIMPLE_NUMBER = re.compile(r"(?<!\d)(?:\d[\d.,]*\d|\d)(?!\d)")
_SPACED_NUMBER = re.compile(r"(?<!\d)\d{1,3}(?:[   ]\d{3})+(?:[.,]\d{1,2})?(?!\d)")
_WIRE_JUNK = re.compile(r"(?i)(?:\b(?:rs|inr|usd|eur|gbp|cad|aud)\b\.?|[₹$€£¥]|/-)")
_WIRE_NUMBER = re.compile(r"^[\d.,]+$")
_MINUS_SIGNS = "-−–‒"


def _dec(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def amount_candidates(token: str) -> set[Decimal]:
    """Every plausible value of ONE digit-and-separator token from a document (always non-negative)."""
    t = token.strip()
    if not t or not re.fullmatch(r"[\d.,]+", t) or not t[0].isdigit() or not t[-1].isdigit():
        return set()
    commas, dots = t.count(","), t.count(".")
    if commas and dots:
        decimal_sep = "," if t.rfind(",") > t.rfind(".") else "."
        group_sep = "." if decimal_sep == "," else ","
        cleaned = t.replace(group_sep, "").replace(decimal_sep, ".")
        value = _dec(cleaned) if cleaned.count(".") == 1 else None
        return {value} if value is not None else set()
    if commas or dots:
        sep = "," if commas else "."
        if (commas or dots) > 1:                               # 1,00,000 / 1.234.567: grouping only
            value = _dec(t.replace(sep, ""))
            return {value} if value is not None else set()
        head, _, tail = t.partition(sep)
        if head in ("", "0") or len(tail) != 3:                # 0,123 / 12,5 / 12,50 / 1,2345: a decimal point
            value = _dec(f"{head or '0'}.{tail}")
            return {value} if value is not None else set()
        readings = {_dec(head + tail), _dec(f"{head}.{tail}")}  # 1,234 / 1.234: thousands group OR decimal
        return {r for r in readings if r is not None}
    value = _dec(t)
    return {value} if value is not None else set()


def find_amounts(text: str) -> set[Decimal]:
    """All plausible numeric values appearing in `text` (used to check that a value is really on a page)."""
    found: set[Decimal] = set()
    for match in _SIMPLE_NUMBER.finditer(text):
        found |= amount_candidates(match.group())
    for match in _SPACED_NUMBER.finditer(text):               # '1 234,56': space as a thousands separator
        found |= amount_candidates(re.sub(r"[   ]", "", match.group()))
    return found


def normalize_amount_string(text: str) -> str | None:
    """Clean an amount the MODEL returned into a plain decimal string ('2160.00', '-500.00'), or None if it
    cannot be read. Currency marks, brackets/minus signs, '/-' and digit grouping are handled; digits are
    never altered."""
    if text is None:
        return None
    s = _WIRE_JUNK.sub("", str(text)).strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s and s[0] in _MINUS_SIGNS:
        negative, s = True, s[1:].strip()
    if s and s[-1] in _MINUS_SIGNS:
        negative, s = True, s[:-1].strip()
    if s.startswith("+"):
        s = s[1:].strip()
    s = re.sub(r"[   ]", "", s)
    if not s or not _WIRE_NUMBER.match(s) or not s[0].isdigit() or not s[-1].isdigit():
        return None
    commas, dots = s.count(","), s.count(".")
    if commas and dots:
        decimal_sep = "," if s.rfind(",") > s.rfind(".") else "."
        group_sep = "." if decimal_sep == "," else ","
        s = s.replace(group_sep, "").replace(decimal_sep, ".")
        if s.count(".") != 1:
            return None
    elif commas:
        if commas > 1:
            s = s.replace(",", "")
        else:
            head, _, tail = s.partition(",")
            s = head + tail if (len(tail) == 3 and head not in ("", "0")) else f"{head}.{tail}"
    elif dots > 1:
        s = s.replace(".", "")
    if _dec(s) is None:
        return None
    return f"-{s}" if negative else s


# ------------------------------------------------------------------------------------------------ dates

_MONTHS = {name: i for i, names in enumerate(
    [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
     ("dec", "december")], start=1) for name in names}
_MONTH_WORD = r"(?P<mon>[A-Za-z]{3,9})\.?"
_ISO = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})(?!\d)")
_DAY_MONTH_YEAR = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s.\-]+" + _MONTH_WORD + r"[\s,.\-]+(\d{4}|\d{2})(?!\d)", re.I)
_MONTH_DAY_YEAR = re.compile(_MONTH_WORD + r"[\s.\-]+(\d{1,2})(?:st|nd|rd|th)?[\s,.\-]+(\d{4}|\d{2})(?!\d)", re.I)


def _year(text: str) -> int:
    y = int(text)
    return y + 2000 if y < 100 else y


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _textual_dates(text: str) -> set[date]:
    found: set[date] = set()
    for m in _DAY_MONTH_YEAR.finditer(text):
        month = _MONTHS.get(m.group("mon").lower())
        if month and (d := _safe_date(_year(m.group(3)), month, int(m.group(1)))):
            found.add(d)
    for m in _MONTH_DAY_YEAR.finditer(text):
        month = _MONTHS.get(m.group("mon").lower())
        if month and (d := _safe_date(_year(m.group(3)), month, int(m.group(2)))):
            found.add(d)
    return found


def _iso_dates(text: str) -> set[date]:
    return {d for m in _ISO.finditer(text) if (d := _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))}


def _numeric_readings(text: str) -> list[tuple[int, int, int]]:
    return [(int(m.group(1)), int(m.group(2)), _year(m.group(3))) for m in _NUMERIC.finditer(text)]


def date_candidates(text: str) -> set[date]:
    """Every date that `text` could be denoting (both day/month readings of an ambiguous numeric date)."""
    found = _iso_dates(text) | _textual_dates(text)
    for a, b, year in _numeric_readings(text):
        for d, m in ((a, b), (b, a)):                            # day-first and month-first readings
            if (value := _safe_date(year, m, d)):
                found.add(value)
    return found


def is_ambiguous_dmy(text: str) -> bool:
    """True if the only date in `text` is numeric with day and month both <= 12 and different (03/04/2026)."""
    if _iso_dates(text) or _textual_dates(text):
        return False
    return any(a <= 12 and b <= 12 and a != b for a, b, _ in _numeric_readings(text))


def normalize_date_string(value: str) -> str | None:
    """ISO date for a model-returned date string when it is unambiguous; None otherwise."""
    candidates = date_candidates(value)
    if len(candidates) == 1:
        return next(iter(candidates)).isoformat()
    return None


# --------------------------------------------------------------------------------------------- currency

def map_currency(raw: str | None, symbol_map: dict[str, str]) -> tuple[str | None, str | None]:
    """(ISO code or None, system note). Three-letter codes pass through (upper-cased); symbols and abbreviations
    are mapped via config; anything else stays unmapped (ambiguous symbols such as the yen sign)."""
    if raw is None or not str(raw).strip():
        return None, None
    value = str(raw).strip()
    if re.fullmatch(r"[A-Za-z]{3}", value):
        return value.upper(), None
    lookup = {k.casefold(): v for k, v in symbol_map.items()}
    for candidate in (value, value.rstrip("."), value.casefold(), value.casefold().rstrip(".")):
        code = symbol_map.get(candidate) or lookup.get(candidate.casefold())
        if code:
            return code, f"currency {value!r} was mapped to {code} by configuration"
    return None, f"currency symbol {value!r} is ambiguous or unsupported and was not mapped"
