"""Score an extraction against a verified answer key.

Per expected field the verdict is one of:
  correct       the extracted value equals the expected one (after normalisation)
  missed        a value was expected but the extraction has null
  hallucinated  null was expected ("must be absent") but the extraction has a value; for lists, an extracted item that
                matches no expected item
  wrong         a value was expected and a DIFFERENT value was extracted

Comparison: money as exact Decimal; dates as ISO; currency upper-cased; tax ids ignoring case, spaces, hyphens and dots;
every other string ignoring case, punctuation and spacing. Line items match by exact amount plus, when the key gives one,
a description that contains (or is contained in) the extracted one after that normalisation. Adjustments match by kind and
signed amount. Fields not in the key are not scored.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.engine.normalize import normalize_tax_id
from app.extraction.grounding import alnum
from app.extraction.manifest import AMOUNT_FIELDS, SCALAR_FIELDS
from app.models.extraction import ExtractedInvoice

VERDICTS = ("correct", "missed", "hallucinated", "wrong")


@dataclass
class Judgement:
    field: str                       # e.g. "total", "line_items"
    verdict: str
    expected: Any = None
    actual: Any = None
    confidence: float | None = None          # effective, after grounding (None when nothing was extracted)
    model_confidence: float | None = None    # the model's raw score


@dataclass
class FileScore:
    file: str
    judgements: list[Judgement] = field(default_factory=list)

    @property
    def all_correct(self) -> bool:
        return all(j.verdict == "correct" for j in self.judgements)


@dataclass
class FieldStats:
    scored: int = 0
    correct: int = 0
    missed: int = 0
    hallucinated: int = 0
    wrong: int = 0

    @property
    def accuracy(self) -> float | None:
        return None if self.scored == 0 else self.correct / self.scored


@dataclass
class Totals:
    """Aggregated over files, per field, plus the confidence of right and wrong answers."""
    fields: dict[str, FieldStats] = field(default_factory=dict)
    conf_correct: list[float] = field(default_factory=list)
    conf_wrong: list[float] = field(default_factory=list)
    model_conf_correct: list[float] = field(default_factory=list)
    model_conf_wrong: list[float] = field(default_factory=list)
    files: int = 0
    files_all_correct: int = 0

    def add(self, score: FileScore) -> None:
        self.files += 1
        self.files_all_correct += score.all_correct
        for j in score.judgements:
            stats = self.fields.setdefault(j.field, FieldStats())
            stats.scored += 1              # every judgement is one scored item (an extra extracted list item counts against accuracy)
            setattr(stats, j.verdict, getattr(stats, j.verdict) + 1)
            if j.confidence is not None:
                (self.conf_correct if j.verdict == "correct" else self.conf_wrong).append(j.confidence)
            if j.model_confidence is not None:
                (self.model_conf_correct if j.verdict == "correct" else self.model_conf_wrong).append(j.model_confidence)


# ----------------------------------------------------------------------------------------------- comparison

def _norm_scalar(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in AMOUNT_FIELDS:
        return Decimal(str(value))
    if isinstance(value, date):
        return value.isoformat()
    if name == "invoice_date":
        return str(value)
    if name == "currency":
        return str(value).upper()
    if name == "document_type":
        return str(value)
    if name == "vendor_tax_id":
        return normalize_tax_id(str(value))
    return alnum(str(value))


def _scalar_verdict(expected: Any, actual: Any) -> str:
    if expected is None:
        return "correct" if actual is None else "hallucinated"
    if actual is None:
        return "missed"
    return "correct" if expected == actual else "wrong"


def _display(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _description_matches(expected: str, actual: str | None) -> bool:
    a, b = alnum(expected), alnum(actual or "")
    return bool(a and b and (a in b or b in a))


def _match_lines(expected: list[dict], extracted: list) -> list[Judgement]:
    unused = list(range(len(extracted)))
    out: list[Judgement] = []
    for want in expected:
        hit = None
        for idx in unused:
            got = extracted[idx]
            if got.amount is None or got.amount != Decimal(want["amount"]):
                continue
            if "description" in want and not _description_matches(want["description"], got.description):
                continue
            if "quantity" in want and (got.quantity is None or got.quantity != Decimal(want["quantity"])):
                continue
            if "unit_price" in want and (got.unit_price is None or got.unit_price != Decimal(want["unit_price"])):
                continue
            hit = idx
            break
        if hit is None:
            out.append(Judgement("line_items", "missed", expected=want))
        else:
            unused.remove(hit)
            got = extracted[hit]
            out.append(Judgement("line_items", "correct", expected=want, actual=_display(got.amount),
                                 confidence=got.confidence, model_confidence=got.model_confidence))
    for idx in unused:                                                   # extracted lines nobody asked for
        got = extracted[idx]
        out.append(Judgement("line_items", "hallucinated", actual={"description": got.description, "amount": _display(got.amount)},
                             confidence=got.confidence, model_confidence=got.model_confidence))
    return out


def _match_adjustments(expected: list[dict], extracted: list) -> list[Judgement]:
    unused = list(range(len(extracted)))
    out: list[Judgement] = []
    for want in expected:
        hit = next((i for i in unused if extracted[i].kind == want["kind"] and extracted[i].amount is not None
                    and extracted[i].amount == Decimal(want["amount"])), None)
        if hit is None:
            out.append(Judgement("adjustments", "missed", expected=want))
        else:
            unused.remove(hit)
            got = extracted[hit]
            out.append(Judgement("adjustments", "correct", expected=want, actual=_display(got.amount),
                                 confidence=got.confidence, model_confidence=got.model_confidence))
    for idx in unused:
        got = extracted[idx]
        out.append(Judgement("adjustments", "hallucinated", actual={"kind": got.kind, "amount": _display(got.amount)},
                             confidence=got.confidence, model_confidence=got.model_confidence))
    return out


def score_file(file: str, expected: dict[str, Any], invoice: ExtractedInvoice) -> FileScore:
    """Judge one extraction against one verified `expected` block (as normalised by the manifest parser)."""
    score = FileScore(file=file)
    for name in SCALAR_FIELDS:
        if name not in expected:
            continue
        got = getattr(invoice, name)
        want_norm = _norm_scalar(name, expected[name])
        got_norm = _norm_scalar(name, got.value)
        verdict = _scalar_verdict(want_norm, got_norm)
        score.judgements.append(Judgement(
            name, verdict, expected=expected[name], actual=_display(got.value),
            confidence=None if got.value is None else got.confidence,
            model_confidence=None if got.value is None else got.model_confidence))
    if "line_items" in expected:
        score.judgements.extend(_match_lines(expected["line_items"], invoice.line_items))
    if "adjustments" in expected:
        score.judgements.extend(_match_adjustments(expected["adjustments"], invoice.adjustments))
    return score
