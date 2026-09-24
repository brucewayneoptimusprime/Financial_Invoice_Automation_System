"""The answer key for the extraction eval: `data/manifest.md`.

A manifest is a human-readable Markdown file. Anywhere in it you may write notes. Each labelled invoice has a `##`
heading with the exact file name, optional bullet notes, and ONE fenced block labelled `expected` holding JSON:

    ## acme_invoice_01.pdf
    - exercises: native PDF, tax excluded
    ```expected
    {"verified": true, "vendor_name": "Acme Supplies Ltd", "invoice_number": "INV-1001", "total": "2160.00",
     "vendor_tax_id": null, "line_items": [{"description": "Widgets", "amount": "2000.00"}]}
    ```

Rules:
  * A field that is omitted is NOT scored. An explicit `null` means "must be absent" (it catches hallucinations).
  * `"verified": true` is required for an entry to be scored. Anything else (false, missing) is a draft. Nothing in this
    program ever sets `verified` to true: a human flips it after checking the values against the document.
  * `line_items` is the COMPLETE expected list when present (each `{description?, amount}`); so is `adjustments`
    (each `{kind, amount}`, the amount signed: discounts and credits negative).
  * A section without an `expected` block is just notes and is ignored (unless its heading names an invoice file, which the
    eval warns about).
Problems (bad JSON, unknown field, duplicate heading...) never crash the parser: they are collected as messages and the
entry is skipped.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCALAR_FIELDS = ("vendor_name", "vendor_tax_id", "vendor_address", "document_type", "invoice_number", "invoice_date",
                 "currency", "po_reference", "subtotal", "tax", "total")
AMOUNT_FIELDS = ("subtotal", "tax", "total")
LIST_FIELDS = ("line_items", "adjustments")
ALLOWED_KEYS = frozenset({"verified", *SCALAR_FIELDS, *LIST_FIELDS})
DOCUMENT_TYPES = ("invoice", "credit_note", "proforma", "quote", "statement", "receipt", "other")
ADJUSTMENT_KINDS = ("shipping", "discount", "credit", "fee", "rounding", "other")

_HEADING = re.compile(r"^##\s+(?P<name>\S.*?)\s*$")
_FENCE_OPEN = re.compile(r"^```\s*(?P<label>[A-Za-z]*)\s*$")
_FENCE_CLOSE = re.compile(r"^```\s*$")
_NOTE = re.compile(r"^\s*[-*]\s+(?P<text>.+?)\s*$")

HEADER = """# Extraction answer key

