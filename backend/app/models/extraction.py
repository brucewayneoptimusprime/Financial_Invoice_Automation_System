"""Extracted-invoice contract (SPEC 6.1) - the LLM-facing model.

Policy: unknown keys are IGNORED (and logged), because model output is not under our control.
Missing data is a valid state: every field's value may be null with confidence 0, and a field
omitted entirely is treated the same way. Whether missing data is acceptable is a rules decision
(completeness / confidence rules), never a schema failure.
"""
import logging
from datetime import date
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

T = TypeVar("T")


class LLMModel(BaseModel):
    """Base for models that parse LLM output: ignore unknown keys, but log them."""

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _log_extra_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            known = set(cls.model_fields)
            extras = sorted(k for k in data if k not in known)
            if extras:
                logger.warning("Ignoring unexpected keys %s in %s", extras, cls.__name__)
        return data


class EvidencedField(LLMModel, Generic[T]):
    """A value plus the evidence and confidence that travel with it."""

    value: T | None = None
    page: int | None = Field(default=None, ge=1)
    source_text: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class CurrencyField(EvidencedField[str]):
    @field_validator("value")
    @classmethod
    def _iso_code(cls, v: str | None) -> str | None:
        if v is None:
            return None
        code = v.strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError(f"currency must be a 3-letter code, got {v!r}")
        return code


class AmountField(EvidencedField[Decimal]):
    @field_validator("value")
    @classmethod
    def _finite(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and not v.is_finite():
            raise ValueError("amount must be finite")
        return v


class TaxField(AmountField):
    included_in_total: bool | None = None


class POReferenceField(EvidencedField[str]):
    """explicit=False means the PO was inferred and must be matched by other signals."""

    explicit: bool | None = None


class ExtractedLineItem(LLMModel):
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    page: int | None = Field(default=None, ge=1)
    source_text: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("quantity", "unit_price", "amount")
    @classmethod
    def _finite(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and not v.is_finite():
            raise ValueError("number must be finite")
        return v


class DocumentQuality(LLMModel):
    type: Literal["scanned", "native"] | None = None
    issues: list[str] = Field(default_factory=list)

    @field_validator("issues", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return [] if v is None else v


class ExtractedInvoice(LLMModel):
    vendor_name: EvidencedField[str] = Field(default_factory=EvidencedField[str])
    invoice_number: EvidencedField[str] = Field(default_factory=EvidencedField[str])
    invoice_date: EvidencedField[date] = Field(default_factory=EvidencedField[date])
    currency: CurrencyField = Field(default_factory=CurrencyField)
    po_reference: POReferenceField = Field(default_factory=POReferenceField)
    subtotal: AmountField = Field(default_factory=AmountField)
    tax: TaxField = Field(default_factory=TaxField)
    total: AmountField = Field(default_factory=AmountField)
    line_items: list[ExtractedLineItem] = Field(default_factory=list)
    document_quality: DocumentQuality = Field(default_factory=DocumentQuality)
    extraction_notes: str | None = None

    @field_validator("line_items", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return [] if v is None else v
