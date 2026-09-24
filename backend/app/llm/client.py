"""The ONLY module that talks to the Anthropic API.

  LLMClient        Protocol: complete(LLMRequest) -> LLMResponse. Tests inject fakes; the eval can replay.
  AnthropicClient  the real client (official SDK; the SDK owns transport retries and backoff).
  MeteredClient    wraps any LLMClient with price lookup, cost ceilings and per-call accounting.
  build_llm_client the factory: a missing key is a clear LLMConfigError, never a crash.

The API key is read via settings only, held only inside the SDK client, and scrubbed from every message
this module produces.
"""
import base64
import logging
import re
import time
from dataclasses import replace
from typing import Any

from app.config import Settings, get_settings
from app.llm.budget import CostTracker, get_session_tracker
from app.llm.errors import (
    LLMAuthError, LLMBadRequestError, LLMConfigError, LLMError, LLMSchemaError, LLMTimeoutError, LLMTransientError,
)
from app.llm.pricing import cost_usd, price_for, worst_case_cost
from app.llm.types import (  # noqa: F401 - re-exported
    LLMClient, LLMPart, LLMRequest, LLMResponse, LLMUsage, image_part, text_part,
)

logger = logging.getLogger("app.llm")

_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


# ----------------------------------------------------------------------------------------- real client

_SCHEMA_ERROR_HINTS = ("grammar", "schema", "union types", "structured output", "output_config")

SCHEMA_REJECTED_ADVICE = (
    "Fix: set LLM_STRUCTURED_OUTPUT=prompt_json in the .env file (the schema is then sent as prompt text instead of a "
    "strict grammar), or run `python -m app.llm.probe --schema --all` to test both modes.")


MISSING_KEY_MESSAGE = (
    "ANTHROPIC_API_KEY is not set. Add it to the .env file in the project root (ANTHROPIC_API_KEY=...) or export it "
    "in your shell. No LLM calls can be made until then; ingest and the rules engine still work."
)


