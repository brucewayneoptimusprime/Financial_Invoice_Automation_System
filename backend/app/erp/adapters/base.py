"""The contract every ERP adapter meets. An adapter maps ONE ERP format into `FeedPO`: the PO form's own input (`POCreate`, string
values, as a person would type them) plus the ERP facts we have no column for. Everything after it (classification, the form's
validation, saving through save_po, the API, the UI) is shared, so a second ERP needs only a second adapter.

Adding an adapter: write `parse(doc) -> list[FeedPO]`, list its envelope `format` values, register it in adapters/__init__.py, and add
a fixture feed with mapping tests. Structural problems (a missing block, a value of the wrong JSON type, a negative quantity) go in
`problems` with the form's issue shape; field rules the form already has (required, amounts, dates) are left to `validate_po`.
"""
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.po.models import POCreate, POIssue


@dataclass
class FeedVendor:
    name: str | None
    tax_id: str | None
    country: str | None
    address: dict[str, Any] | None = None


@dataclass
class FeedPO:
    index: int                                   # position in the feed (0-based)
    create: POCreate
    vendor: FeedVendor
    buyer_reference: str | None = None
    erp_status: str | None = None
    erp_line_numbers: list[Any] = field(default_factory=list)
    line_uom: list[str | None] = field(default_factory=list)
    problems: list[POIssue] = field(default_factory=list)

    @property
    def po_number(self) -> str | None:
        return self.create.po_number or None


class ERPAdapter(Protocol):
    key: str
    formats: tuple[str, ...]

    def parse(self, doc: dict[str, Any], *, max_lines: int) -> list[FeedPO]: ...