Notes for humans go anywhere in this file. Each invoice has a `##` heading with its exact file name and ONE fenced
`expected` JSON block. Omitted fields are not scored; an explicit `null` means "must be absent". Only entries with
`"verified": true` are scored. `--draft-manifest` writes drafts with `"verified": false`; check every value against the
document yourself, then change it to `true`.
"""


@dataclass
class ManifestEntry:
    file: str
    expected: dict[str, Any]              # normalised: amounts as decimal strings, dates as ISO strings
    verified: bool = False
    notes: list[str] = field(default_factory=list)
    line: int = 0                         # 1-based line of the heading, for messages


@dataclass
class Manifest:
    entries: dict[str, ManifestEntry] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)              # human-readable; entries with problems are skipped
    sections_without_block: list[str] = field(default_factory=list)  # `##` headings that had no expected block

    @property
    def verified(self) -> dict[str, ManifestEntry]:
        return {k: e for k, e in self.entries.items() if e.verified}


# ------------------------------------------------------------------------------------------- normalisation

def _amount(value: Any, where: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{where}: expected a number or a numeric string")
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{where}: {value!r} is not a number") from None
    if not parsed.is_finite():
        raise ValueError(f"{where}: {value!r} is not finite")
    return format(parsed, "f")


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: expected a non-empty string (or null)")
    return value.strip()


def normalise_expected(raw: Any) -> tuple[dict[str, Any], bool]:
    """(expected dict, verified) or ValueError naming the problem."""
    if not isinstance(raw, dict):
        raise ValueError("the expected block must be a JSON object")
    unknown = sorted(set(raw) - ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"unknown field(s) {unknown}; allowed: {sorted(ALLOWED_KEYS)}")
    verified = raw.get("verified", False)
    if not isinstance(verified, bool):
        raise ValueError('"verified" must be true or false')
    out: dict[str, Any] = {}
    for name in SCALAR_FIELDS:
        if name not in raw:
            continue
        value = raw[name]
        if value is None:
            out[name] = None
        elif name in AMOUNT_FIELDS:
            out[name] = _amount(value, name)
        elif name == "invoice_date":
            try:
                out[name] = date.fromisoformat(_text(value, name)).isoformat()
            except ValueError:
                raise ValueError(f"invoice_date: {value!r} is not an ISO date (YYYY-MM-DD)") from None
        elif name == "currency":
            code = _text(value, name).upper()
            if len(code) != 3 or not code.isalpha():
                raise ValueError(f"currency: {value!r} is not a 3-letter code")
            out[name] = code
        elif name == "document_type":
            kind = _text(value, name).lower()
            if kind not in DOCUMENT_TYPES:
                raise ValueError(f"document_type: {value!r} must be one of {list(DOCUMENT_TYPES)}")
            out[name] = kind
        else:
            out[name] = _text(value, name)
    for name in LIST_FIELDS:
        if name not in raw:
            continue
        items = raw[name]
        if not isinstance(items, list):
            raise ValueError(f"{name}: expected a list")
        out[name] = [_normalise_item(name, i, item) for i, item in enumerate(items, start=1)]
    return out, verified


def _normalise_item(list_name: str, index: int, item: Any) -> dict[str, Any]:
    where = f"{list_name}[{index}]"
    if not isinstance(item, dict):
        raise ValueError(f"{where}: expected an object")
    if list_name == "line_items":
        extra = sorted(set(item) - {"description", "quantity", "unit_price", "amount"})
        if extra:
            raise ValueError(f"{where}: unknown key(s) {extra}")
        if "amount" not in item:
            raise ValueError(f"{where}: 'amount' is required")
        out: dict[str, Any] = {"amount": _amount(item["amount"], f"{where}.amount")}
        for key in ("quantity", "unit_price"):
            if item.get(key) is not None:
                out[key] = _amount(item[key], f"{where}.{key}")
        if item.get("description") is not None:
            out["description"] = _text(item["description"], f"{where}.description")
        return out
    extra = sorted(set(item) - {"kind", "amount", "description"})
    if extra:
        raise ValueError(f"{where}: unknown key(s) {extra}")
    if "kind" not in item or "amount" not in item:
        raise ValueError(f"{where}: 'kind' and 'amount' are required")
    kind = _text(item["kind"], f"{where}.kind").lower()
    if kind not in ADJUSTMENT_KINDS:
        raise ValueError(f"{where}.kind: {item['kind']!r} must be one of {list(ADJUSTMENT_KINDS)}")
    out = {"kind": kind, "amount": _amount(item["amount"], f"{where}.amount")}
    if item.get("description") is not None:
        out["description"] = _text(item["description"], f"{where}.description")
    return out


# ------------------------------------------------------------------------------------------------ parsing

def parse_manifest(text: str) -> Manifest:
    manifest = Manifest()
    lines = text.splitlines()
    i = 0
    current: str | None = None
    current_line = 0
    notes: list[str] = []
    block: list[str] | None = None
    block_label = ""
    found_block = False
    seen: set[str] = set()

    def finish_section() -> None:
        nonlocal current, notes, found_block
        if current is not None and not found_block:
            manifest.sections_without_block.append(current)
        current, notes, found_block = None, [], False

    while i < len(lines):
        line = lines[i]
        i += 1
        if block is not None:                                          # inside a fenced block
            if _FENCE_CLOSE.match(line):
                if block_label == "expected" and current is not None:
                    found_block = True
                    _add_entry(manifest, seen, current, current_line, notes,"\n".join(block))
                block = None
            else:
                block.append(line)
            continue
        if (m := _FENCE_OPEN.match(line)):
            block, block_label = [], m.group("label").lower()
            continue
        if (m := _HEADING.match(line)):
            finish_section()
            current, current_line = m.group("name"), i
            continue
        if current is not None and (m := _NOTE.match(line)):
            notes.append(m.group("text"))
    if block is not None:
        manifest.problems.append(f"{current or 'the file'}: a fenced block was never closed")
    finish_section()
    return manifest


def _add_entry(manifest: Manifest, seen: set[str], name: str, line: int, notes: list[str], body: str) -> None:
    if name in seen:
        manifest.problems.append(f"{name}: appears more than once (line {line}); the first entry is used")
        return
    seen.add(name)
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        manifest.problems.append(f"{name}: the expected block is not valid JSON ({exc.msg} at line {exc.lineno}); entry skipped")
        return
    try:
        expected, verified = normalise_expected(raw)
    except ValueError as exc:
        manifest.problems.append(f"{name}: {exc}; entry skipped")
        return
    manifest.entries[name] = ManifestEntry(file=name, expected=expected, verified=verified, notes=list(notes), line=line)


def load_manifest(path: Path) -> Manifest:
    """A missing manifest is an empty one (nothing is scored); an unreadable one is reported as a problem."""
    path = Path(path)
    if not path.is_file():
        return Manifest()
    try:
        return parse_manifest(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as exc:
        manifest = Manifest()
        manifest.problems.append(f"{path.name}: could not be read ({type(exc).__name__})")
        return manifest


# ------------------------------------------------------------------------------------------------ drafting

def draft_expected(invoice: Any) -> dict[str, Any]:
    """A draft `expected` block from an ExtractedInvoice: exactly what was extracted, `verified` false. Missing values become
    explicit nulls, which a human must confirm mean 'not on the document' (or delete)."""
    out: dict[str, Any] = {"verified": False}
    for name in SCALAR_FIELDS:
        value = getattr(invoice, name).value
        out[name] = None if value is None else (value.isoformat() if isinstance(value, date) else
                                                format(value, "f") if isinstance(value, Decimal) else str(value))
    out["line_items"] = [
        {k: v for k, v in (("description", i.description), ("quantity", None if i.quantity is None else format(i.quantity, "f")),
                           ("unit_price", None if i.unit_price is None else format(i.unit_price, "f")),
                           ("amount", None if i.amount is None else format(i.amount, "f"))) if v is not None}
        for i in invoice.line_items if i.amount is not None]
    out["adjustments"] = [{"kind": a.kind or "other", "amount": format(a.amount, "f"),
                           **({"description": a.description} if a.description else {})}
                          for a in invoice.adjustments if a.amount is not None]
    return out


def render_entry(file: str, expected: dict[str, Any], notes: list[str]) -> str:
    body = json.dumps(expected, indent=2, ensure_ascii=False)
    note_lines = "".join(f"- {n}\n" for n in notes)
    return f"## {file}\n{note_lines}```expected\n{body}\n```\n"


def append_drafts(path: Path, drafts: dict[str, tuple[dict[str, Any], list[str]]]) -> tuple[list[str], list[str]]:
    """Append draft entries for files that have NO entry yet. An existing entry (verified or not, even a broken one) is never
    touched. Returns (files written, files skipped because the manifest already mentions them)."""
    path = Path(path)
    existing_text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    mentioned = {m.group("name") for line in existing_text.splitlines() if (m := _HEADING.match(line))}
    written, skipped = [], []
    chunks = []
    for file, (expected, notes) in drafts.items():
        if file in mentioned:
            skipped.append(file)
            continue
        if expected.get("verified") is not False:
            raise ValueError("drafts must be written with verified=false")               # never auto-verify
        chunks.append(render_entry(file, expected, notes))
        written.append(file)
    if not chunks:
        return written, skipped
    base = existing_text if existing_text.strip() else HEADER
    text = base.rstrip("\n") + "\n\n" + "\n".join(chunks)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return written, skipped
