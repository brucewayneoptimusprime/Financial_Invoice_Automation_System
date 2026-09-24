"""AuditEvent - one row of audit_events (SPEC sections 5 and 6.2)."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.enums import Outcome


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None  # assigned by the DB
    run_id: str | None = Field(default=None, min_length=1)  # filled in by the pipeline before persisting
    seq: int | None = Field(default=None, ge=0)  # assigned when persisted, monotonic per run
    stage: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    rule_id: str | None = None
    outcome: Outcome
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
