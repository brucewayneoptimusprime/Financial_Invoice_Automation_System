"""Central configuration. Values here are defaults; environment / .env override them.

Model name, confidence threshold, tolerance defaults and the severity order live here, not in
code paths. The Claude API key is only ever read from the ANTHROPIC_API_KEY environment variable.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enums import Decision

ROOT_DIR = Path(__file__).resolve().parents[2]

# The one place the decision ordering lives (SPEC section 4). Whether request_info should
# outrank reject is an open question: change the numbers here and nowhere else.
DEFAULT_DECISION_SEVERITY: dict[str, int] = {
    Decision.APPROVE.value: 0,
    Decision.REVIEW.value: 1,
    Decision.REQUEST_INFO.value: 2,
    Decision.REJECT.value: 3,
}

# Fields that must be present for an invoice to be processable (drives the completeness rule).
DEFAULT_REQUIRED_FIELDS: list[str] = [
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "currency",
    "total",
]

# Rules whose `enabled=false` the engine ignores (it records an info event saying so). Their params
# stay editable. Only a human via settings may edit builtin rules; nl / LLM paths may only ADD rules.
LOCKED_RULE_IDS: frozenset[str] = frozenset({"r_duplicate_exact", "r_vendor_status"})


DEFAULT_LEGAL_SUFFIXES: tuple[str, ...] = (
    "ltd", "limited", "inc", "incorporated", "llc", "llp", "lp", "plc", "corp", "corporation", "co", "company",
    "gmbh", "ag", "sa", "srl", "bv", "nv", "pty", "pvt", "pte",
)


class MatchConfig(BaseModel):
    """Vendor resolution and PO candidate scoring. Starting values, tuned by tests on hand-written data -
    never by a specific vendor or invoice. Everything here is a config value, not a code path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # vendor resolution
    legal_suffixes: tuple[str, ...] = DEFAULT_LEGAL_SUFFIXES  # generic filler tokens dropped before comparing names
    vendor_fuzzy_min: float = Field(default=0.85, ge=0.0, le=1.0)      # min similarity for a fuzzy vendor match
    vendor_ambiguity_margin: float = Field(default=0.05, ge=0.0, le=1.0)

    # PO candidate score = sum of weight * signal (each signal is 0..1); weights sum to 1
    weight_reference: float = Field(default=0.40, ge=0.0, le=1.0)
    weight_vendor: float = Field(default=0.25, ge=0.0, le=1.0)
    weight_amount: float = Field(default=0.20, ge=0.0, le=1.0)
    weight_lines: float = Field(default=0.15, ge=0.0, le=1.0)

    # reference signal
    reference_fuzzy_min: float = Field(default=0.80, ge=0.0, le=1.0)
    reference_fuzzy_factor: float = Field(default=0.6, ge=0.0, le=1.0)    # a fuzzy reference is worth at most this
    reference_contained_score: float = Field(default=0.9, ge=0.0, le=1.0)  # 'PO-1001' vs '1001'
    reference_contained_min_len: int = Field(default=3, ge=1)
    inferred_reference_factor: float = Field(default=0.5, ge=0.0, le=1.0)  # reference not marked explicit

    # line-overlap signal
    line_desc_min: float = Field(default=0.6, ge=0.0, le=1.0)
    line_desc_weight: float = Field(default=0.7, ge=0.0, le=1.0)          # rest of a line's score is unit-price agreement
    candidate_line_min: float = Field(default=0.5, ge=0.0, le=1.0)        # line overlap that alone makes a PO a candidate

    # decision thresholds
    min_score: float = Field(default=0.5, ge=0.0, le=1.0)                  # top score needed for a confident match
    ambiguity_margin: float = Field(default=0.10, ge=0.0, le=1.0)          # top-two gap below this is ambiguous...
    ambiguity_min_score: float = Field(default=0.4, ge=0.0, le=1.0)        # ...if the runner-up scores at least this
    max_candidates: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "MatchConfig":
        total = self.weight_reference + self.weight_vendor + self.weight_amount + self.weight_lines
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"match weights must sum to 1.0, got {total}")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=("settings_",),
    )

    # LLM
    anthropic_api_key: SecretStr | None = None
    model_name: str = "claude-sonnet-5"

    # Defaults for the runtime `settings` table (non-rule values only)
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)

    # Defaults used to seed the builtin amount-tolerance rule's params
    tolerance_pct: float = Field(default=2.0, ge=0.0)
    tolerance_abs: float = Field(default=50.0, ge=0.0)

    required_fields: list[str] = Field(default_factory=lambda: list(DEFAULT_REQUIRED_FIELDS))
    decision_severity: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_DECISION_SEVERITY))

    # Rule param defaults (seeded into builtin rules; the DB rows are the source of truth afterwards)
    tolerance_mode: Literal["lesser_of", "greater_of"] = "lesser_of"  # allowance = min / max of (pct of balance, abs)
    arithmetic_rounding_per_term: float = Field(default=0.01, ge=0.0)  # rounding allowance per summed/multiplied term
    duplicate_fuzzy_days: int = Field(default=7, ge=0)  # near-duplicate window between invoice dates
    duplicate_fuzzy_amount_tolerance: float = Field(default=0.0, ge=0.0)  # abs amount difference still "same amount"
    duplicate_counted_statuses: list[str] = Field(default_factory=lambda: ["approved", "in_review", "pending"])

    match: MatchConfig = Field(default_factory=MatchConfig)

    # Engine
    locked_rule_ids: frozenset[str] = LOCKED_RULE_IDS
    engine_floor_severity: int = 1  # severity the engine floor forces (must be a non-approve severity)
    amount_compare_field: Literal["total", "subtotal"] = "total"  # which invoice amount is compared to the PO balance

    # Paths
    db_path: Path = ROOT_DIR / "data" / "app.db"
    seed_path: Path = ROOT_DIR / "data" / "seed.json"

    @field_validator("decision_severity")
    @classmethod
    def _check_severity(cls, v: dict[str, int]) -> dict[str, int]:
        if set(v) != {d.value for d in Decision}:
            raise ValueError("decision_severity must define exactly the four decisions")
        if v[Decision.APPROVE.value] != 0:
            raise ValueError("approve must have severity 0")
        if any(sev <= 0 for name, sev in v.items() if name != Decision.APPROVE.value):
            raise ValueError("every non-approve decision must have severity > 0")
        if len(set(v.values())) != len(v):
            raise ValueError("decision severities must be unique (severity -> decision must be unambiguous)")
        return v

    @model_validator(mode="after")
    def _check_floor_severity(self) -> "Settings":
        allowed = {s for name, s in self.decision_severity.items() if name != Decision.APPROVE.value}
        if self.engine_floor_severity not in allowed:
            raise ValueError(f"engine_floor_severity must be one of {sorted(allowed)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
