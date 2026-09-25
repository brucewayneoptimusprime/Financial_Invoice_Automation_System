"""What the PO form posts, and what validation reports. Values arrive as the person typed them (strings); `validate.py` parses
them so a problem comes back as a named field message rather than a schema error."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class POLineIn(_In):
    description: str | None = Field(default=None, max_length=500)
    quantity: str | None = Field(default=None, max_length=40)
    unit_price: str | None = Field(default=None, max_length=40)
    amount: str | None = Field(default=None, max_length=40)


class NewVendorIn(_In):
    """A vendor created while entering a PO. There is deliberately no status field: it is always `new` (owner decision 2)."""
    name: str = Field(min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=60)
    country: str | None = Field(default=None, max_length=60)


class POCreate(_In):
    po_number: str | None = Field(default=None, max_length=64)
    vendor_id: int | None = None
    currency: str | None = Field(default=None, max_length=10)
    total: str | None = Field(default=None, max_length=40)
    issued_date: str | None = Field(default=None, max_length=20)
    lines: list[POLineIn] = Field(default_factory=list, max_length=1000)


class SaveRequest(_In):
    po: POCreate
    new_vendor: NewVendorIn | None = None
    draft_id: str | None = Field(default=None, max_length=64)


class ValidateRequest(_In):
    po: POCreate
    new_vendor: NewVendorIn | None = None


@dataclass(frozen=True)
class POIssue:
    field: str                          # "po_number", "vendor", "total", "lines[2].amount", ...
    level: Literal["error", "warning"]
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"field": self.field, "level": self.level, "code": self.code, "message": self.message}


@dataclass
class ParsedLine:
    line_no: int
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount_minor: int | None


@dataclass
class ParsedPO:
    """The PO after validation, ready for `save_po` (only built when there are no blocking issues)."""
    po_number: str
    vendor_id: int | None               # None when a new vendor is created in the same transaction
    currency: str
    total_minor: int
    issued_date: str | None
    lines: list[ParsedLine] = field(default_factory=list)
