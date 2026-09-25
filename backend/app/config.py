"""Central configuration. Values here are defaults; environment / .env override them.

Model name, confidence threshold, tolerance defaults and the severity order live here, not in
code paths. The Claude API key is only ever read from the ANTHROPIC_API_KEY environment variable.
"""
from functools import lru_cache
from pathlib import Path
from decimal import Decimal
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


class ModelPrice(BaseModel):
    """USD per million tokens for one model, plus prompt-cache multipliers. Prices live in config, never in code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    cache_read_mult: Decimal = Field(default=Decimal("0.1"), ge=0)    # cache reads bill at this multiple of input
    cache_write_mult: Decimal = Field(default=Decimal("1.25"), ge=0)  # cache writes bill at this multiple of input


DEFAULT_LLM_PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-5": ModelPrice(input_per_mtok=Decimal("2.00"), output_per_mtok=Decimal("10.00")),
}

DEFAULT_CURRENCY_SYMBOL_MAP: dict[str, str] = {
    "$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "Rs": "INR", "Rs.": "INR",
}


# Currency NAMES as words on a page ("Rupees Four Thousand only") -> ISO code. Consulted by the grounding check only, so a
# currency read correctly from words is not called a mismatch. Deliberately small; a bare "dollars" follows the "$" mapping.
DEFAULT_CURRENCY_NAME_MAP: dict[str, str] = {
    "rupee": "INR", "rupees": "INR", "indian rupee": "INR", "indian rupees": "INR",
    "dollar": "USD", "dollars": "USD", "us dollar": "USD", "us dollars": "USD", "united states dollar": "USD",
    "united states dollars": "USD", "australian dollar": "AUD", "australian dollars": "AUD",
    "canadian dollar": "CAD", "canadian dollars": "CAD",
    "euro": "EUR", "euros": "EUR", "pound": "GBP", "pounds": "GBP", "pound sterling": "GBP", "pounds sterling": "GBP",
    "yen": "JPY", "swiss franc": "CHF", "swiss francs": "CHF",
}


class GroundingCaps(BaseModel):
    """Confidence ceilings applied by the grounding check (effective = min(current confidence, cap); never raised)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    no_source: float = Field(default=0.50, ge=0.0, le=1.0)        # a value with no source_text
    value_mismatch: float = Field(default=0.30, ge=0.0, le=1.0)   # the value disagrees with its own source_text
    fuzzy: float = Field(default=0.75, ge=0.0, le=1.0)            # source_text only approximately on the page
    value_present: float = Field(default=0.85, ge=0.0, le=1.0)    # snippet not found but the VALUE is on the page
    not_found: float = Field(default=0.40, ge=0.0, le=1.0)        # neither snippet nor value on the page
    fuzzy_min_similarity: float = Field(default=0.90, ge=0.0, le=1.0)


