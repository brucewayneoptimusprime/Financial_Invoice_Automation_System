"""Plain data types shared by the LLM layer (no imports from the rest of app.llm except errors)."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from app.llm.errors import LLMRefusedError, LLMTruncatedError


@dataclass(frozen=True)
class LLMPart:
    kind: Literal["text", "image"]
    text: str = ""
    media_type: str = ""
    data: bytes = b""


def text_part(text: str) -> LLMPart:
    return LLMPart(kind="text", text=text)


def image_part(media_type: str, data: bytes) -> LLMPart:
    return LLMPart(kind="image", media_type=media_type, data=data)


@dataclass(frozen=True)
class LLMRequest:
    system: str
    parts: tuple[LLMPart, ...]
    model: str
    max_output_tokens: int
    schema: dict | None = None          # JSON schema for structured output
    run_id: str | None = None           # cost is tracked per run
    purpose: str = "extract"
    cache_system: bool = False


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0               # uncached input tokens
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: LLMUsage
    stop_reason: str | None
    model: str
    request_id: str | None = None
    latency_ms: int = 0
    thinking_mode: str | None = None    # "disabled" | "omit" as actually sent
    effort: str | None = None
    param_fallback: str | None = None   # e.g. "thinking_omitted" when the API refused thinking=disabled + effort
    param_fallback_reason: str | None = None
    cost_usd: Decimal | None = None     # filled in by MeteredClient

    def ensure_usable(self) -> "LLMResponse":
        """Raise a typed error for answers that cannot be trusted (usage is already accounted for)."""
        if self.stop_reason == "refusal":
            raise LLMRefusedError("The model declined to answer (stop_reason=refusal).")
        if self.stop_reason == "max_tokens":
            raise LLMTruncatedError("The answer was cut off at the output-token limit (stop_reason=max_tokens).")
        return self


class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...
