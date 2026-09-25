"""The trail digest: the ONLY material the explainer and the drafter may work from.

`build_digest(ctx, ...)` is deterministic code. It turns the rule results, engine floors, vendor and PO match and extraction
metadata already on the run context into numbered facts (F1, F2, ...). Nothing here comes from a model's memory or judgment:
the text of a rule fact is the engine's own message. No invoice text, no images and no free-text model output goes in.

A fact may carry a vendor-facing `category` (from config `vendor_facing_rules`); facts without one are internal and are never
shown to a vendor.
"""
import re
from dataclasses import dataclass, field, replace
from typing import Any

from app.config import Settings
from app.engine.floor import FLOOR_RULE_ID, REFERENCE_FLOOR_RULE_ID
from app.engine.severity import aggregate, is_triggered
from app.enums import Decision, MatchStatus
from app.models.run import RunContext

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
FLOOR_IDS = (FLOOR_RULE_ID, REFERENCE_FLOOR_RULE_ID)


def numbers_in(text: str) -> set[str]:
    """Every number in `text`, commas removed ('1,125.00' -> '1125.00'); dates count as a unit ('2013-03-07')."""
    found = {d for d in _DATE.findall(text)}
    stripped = _DATE.sub(" ", text)
    found |= {m.replace(",", "").rstrip(".") for m in _NUMBER.findall(stripped)}
    return {f for f in found if f}


@dataclass(frozen=True)
class Fact:
    id: str                                   # F1, F2, ...
    kind: str                                 # decision | rule | floor | override | summary | vendor | match | extraction | invoice
    text: str                                 # deterministic, the engine's own wording
    short: str = ""                           # a few words for one-line summaries
    rule_id: str | None = None
    outcome_key: str | None = None
    severity: int = 0
    category: str | None = None               # vendor-facing request kind, or None (internal)
    items: tuple[str, ...] = ()               # e.g. the missing field names
    data: dict[str, Any] = field(default_factory=dict, compare=False)     # for templates only; never sent to a model

    @property
    def triggered(self) -> bool:
        return self.kind in ("rule", "floor", "override") and self.severity > 0


@dataclass(frozen=True)
class TrailDigest:
    decision: Decision
    severity: int
    facts: tuple[Fact, ...]

    def fact(self, fact_id: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fact_id), None)

    @property
    def triggered(self) -> tuple[Fact, ...]:
        return tuple(f for f in self.facts if f.triggered)

    @property
    def vendor_facing(self) -> tuple[Fact, ...]:
        return tuple(f for f in self.triggered if f.category)

    def kind(self, kind: str) -> tuple[Fact, ...]:
        return tuple(f for f in self.facts if f.kind == kind)

    def allowed_numbers(self, fact_ids: list[str] | None = None) -> set[str]:
        ids = set(fact_ids) if fact_ids is not None else None
        out: set[str] = set()
        for f in self.facts:
            if ids is None or f.id in ids:
                out |= numbers_in(f.text)
        return out

    def prompt_dict(self, only: tuple[Fact, ...] | None = None) -> dict[str, Any]:
        """What a model sees: ids, kinds, the deterministic text, severity, the vendor-facing category and items. Nothing else."""
        return {"decision": self.decision.value, "severity": self.severity,
                "facts": [{"id": f.id, "kind": f.kind, "text": f.text, "severity": f.severity, "category": f.category,
                           "items": list(f.items)} for f in (only if only is not None else self.facts)]}

    def with_override(self, text: str, decision: Decision, severity: int) -> "TrailDigest":
        """A new digest for a decision the ACT stage had to escalate, with an extra triggered fact explaining why."""
        facts = list(self.facts)
        facts[0] = replace(facts[0], text=_decision_text(decision, severity))
        facts.append(Fact(id=f"F{len(facts) + 1}", kind="override", text=text, short="approval withheld", severity=severity))
        return TrailDigest(decision=decision, severity=severity, facts=tuple(facts))


def _decision_text(decision: Decision, severity: int) -> str:
    return f"Final decision: {decision.value} (severity {severity}), the highest severity among the triggered checks."


class _Ids:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"F{self.n}"


def _rule_items(rule_id: str, detail: dict[str, Any]) -> tuple[str, ...]:
    if rule_id == "r_required_fields":
        return tuple(detail.get("missing") or ())
    if rule_id == "r_extraction_confidence":
        return tuple(x["field"] for x in detail.get("low_confidence") or ())
    if rule_id == "r_arithmetic":
        return tuple(detail.get("failed") or ())
    return ()


def _vendor_fact(ctx: RunContext, fid: str) -> Fact:
    ex = ctx.extracted
    name = None if ex is None else ex.vendor_name.value
    vm = ctx.matched_vendor
    if vm is None:
        return Fact(fid, "vendor", "Vendor matching was not run.", "no vendor match")
    vendor = None if ctx.facts is None or vm.vendor_id is None else ctx.facts.vendor_by_id(vm.vendor_id)
    printed = f"'{name}'" if name else "no vendor name"
    if vendor is None:
        return Fact(fid, "vendor", f"The invoice shows {printed}; it did not match any known vendor.", "vendor unknown")
    tail = f" ({vm.method}, score {vm.score:.2f}" + ("; ambiguous with another vendor" if vm.ambiguous else "") + ")"
    return Fact(fid, "vendor", f"The invoice shows {printed}; it matched known vendor '{vendor.name}' (status {vendor.status.value}){tail}.",
                "vendor " + vendor.status.value, data={"vendor": vendor.name, "status": vendor.status.value})


