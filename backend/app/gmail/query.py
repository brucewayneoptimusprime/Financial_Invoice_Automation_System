"""The Gmail search query allowlist (SPEC section 11 item 84). ONE validator for every query, typed by the user or (later) written
by the query translator: anything outside the allowlist is REFUSED with a reason naming the term, never silently dropped or "fixed".

Allowed: plain words, "quoted phrases", OR between two terms, a leading '-' on words / phrases / from / to / subject / filename, and
the operators from:, to:, subject:, after:, before:, newer_than:, older_than:, filename:, larger:, smaller:, has:attachment.
Refused: everything else, notably in: (so spam, trash and all mail are never searched), is:, label:, category:, deliveredto:, list:,
rfc822msgid:, brackets, braces, control characters, and queries over the configured length or term count.

`finalize` then always adds has:attachment and a date window (newer_than:<N>d, or an after: N days before an upper bound) unless
the query already has a lower date bound. The final string is what is sent to Gmail and shown to the user.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.config import Settings
from app.gmail.errors import GmailError

ALLOWED_OPERATORS = ("from", "to", "subject", "after", "before", "newer_than", "older_than", "filename", "larger", "smaller", "has")
NEGATABLE = frozenset({"from", "to", "subject", "filename"})
_ALLOWED_TEXT = "from:, to:, subject:, after:, before:, newer_than:, older_than:, filename:, larger:, smaller:, has:attachment"
_REFUSED_HINT = {
    "in": "'in:' is not allowed: the search never looks in spam, trash or all mail.",
    "is": "'is:' (read, unread, starred, ...) is not allowed.",
    "label": "'label:' is not allowed.",
    "category": "'category:' is not allowed.",
    "deliveredto": "'deliveredto:' is not allowed.",
    "list": "'list:' is not allowed.",
    "rfc822msgid": "'rfc822msgid:' is not allowed.",
}

_TOKEN = re.compile(r'-?[A-Za-z][A-Za-z0-9_]*:"[^"]*"|-?"[^"]*"|\S+')
_OPERATOR = re.compile(r"^(-?)([A-Za-z][A-Za-z0-9_]*):(.*)$", re.S)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WORD = re.compile(r'[^\s"(){}:\\]{1,100}')
_ADDRESS = re.compile(r"[A-Za-z0-9@._+\-]{1,100}")
_FILENAME = re.compile(r"[A-Za-z0-9._\-]{1,100}")
_SIZE = re.compile(r"\d{1,6}[kKmM]?")
_RELATIVE = re.compile(r"(\d{1,4})([dmy])")
_DATE = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")
_DAYS_PER_UNIT = {"d": 1, "m": 30, "y": 365}


@dataclass(frozen=True)
class Term:
    kind: str                    # word | phrase | op | or
    value: str = ""
    op: str | None = None
    negated: bool = False
    quoted: bool = False         # an operator value given as a "phrase"

    def render(self) -> str:
        if self.kind == "or":
            return "OR"
        value = f'"{self.value}"' if self.kind == "phrase" or self.quoted else self.value
        return ("-" if self.negated else "") + (f"{self.op}:{value}" if self.kind == "op" else value)


@dataclass(frozen=True)
class ValidatedQuery:
    terms: tuple[Term, ...]

    @property
    def text(self) -> str:
        return " ".join(t.render() for t in self.terms)

    def ops(self, *names: str, negated: bool = False) -> list[Term]:
        return [t for t in self.terms if t.kind == "op" and t.op in names and t.negated == negated]


def _phrase(raw: str, label: str) -> tuple[str | None, str | None]:
    """(inner text, problem) for a value written as "...". """
    if len(raw) < 2 or not raw.endswith('"') or raw.count('"') != 2:
        return None, f"{label}: a quote is not closed."
    inner = raw[1:-1].strip()
    if not inner:
        return None, f"{label}: the quoted text is empty."
    if len(inner) > 100 or any(c in inner for c in "(){}\\"):
        return None, f"{label}: the quoted text is too long or contains brackets."
    return inner, None


def _parse_date(text: str) -> date | None:
    m = _DATE.fullmatch(text)
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return d if 1970 <= d.year <= 2100 else None


def _operator(token: str, negated: bool, op: str, value: str) -> tuple[Term | None, str | None]:
    if op not in ALLOWED_OPERATORS:
        return None, f"{_REFUSED_HINT.get(op, f'{op}: is not an allowed search operator.')} Allowed: {_ALLOWED_TEXT}."
    if negated and op not in NEGATABLE:
        return None, f"'{token}': {op}: cannot be negated."
    if not value:
        return None, f"'{token}': {op}: needs a value."
    quoted = value.startswith('"')
    if quoted:
        if op not in ("from", "to", "subject"):
            return None, f"'{token}': {op}: does not take a quoted value."
        inner, problem = _phrase(value, f"'{token}'")
        return (None, problem) if problem else (Term("op", inner, op, negated, quoted=True), None)
    ok = {"from": _ADDRESS, "to": _ADDRESS, "subject": _WORD, "filename": _FILENAME, "larger": _SIZE, "smaller": _SIZE}
    if op in ok:
        if not ok[op].fullmatch(value):
            return None, f"'{token}': the value is not allowed for {op}:."
        return Term("op", value, op, negated), None
    if op in ("after", "before"):
        d = _parse_date(value)
        if d is None:
            return None, f"'{token}': {op}: needs a date written YYYY/MM/DD."
        return Term("op", d.strftime("%Y/%m/%d"), op, negated), None
    if op in ("newer_than", "older_than"):
        m = _RELATIVE.fullmatch(value)
        if not m or int(m.group(1)) < 1:
            return None, f"'{token}': {op}: needs a number and d, m or y (for example {op}:30d)."
        return Term("op", value, op, negated), None
    if value.lower() != "attachment":                                       # op == "has"
        return None, f"'{token}': only has:attachment is allowed."
    return Term("op", "attachment", "has", negated), None


def validate_query(text: str, *, max_chars: int, max_terms: int) -> ValidatedQuery:
    """Parse and check a Gmail query against the allowlist. Raises GmailError('query_invalid') listing every problem."""
    text = text or ""
    if len(text) > max_chars:
        raise GmailError("query_invalid", f"The search was not run: it is longer than {max_chars} characters.",
                         problems=[f"The search is longer than {max_chars} characters."])
    problems: list[str] = []
    if _CONTROL.search(text):
        problems.append("The search contains control characters.")
    terms: list[Term] = []
    for token in _TOKEN.findall(text):
        if token == "OR":
            terms.append(Term("or"))
            continue
        if token == "AND":
            problems.append("'AND' is implicit in a Gmail search: remove it.")
            continue
        if any(c in token for c in "(){}"):
            problems.append(f"'{token}': brackets and braces are not allowed.")
            continue
        m = _OPERATOR.match(token)
        if m:
            term, problem = _operator(token, m.group(1) == "-", m.group(2).lower(), m.group(3))
        else:
            negated = token.startswith("-")
            body = token[1:] if negated else token
            if not body:
                term, problem = None, "A lone '-' needs a word after it."
            elif body.startswith('"'):
                inner, problem = _phrase(body, f"'{token}'")
                term = None if problem else Term("phrase", inner, negated=negated)
            elif _WORD.fullmatch(body):
                term, problem = Term("word", body, negated=negated), None
            else:
                term, problem = None, f"'{token}' contains characters that are not allowed (quotes, colons, brackets)."
        if problem:
            problems.append(problem)
        else:
            terms.append(term)
    for i, t in enumerate(terms):
        if t.kind == "or" and (i == 0 or i == len(terms) - 1 or terms[i - 1].kind == "or" or terms[i + 1].kind == "or"):
            problems.append("OR needs a search term on both sides.")
            break
    if sum(t.kind != "or" for t in terms) > max_terms:
        problems.append(f"The search has more than {max_terms} terms; make it shorter.")
    if problems:
        raise GmailError("query_invalid", "The search was not run: " + problems[0], problems=problems)
    return ValidatedQuery(tuple(terms))


def _upper_bound(q: ValidatedQuery, today: date) -> date | None:
    bounds = [_parse_date(t.value) for t in q.ops("before")]
    for t in q.ops("older_than"):
        n, unit = _RELATIVE.fullmatch(t.value).groups()
        bounds.append(today - timedelta(days=int(n) * _DAYS_PER_UNIT[unit]))
    bounds = [b for b in bounds if b is not None]
    return min(bounds) if bounds else None


def finalize(q: ValidatedQuery, settings: Settings, today: date) -> tuple[str, list[str]]:
    """(the query sent to Gmail, the terms the system added). Always has:attachment; always a lower date bound."""
    added: list[str] = []
    if not q.ops("has"):
        added.append("has:attachment")
    if not q.ops("after", "newer_than"):
        window = settings.gmail_default_window_days
        upper = _upper_bound(q, today)
        added.append(f"after:{(upper - timedelta(days=window)).strftime('%Y/%m/%d')}" if upper else f"newer_than:{window}d")
    return " ".join([q.text, *added]).strip(), added


def check_and_finalize(text: str, settings: Settings, today: date) -> tuple[ValidatedQuery, str, list[str]]:
    q = validate_query(text, max_chars=settings.gmail_query_max_chars, max_terms=settings.gmail_query_max_terms)
    final, added = finalize(q, settings, today)
    return q, final, added
