"""Typed LLM failures. Messages are written by us (never echoing request content or credentials)."""


class LLMError(Exception):
    """Base class. `code` is a stable machine-readable reason used in audit events and run metadata."""

    code = "llm_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class LLMConfigError(LLMError):
    """Missing or unusable configuration (most commonly: ANTHROPIC_API_KEY is not set)."""
    code = "config"


class LLMAuthError(LLMError):
    """The API rejected the credentials or permissions (401/403)."""
    code = "auth"


class LLMBadRequestError(LLMError):
    """The API rejected the request itself (400/404/413/422)."""
    code = "bad_request"


class LLMTimeoutError(LLMError):
    """The request timed out (after the SDK's own retries)."""
    code = "timeout"


class LLMTransientError(LLMError):
    """Rate limit, server error or connection failure that persisted after the SDK's own retries."""
    code = "transient"


class LLMRefusedError(LLMError):
    """The model declined to answer (stop_reason == 'refusal')."""
    code = "refused"


class LLMTruncatedError(LLMError):
    """The answer hit the output-token cap (stop_reason == 'max_tokens') so it cannot be trusted."""
    code = "max_tokens"


class CostCeilingExceeded(LLMError):
    """A per-run or per-session cost ceiling would be exceeded; the call was NOT made."""
    code = "cost_ceiling"


class PriceNotConfigured(LLMError):
    """No price for the model in config, so cost (and therefore the ceiling) cannot be enforced."""
    code = "price_not_configured"


class ReplayMiss(LLMError):
    """A replay fixture is missing for this request."""
    code = "replay_miss"
