"""RunContext and StageResult (SPEC 6.2). Internal models: unknown keys are rejected."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.facts import RunFacts
from app.enums import Decision, LineMatchStatus, MatchStatus, StageStatus
from app.models.audit import AuditEvent
from app.models.extraction import ExtractedInvoice
from app.models.extraction_meta import ExtractionMeta, IngestInfo
from app.models.rules import RuleResult


class POCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_id: int
    po_number: str = Field(min_length=1)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    breakdown: dict[str, float] = Field(default_factory=dict)  # per-signal contribution to the score
    reasons: list[str] = Field(default_factory=list)


class VendorMatch(BaseModel):
    """Result of resolving the invoice's vendor name against known vendors."""

    model_config = ConfigDict(extra="forbid")

    vendor_id: int | None = None  # None = no vendor resolved
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    method: Literal["tax_id", "exact_name", "alias", "fuzzy", "none"] = "none"
    ambiguous: bool = False
    runner_up_score: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_vendor_ids: list[int] = Field(default_factory=list)  # the vendors tied with the top one (only when ambiguous)


class LineCandidate(BaseModel):
    """One PO line considered for one invoice line."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    po_line_id: int | None = None
    po_line_no: int
    score: float = Field(ge=0.0, le=1.0)
    breakdown: dict[str, float] = Field(default_factory=dict)   # weighted contribution per signal
    reasons: list[str] = Field(default_factory=list)


class InvoiceLineMatch(BaseModel):
    """The automatic line-match result for one invoice line (line-item PO consumption)."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    invoice_line_no: int = Field(ge=1)                           # 1-based index into extracted.line_items
    status: LineMatchStatus
    po_line_id: int | None = None                                # set only when status is matched
    po_line_no: int | None = None
    score: float | None = None                                   # the top candidate's score
    candidates: list[LineCandidate] = Field(default_factory=list)
    reason: str | None = None                                    # why not_evaluable / no_match


class LineMatchSet(BaseModel):
    """Line matching of an invoice against its confidently matched PO. Stored for the reviewer; changes no decision (yet)."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    po_id: int | None = None
    po_number: str | None = None
    mode: Literal["line_level", "total_only", "partial", "not_evaluable"]
    bundled_hint: bool = False
    reason: str | None = None
    lines: list[InvoiceLineMatch] = Field(default_factory=list)


class RunContext(BaseModel):
    """Accumulates state through the pipeline. Mutable; assignments are re-validated."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    file_hash: str | None = None
    started_at: datetime | None = None
    ingest: IngestInfo | None = None                    # set by the ingest stage (M2)
    extracted: ExtractedInvoice | None = None
    extraction_meta: ExtractionMeta | None = None       # set by the extract stage (M2)
    facts: RunFacts | None = None  # read-only snapshot, taken once per run by engine/loader.py
    matched_vendor: VendorMatch | None = None
    matched_po: POCandidate | None = None
    match_status: MatchStatus | None = None
    candidates: list[POCandidate] = Field(default_factory=list)
    rule_results: list[RuleResult] = Field(default_factory=list)
    decision: Decision | None = None
    line_matches: LineMatchSet | None = None            # set by the match stage when a PO was matched (schema v2)


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str = Field(min_length=1)
    status: StageStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    events: list[AuditEvent] = Field(default_factory=list)  # emitted live over SSE and persisted
