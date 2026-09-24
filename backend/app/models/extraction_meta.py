"""System-side records of ingest and extraction (never produced by the LLM): extra="forbid"."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FailureKind = Literal["vendor_side", "system_side"]
ExtractionPath = Literal["vision_only", "text_and_vision", "text_only", "none"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextLayerInfo(_Strict):
    present: bool = False
    usable: bool = False
    reason: str = "no_text_layer"        # usable | no_text_layer | too_little_text | garbled_text
    chars_by_page: list[int] = Field(default_factory=list)
    pages_without_text: list[int] = Field(default_factory=list)   # 1-based
    truncated_pages: list[int] = Field(default_factory=list)      # 1-based pages cut at text_max_chars_per_page
    wordlike_ratio: float | None = None


class PageImageInfo(_Strict):
    number: int = Field(ge=1)            # 1-based
    path: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    media_type: Literal["image/png", "image/jpeg"]
    blank: bool = False


class IngestInfo(_Strict):
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str
    original_name: str                   # display only; the stored copy has a generated name
    run_dir: str
    original_path: str
    pages_total: int = Field(default=0, ge=0)
    pages_processed: int = Field(default=0, ge=0)
    truncated: bool = False              # pages beyond max_pages were not processed
    pages: list[PageImageInfo] = Field(default_factory=list)
    text_paths: list[str | None] = Field(default_factory=list)   # per processed page; None = no text layer
    text_layer: TextLayerInfo = Field(default_factory=TextLayerInfo)
    issues: list[str] = Field(default_factory=list)              # e.g. blank_page:2, password_protected
    failure_kind: FailureKind | None = None
    failure_code: str | None = None      # stable: password_protected, blank_document, corrupt_pdf, ...
    failure_reason: str | None = None


class LLMCallRecord(_Strict):
    attempt: int
    tokens_in: int = 0                   # all input tokens (uncached + cache read + cache write)
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    stop_reason: str | None = None
    request_id: str | None = None
    error_code: str | None = None        # set when the attempt failed


class ExtractionMeta(_Strict):
    path: ExtractionPath = "none"
    mode_requested: str = "auto"
    model: str | None = None
    prompt_version: str = ""
    structured_output: str | None = None   # json_schema | prompt_json
    pages_processed: int = 0
    truncated: bool = False
    attempts: int = 0
    schema_repair_used: bool = False
    degraded: bool = False
    failure_kind: FailureKind | None = None
    failure_code: str | None = None      # stable code, e.g. timeout, cost_ceiling, config, password_protected
    failure_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    thinking_mode: str | None = None
    effort: str | None = None
    param_fallback: str | None = None
    calls: list[LLMCallRecord] = Field(default_factory=list)
    grounding: dict[str, int] = Field(default_factory=dict)      # status -> count (filled by the grounding check)
    injection_suspected: bool = False
    text_layer_usable: bool = False