def _match_fact(ctx: RunContext, fid: str) -> Fact:
    status = ctx.match_status
    top = ctx.candidates[0] if ctx.candidates else None
    if status is None:
        return Fact(fid, "match", "PO matching was not run.", "no PO match")
    if status == MatchStatus.MATCHED and ctx.matched_po is not None:
        return Fact(fid, "match", f"Purchase order {ctx.matched_po.po_number} is a confident, unambiguous match (score {ctx.matched_po.score:.2f}).",
                    "PO matched", data={"po_number": ctx.matched_po.po_number})
    if status == MatchStatus.NO_CANDIDATES:
        return Fact(fid, "match", "No candidate purchase order was found.", "no PO candidates")
    ranked = ", ".join(f"{c.po_number} ({c.score:.2f})" for c in ctx.candidates[:2])
    label = {MatchStatus.LOW_SCORE: "no candidate scored high enough for a confident match",
             MatchStatus.AMBIGUOUS: "the top candidates are too close to call"}[status]
    return Fact(fid, "match", f"No purchase order was matched: {label}" + (f" (best: {ranked})." if top else "."), "PO not matched")


def _extraction_facts(ctx: RunContext, ids: _Ids) -> list[Fact]:
    meta = ctx.extraction_meta
    out: list[Fact] = []
    if meta is None:
        return out
    if meta.degraded:
        out.append(Fact(ids(), "extraction", f"Extraction failed ({meta.failure_kind}, {meta.failure_code}): {meta.failure_reason}",
                        "extraction failed", data={"failure_kind": meta.failure_kind, "failure_code": meta.failure_code}))
    problems = {k: v for k, v in meta.grounding.items() if k in ("value_mismatch", "not_found", "no_source", "fuzzy")}
    if problems:
        out.append(Fact(ids(), "extraction", "Extracted values that the document text did not support: "
                        + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(problems.items())) + ".", "grounding problems"))
    return out


def _invoice_fact(ctx: RunContext, fid: str) -> Fact | None:
    ex = ctx.extracted
    if ex is None:
        return None
    bits = []
    if ex.invoice_number.value:
        bits.append(f"number {ex.invoice_number.value}")
    if ex.invoice_date.value:
        bits.append(f"dated {ex.invoice_date.value.isoformat()}")
    if ex.total.value is not None:
        bits.append(f"total {ex.total.value}" + (f" {ex.currency.value}" if ex.currency.value else ""))
    if not bits:
        return None
    return Fact(fid, "invoice", "Invoice as extracted: " + ", ".join(bits) + ".", "invoice",
                data={"number": ex.invoice_number.value, "date": None if ex.invoice_date.value is None else ex.invoice_date.value.isoformat(),
                      "total": None if ex.total.value is None else str(ex.total.value), "currency": ex.currency.value,
                      "vendor_name": ex.vendor_name.value})


def build_digest(ctx: RunContext, settings: Settings, rule_names: dict[str, str] | None = None) -> TrailDigest:
    """Numbered facts, in a fixed order: decision, triggered checks (highest severity first), what passed, vendor, PO match,
    extraction problems, the invoice as extracted."""
    rule_names = rule_names or {}
    ids = _Ids()
    severity = aggregate(ctx.rule_results)
    decision = ctx.decision if ctx.decision is not None else Decision.REVIEW
    facts: list[Fact] = [Fact(ids(), "decision", _decision_text(decision, severity), decision.value, severity=severity)]

    triggered = sorted((r for r in ctx.rule_results if is_triggered(r)), key=lambda r: (-r.severity, r.rule_id))
    for r in triggered:
        if r.rule_id in FLOOR_IDS:
            reasons = r.detail.get("reasons")
            if r.rule_id == FLOOR_RULE_ID and reasons:
                for reason in reasons:
                    vendor_side = reason["code"] == "extraction_degraded" and reason.get("failure_kind") == "vendor_side"
                    facts.append(Fact(ids(), "floor", f"Engine floor ({reason['code']}): {reason['message']}.", reason["code"], rule_id=r.rule_id,
                                      outcome_key=reason["code"], severity=r.severity, category="unreadable_file" if vendor_side else None,
                                      items=(reason.get("failure_code") or "",) if vendor_side else (),
                                      data={"reason": reason}))
            else:
                facts.append(Fact(ids(), "floor", r.message, r.outcome_key or "reference floor", rule_id=r.rule_id, outcome_key=r.outcome_key,
                                  severity=r.severity))
            continue
        name = rule_names.get(r.rule_id, r.rule_id)
        facts.append(Fact(ids(), "rule", f"{r.rule_id} - {name} ({r.outcome_key}, severity {r.severity}): {r.message}",
                          f"{r.rule_id} {r.outcome_key}", rule_id=r.rule_id, outcome_key=r.outcome_key, severity=r.severity,
                          category=settings.vendor_facing_rules.get(r.rule_id), items=_rule_items(r.rule_id, r.detail),
                          data={"detail": r.detail, "message": r.message}))

    passed = sorted(r.rule_id for r in ctx.rule_results if r.outcome.value == "pass" and r.rule_id not in FLOOR_IDS)
    unevaluated = sorted(r.rule_id for r in ctx.rule_results if r.outcome.value == "info")
    if passed:
        facts.append(Fact(ids(), "summary", f"{len(passed)} checks passed with no concern: {', '.join(passed)}.", "checks passed"))
    if unevaluated:
        facts.append(Fact(ids(), "summary", f"{len(unevaluated)} checks could not be evaluated (a required input was missing): "
                          f"{', '.join(unevaluated)}.", "checks not evaluated"))
    facts.append(_vendor_fact(ctx, ids()))
    facts.append(_match_fact(ctx, ids()))
    facts.extend(_extraction_facts(ctx, ids))
    inv = _invoice_fact(ctx, ids())
    if inv is not None:
        facts.append(inv)
    return TrailDigest(decision=decision, severity=severity, facts=tuple(facts))
