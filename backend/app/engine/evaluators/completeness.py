"""required_fields and extraction_confidence. Null counts as missing regardless of its confidence."""
from typing import Any

from pydantic import Field, field_validator

from app.config import get_settings
from app.engine.evaluators.base import flag, not_evaluable, ok, system_side_failure
from app.engine.evaluators.registry import BaseParams, register
from app.engine.normalize import is_missing
from app.models.run import RunContext

CHECKABLE_FIELDS = frozenset({"vendor_name", "invoice_number", "invoice_date", "currency", "po_reference",
                              "subtotal", "tax", "total", "line_items"})


class FieldListParams(BaseParams):
    fields: list[str] = Field(default_factory=lambda: list(get_settings().required_fields), min_length=1)

    @field_validator("fields")
    @classmethod
    def _known_fields(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - CHECKABLE_FIELDS)
        if unknown:
            raise ValueError(f"unknown invoice field(s): {unknown}")
        return v


def _is_missing_field(ctx: RunContext, name: str) -> bool:
    if ctx.extracted is None:
        return True
    if name == "line_items":
        return not ctx.extracted.line_items
    return is_missing(getattr(ctx.extracted, name).value)


@register("required_fields", FieldListParams)
def required_fields(ctx: RunContext, params: dict[str, Any]):
    if (code := system_side_failure(ctx)) is not None:
        # A failure of OURS is not the vendor's omission: do not ask the vendor to resend. The engine floor
        # (extraction_degraded) sends the run to a human instead.
        return not_evaluable("extraction_failed_system_side",
                             f"the extraction failed on our side ({code}); the fields are unknown, not missing",
                             {"failure_code": code})
    fields = params["fields"]
    missing = [f for f in fields if _is_missing_field(ctx, f)]
    detail = {"required": fields, "missing": missing, "present": [f for f in fields if f not in missing]}
    if missing:
        return flag(params, "missing", "Required field(s) missing or unreadable: " + ", ".join(missing) + ".", detail)
    return ok(f"All {len(fields)} required fields are present.", detail, "complete")


@register("extraction_confidence", FieldListParams)
def extraction_confidence(ctx: RunContext, params: dict[str, Any]):
    if ctx.facts is None:
        return not_evaluable("no_facts", "the confidence threshold is unavailable")
    if ctx.extracted is None:
        return not_evaluable("no_extraction", "no extracted invoice is available")
    threshold = ctx.facts.settings.confidence_threshold
    low, checked, missing = [], [], []
    for name in params["fields"]:
        if name == "line_items":
            continue                                    # line-level confidence is out of scope for this rule
        field = getattr(ctx.extracted, name)
        if is_missing(field.value):
            missing.append(name)                        # null is the completeness rule's business
            continue
        checked.append({"field": name, "confidence": field.confidence})
        if field.confidence < threshold:
            low.append({"field": name, "confidence": field.confidence, "threshold": threshold})
    detail = {"threshold": threshold, "checked": checked, "low_confidence": low, "skipped_missing": missing}
    if not checked:
        return not_evaluable("no_present_fields", "none of the listed fields has a value to assess", detail)
    if low:
        parts = ", ".join(f"{x['field']} ({x['confidence']:.2f} < {threshold:.2f})" for x in low)
        return flag(params, "low_confidence", f"Low extraction confidence on: {parts}.", detail)
    return ok(f"All {len(checked)} checked fields meet the confidence threshold ({threshold:.2f}).", detail, "confident")


class DocumentTypeParams(BaseParams):
    allowed: list[str] = Field(default_factory=lambda: ["invoice"], min_length=1)


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


@register("document_type", DocumentTypeParams)
def document_type(ctx: RunContext, params: dict[str, Any]):
    """Only the allowed document types (default: invoice) go on; a credit note, quote, statement... needs a human.
    A missing type is not evaluable (the completeness and confidence rules deal with missing data)."""
    if ctx.extracted is None:
        return not_evaluable("no_extraction", "no extracted invoice is available")
    value = ctx.extracted.document_type.value
    if is_missing(value):
        return not_evaluable("missing:document_type", "the document type could not be determined")
    detail = {"document_type": value, "allowed": params["allowed"]}
    if value in params["allowed"]:
        return ok(f"The document is {_article(value)} {value.replace('_', ' ')}.", detail, "allowed")
    return flag(params, "not_an_invoice", f"The document looks like {_article(value)} {value.replace('_', ' ')}, not an invoice.", detail)
