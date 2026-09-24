"""RunContext and StageResult (SPEC 6.2). Internal models: unknown keys are rejected."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.enums import Decision, StageStatus
from app.models.audit import AuditEvent
from app.models.extraction import ExtractedInvoice
from app.models.rules import RuleResult


class POCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_id: int
    po_number: str = Field(min_length=1)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)


class RunContext(BaseModel):
    """Accumulates state through the pipeline. Mutable; assignments are re-validated."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    file_hash: str | None = None
    started_at: datetime | None = None
    extracted: ExtractedInvoice | None = None
    matched_po: POCandidate | None = None
    candidates: list[POCandidate] = Field(default_factory=list)
    rule_results: list[RuleResult] = Field(default_factory=list)
    decision: Decision | None = None


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str = Field(min_length=1)
    status: StageStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    events: list[AuditEvent] = Field(default_factory=list)  # emitted live over SSE and persisted
