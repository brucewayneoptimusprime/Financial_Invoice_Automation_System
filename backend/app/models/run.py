"""RunContext and StageResult (SPEC 6.2). Internal models: unknown keys are rejected."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.facts import RunFacts
from app.enums import Decision, MatchStatus, StageStatus
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


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str = Field(min_length=1)
    status: StageStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    events: list[AuditEvent] = Field(default_factory=list)  # emitted live over SSE and persisted