# Deterministic scan for text addressed to an AI reader rather than to the accounts-payable clerk.
DEFAULT_INJECTION_PATTERNS: tuple[str, ...] = (
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?|messages?)",
    r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|your)\s+(?:instructions?|prompts?|rules?)",
    r"forget\s+(?:all\s+|everything\s+|your\s+)?(?:previous|prior|above|earlier)?\s*(?:instructions?|rules?)",
    r"you\s+are\s+now\s+(?:a|an|the|in)\b",
    r"\bsystem\s*(?:prompt|message|instruction)s?\b",
    r"\b(?:as|to)\s+(?:an?\s+)?(?:ai|llm|language\s+model|assistant|chatbot)\b.{0,40}\b(?:must|should|please|approve|ignore)\b",
    r"\b(?:auto[-\s]?)?approve\s+(?:this|the)\s+(?:invoice|payment|document)\b",
    r"\b(?:mark|set|treat)\s+(?:this|the)\s+(?:invoice|document)\s+as\s+(?:approved|paid|verified|valid)\b",
    r"\bdo\s+not\s+(?:flag|escalate|report|mention)\b",
    r"\b(?:override|bypass)\s+(?:the\s+)?(?:rules?|checks?|validation|controls?)\b",
    r"<\s*/?\s*(?:page_text|system|instructions?)\b",
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
    # A printed unit price is rounded to the cent, so quantity x unit price can differ from the amount by up to half a
    # cent PER UNIT (4 x 461.48 = 1845.92 for a printed 1845.94). Extra allowance per unit on the line-math check only.
    arithmetic_unit_price_rounding: float = Field(default=0.005, ge=0.0)
    duplicate_fuzzy_days: int = Field(default=7, ge=0)  # near-duplicate window between invoice dates
    duplicate_fuzzy_amount_tolerance: float = Field(default=0.0, ge=0.0)  # abs amount difference still "same amount"
    duplicate_counted_statuses: list[str] = Field(default_factory=lambda: ["approved", "in_review", "pending"])

    match: MatchConfig = Field(default_factory=MatchConfig)

    # LLM (M2). The API key is read ONLY from ANTHROPIC_API_KEY (above); an empty value counts as unset.
    llm_timeout_s: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)            # SDK-level retries (429/5xx/timeouts/connection)
    llm_max_output_tokens: int = Field(default=4096, ge=1)
    llm_thinking: Literal["disabled", "omit"] = "disabled"   # on Sonnet 5, omitting `thinking` means adaptive thinking
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_cache_system_prompt: bool = False
    # "json_schema": send a strict JSON schema (grammar-constrained output). "prompt_json": send NO schema to the
    # API; the prompt describes the JSON shape (schema included as text) and the reply is parsed and validated here.
    llm_structured_output: Literal["json_schema", "prompt_json"] = "json_schema"
    schema_repair_retries: int = Field(default=1, ge=0)
    llm_prices: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_LLM_PRICES))
    cost_ceiling_per_run_usd: Decimal = Field(default=Decimal("0.25"), ge=0)
    cost_ceiling_per_session_usd: Decimal = Field(default=Decimal("5.00"), ge=0)  # "session" = one process

    # Ingest (M2)
    allowed_media_types: tuple[str, ...] = ("application/pdf", "image/png", "image/jpeg")
    max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    max_pages: int = Field(default=10, ge=1)
    runs_dir: Path = ROOT_DIR / "data" / "runs"
    render_max_side_px: int = Field(default=1568, ge=64)
    render_dpi: int = Field(default=150, ge=36)
    text_min_chars_per_page: int = Field(default=40, ge=0)
    text_min_wordlike_ratio: float = Field(default=0.6, ge=0.0, le=1.0)
    text_max_chars_per_page: int = Field(default=15000, ge=100)

    # Extraction (M2)
    extraction_mode: Literal["auto", "vision", "text_and_vision", "text"] = "auto"
    currency_symbol_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_CURRENCY_SYMBOL_MAP))
    # A currency read from a bare symbol ($, EUR sign...) is a mapping, not a reading: its effective confidence is this
    # value whatever the model said (the model's raw score is kept in model_confidence). See SPEC section 11.
    currency_name_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_CURRENCY_NAME_MAP))
    currency_symbol_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    grounding: GroundingCaps = Field(default_factory=GroundingCaps)
    injection_patterns: tuple[str, ...] = DEFAULT_INJECTION_PATTERNS

    # Pipeline (M3): what may be said to a vendor. rule id -> the kind of request it becomes in a vendor email. Rules not listed are
    # internal (vendor status, PO balance, near-duplicates, ...): they explain the decision to us and never appear in a vendor email.
    vendor_facing_rules: dict[str, str] = Field(default_factory=lambda: {
        "r_required_fields": "missing_fields", "r_extraction_confidence": "unclear_fields", "r_po_found": "po_reference",
        "r_arithmetic": "arithmetic", "r_currency_mismatch": "currency", "r_document_type": "document_type",
        "r_duplicate_exact": "duplicate"})
    # rule id -> outcome keys that mean the vendor must NOT be emailed (an internal notification is drafted instead)
    no_vendor_email_outcomes: dict[str, list[str]] = Field(default_factory=lambda: {"r_vendor_status": ["blocked"]})
    review_reason_max_chars: int = Field(default=500, ge=50)
    # Explainer and drafter (LLM roles): None = the extraction model. Same client, same cost ceilings as extraction.
    explain_with_llm: bool = True
    draft_with_llm: bool = True
    explainer_model: str | None = None
    drafter_model: str | None = None
    explainer_max_output_tokens: int = Field(default=700, ge=100)
    drafter_max_output_tokens: int = Field(default=900, ge=100)
    explanation_max_sentences: int = Field(default=8, ge=2)
    draft_max_words: int = Field(default=180, ge=40)

    # Engine
    locked_rule_ids: frozenset[str] = LOCKED_RULE_IDS
    engine_floor_severity: int = 1  # severity the engine floor forces (must be a non-approve severity)
    amount_compare_field: Literal["total", "subtotal"] = "total"  # which invoice amount is compared to the PO balance

    # Paths
    db_path: Path = ROOT_DIR / "data" / "app.db"
    seed_path: Path = ROOT_DIR / "data" / "seed.json"                 # the M0 placeholder seed (used by the M0/M1 tests)
    demo_seed_path: Path = ROOT_DIR / "data" / "seed_demo.json"       # the M3 demo dataset (hand-written, built around the samples)

    # API and live run view (M4). Local only: the server binds to localhost and allows the Vite dev origin.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    api_upload_dir: Path = ROOT_DIR / "data" / "uploads"              # temporary upload copies, removed after each run
    api_busy_timeout_ms: int = Field(default=5000, ge=0)
    sse_poll_ms: int = Field(default=250, ge=10)
    sse_heartbeat_s: float = Field(default=15.0, gt=0)
    api_recent_runs_max: int = Field(default=100, ge=1)

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

    def api_key_value(self) -> str | None:
        """The API key, or None if unset/blank. The only place the secret is unwrapped; never log the result."""
        if self.anthropic_api_key is None:
            return None
        value = self.anthropic_api_key.get_secret_value().strip()
        return value or None

    @model_validator(mode="after")
    def _check_floor_severity(self) -> "Settings":
        allowed = {s for name, s in self.decision_severity.items() if name != Decision.APPROVE.value}
        if self.engine_floor_severity not in allowed:
            raise ValueError(f"engine_floor_severity must be one of {sorted(allowed)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