class AnthropicClient:
    def __init__(self, settings: Settings | None = None, sdk_client: Any = None):
        self._settings = settings or get_settings()
        self._key = self._settings.api_key_value()      # kept only so messages can be scrubbed
        self._thinking = self._settings.llm_thinking
        if sdk_client is not None:
            self._sdk = sdk_client
        else:
            if not self._key:
                raise LLMConfigError(MISSING_KEY_MESSAGE)
            import anthropic

            self._sdk = anthropic.Anthropic(
                api_key=self._key, timeout=self._settings.llm_timeout_s, max_retries=self._settings.llm_max_retries)

    def __repr__(self) -> str:
        return f"AnthropicClient(thinking={self._thinking!r}, effort={self._settings.llm_effort!r})"

    @property
    def thinking_mode(self) -> str:
        return self._thinking

    # ---- helpers
    def _scrub(self, text: str, limit: int = 300) -> str:
        if self._key:
            text = text.replace(self._key, "[redacted]")
        return _KEY_PATTERN.sub("[redacted]", text)[:limit]

    def _kwargs(self, request: LLMRequest, thinking: str) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for part in request.parts:
            if part.kind == "text":
                content.append({"type": "text", "text": part.text})
            else:
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": part.media_type,
                    "data": base64.standard_b64encode(part.data).decode("ascii")}})
        system: Any = request.system
        if request.cache_system:
            system = [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}]
        output_config: dict[str, Any] = {"effort": self._settings.llm_effort}
        if request.schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.schema}
        kwargs: dict[str, Any] = {
            "model": request.model, "max_tokens": request.max_output_tokens, "system": system,
            "messages": [{"role": "user", "content": content}], "output_config": output_config,
        }
        if thinking == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        return kwargs

    def _map_error(self, exc: Exception) -> LLMError | None:
        import anthropic

        if isinstance(exc, anthropic.APITimeoutError):
            return LLMTimeoutError("The LLM request timed out (after retries). Try again, or raise LLM_TIMEOUT_S.")
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMTransientError("Could not reach the Anthropic API (network problem, after retries).")
        if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return LLMAuthError("The API rejected the API key or its permissions (HTTP 401/403). Check ANTHROPIC_API_KEY.")
        if isinstance(exc, anthropic.RateLimitError):
            return LLMTransientError("The API rate limit was still exceeded after retries.")
        if isinstance(exc, anthropic.APIStatusError):
            status = getattr(exc, "status_code", 0) or 0
            detail = self._scrub(getattr(exc, "message", "") or str(exc))
            if status >= 500:
                return LLMTransientError(f"The API returned a server error (HTTP {status}) after retries.")
            if status == 400 and any(h in detail.lower() for h in _SCHEMA_ERROR_HINTS):
                return LLMSchemaError(f"The API rejected the structured-output schema (HTTP 400): {detail} {SCHEMA_REJECTED_ADVICE}")
            return LLMBadRequestError(f"The API rejected the request (HTTP {status}): {detail}")
        return None

    def _create(self, request: LLMRequest, thinking: str) -> Any:
        return self._sdk.messages.create(**self._kwargs(request, thinking))

    # ---- the call
    def complete(self, request: LLMRequest) -> LLMResponse:
        import anthropic

        started = time.perf_counter()
        mode = self._thinking
        fallback = reason = None
        try:
            try:
                message = self._create(request, mode)
            except anthropic.BadRequestError as exc:
                text = (getattr(exc, "message", "") or str(exc)).lower()
                if mode == "disabled" and ("thinking" in text or "effort" in text):
                    reason = self._scrub(getattr(exc, "message", "") or str(exc))
                    logger.warning("API rejected thinking=disabled with effort=%s (%s); retrying with thinking omitted",
                                   self._settings.llm_effort, reason)
                    self._thinking, mode, fallback = "omit", "omit", "thinking_omitted"
                    message = self._create(request, mode)
                else:
                    raise
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            mapped = self._map_error(exc)
            if mapped is None:
                raise
            raise mapped from None

        text = "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", None) == "text")
        u = message.usage
        usage = LLMUsage(
            input_tokens=getattr(u, "input_tokens", 0) or 0, output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0)
        return LLMResponse(
            text=text, usage=usage, stop_reason=getattr(message, "stop_reason", None),
            model=getattr(message, "model", request.model), request_id=getattr(message, "_request_id", None),
            latency_ms=int((time.perf_counter() - started) * 1000), thinking_mode=mode,
            effort=self._settings.llm_effort, param_fallback=fallback, param_fallback_reason=reason)


# --------------------------------------------------------------------------------------- metering

class MeteredClient:
    """Adds price lookup, ceilings and accounting around any LLMClient."""

    def __init__(self, inner: LLMClient, tracker: CostTracker, prices: dict):
        self.inner, self.tracker, self.prices = inner, tracker, prices

    def __repr__(self) -> str:
        return f"MeteredClient({self.inner!r})"

    def complete(self, request: LLMRequest) -> LLMResponse:
        price = price_for(request.model, self.prices)                    # PriceNotConfigured before any call
        reservation = self.tracker.reserve(request.run_id, worst_case_cost(request, price))  # CostCeilingExceeded
        try:
            response = self.inner.complete(request)
        except BaseException:
            self.tracker.release(reservation)
            raise
        cost = cost_usd(response.usage, price)
        self.tracker.settle(reservation, cost)
        logger.info(
            "llm_call purpose=%s model=%s run=%s in=%d out=%d cache_r=%d cache_w=%d cost=$%.6f latency_ms=%d stop=%s request_id=%s",
            request.purpose, response.model, request.run_id, response.usage.input_tokens, response.usage.output_tokens,
            response.usage.cache_read_tokens, response.usage.cache_write_tokens, cost, response.latency_ms,
            response.stop_reason, response.request_id)
        return replace(response, cost_usd=cost)


def build_llm_client(settings: Settings | None = None, tracker: CostTracker | None = None,
                     sdk_client: Any = None) -> MeteredClient:
    """The real client, metered. Raises LLMConfigError (clear message) if the API key is missing."""
    settings = settings or get_settings()
    inner = AnthropicClient(settings, sdk_client=sdk_client)
    return MeteredClient(inner, tracker or get_session_tracker(), settings.llm_prices)
