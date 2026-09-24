"""Fakes for the LLM layer: a scripted LLMClient, and a fake Anthropic SDK that can raise the real SDK exceptions."""
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2

from app.llm.client import LLMRequest, LLMResponse, LLMUsage

_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _resp(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=_REQ, json={"error": {"type": "x", "message": "boom"}})


def sdk_error(kind: str, message: str = "boom") -> Exception:
    return {
        "bad_request": lambda: anthropic.BadRequestError(message, response=_resp(400), body=None),
        "auth": lambda: anthropic.AuthenticationError(message, response=_resp(401), body=None),
        "permission": lambda: anthropic.PermissionDeniedError(message, response=_resp(403), body=None),
        "rate_limit": lambda: anthropic.RateLimitError(message, response=_resp(429), body=None),
        "server": lambda: anthropic.InternalServerError(message, response=_resp(500), body=None),
        "too_large": lambda: anthropic.RequestTooLargeError(message, response=_resp(413), body=None),
        "timeout": lambda: anthropic.APITimeoutError(request=_REQ),
        "connection": lambda: anthropic.APIConnectionError(request=_REQ),
    }[kind]()


def sdk_message(text: str = '{"ok": true}', *, input_tokens: int = 100, output_tokens: int = 20, cache_read: int = 0,
                cache_write: int = 0, stop_reason: str = "end_turn", model: str = "claude-sonnet-5",
                request_id: str = "req_test_1") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write),
        stop_reason=stop_reason, model=model, _request_id=request_id)


class FakeSDK:
    """Stands in for `anthropic.Anthropic`: `.messages.create(**kwargs)` pops scripted messages/exceptions."""

    def __init__(self, *script: Any):
        self.script = list(script)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else sdk_message()
        if isinstance(item, Exception):
            raise item
        return item


class FakeLLMClient:
    """A scripted LLMClient: each call pops the next LLMResponse (or raises the next exception)."""

    def __init__(self, *script: Any):
        self.script = list(script)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        item = self.script.pop(0) if self.script else ok_response()
        if isinstance(item, Exception):
            raise item
        return item


def ok_response(text: str = '{"ok": true}', *, input_tokens: int = 100, output_tokens: int = 20, stop_reason: str = "end_turn",
                model: str = "claude-sonnet-5", **kw: Any) -> LLMResponse:
    return LLMResponse(text=text, usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens,
                                                 cache_read_tokens=kw.pop("cache_read", 0), cache_write_tokens=kw.pop("cache_write", 0)),
                       stop_reason=stop_reason, model=model, request_id="req_fake", latency_ms=5, **kw)
