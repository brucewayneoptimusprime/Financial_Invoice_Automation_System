import base64
import logging

import pytest

from app.config import Settings
from app.llm.client import (
    MISSING_KEY_MESSAGE, AnthropicClient, LLMRequest, LLMResponse, LLMUsage, build_llm_client, image_part, text_part,
)
from app.llm.errors import (
    LLMAuthError, LLMBadRequestError, LLMConfigError, LLMRefusedError, LLMTimeoutError, LLMTransientError,
    LLMTruncatedError,
)
from tests.llm.fakes import FakeSDK, sdk_error, sdk_message

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
CANARY = "sk-ant-api03-CANARY-do-not-leak-0123456789"


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def request(**kw) -> LLMRequest:
    base = dict(system="SYSTEM PROMPT", parts=(text_part("hello"), image_part("image/png", b"\x89PNGdata")),
                model="claude-sonnet-5", max_output_tokens=4096, schema=SCHEMA, run_id="run-1")
    base.update(kw)
    return LLMRequest(**base)


def client(*script, **kw):
    sdk = FakeSDK(*script)
    return AnthropicClient(settings(**kw), sdk_client=sdk), sdk


# ------------------------------------------------------------------------------ request shape

def test_request_uses_model_tokens_thinking_disabled_effort_low_and_the_json_schema():
    c, sdk = client(sdk_message())
    c.complete(request())
    k = sdk.calls[0]
    assert k["model"] == "claude-sonnet-5" and k["max_tokens"] == 4096
    assert k["thinking"] == {"type": "disabled"}
    assert k["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert k["system"] == "SYSTEM PROMPT"
    assert "temperature" not in k and "top_p" not in k          # sampling params are rejected on Sonnet 5


def test_parts_become_text_and_base64_image_blocks_in_order():
    c, sdk = client(sdk_message())
    c.complete(request())
    content = sdk.calls[0]["messages"][0]["content"]
    assert sdk.calls[0]["messages"][0]["role"] == "user"
    assert content[0] == {"type": "text", "text": "hello"}
    assert content[1]["type"] == "image" and content[1]["source"]["media_type"] == "image/png"
    assert base64.b64decode(content[1]["source"]["data"]) == b"\x89PNGdata"


def test_model_and_limits_come_from_the_request_and_config():
    c, sdk = client(sdk_message(), llm_effort="medium")
    c.complete(request(model="claude-other", max_output_tokens=123))
    assert sdk.calls[0]["model"] == "claude-other" and sdk.calls[0]["max_tokens"] == 123
    assert sdk.calls[0]["output_config"]["effort"] == "medium"


def test_thinking_can_be_omitted_by_config():
    c, sdk = client(sdk_message(), llm_thinking="omit")
    c.complete(request())
    assert "thinking" not in sdk.calls[0]


def test_no_schema_means_no_format():
    c, sdk = client(sdk_message())
    c.complete(request(schema=None))
    assert "format" not in sdk.calls[0]["output_config"]


def test_system_prompt_caching_is_off_by_default_and_opt_in():
    c, sdk = client(sdk_message(), sdk_message())
    c.complete(request())
    c.complete(request(cache_system=True))
    assert sdk.calls[0]["system"] == "SYSTEM PROMPT"
    assert sdk.calls[1]["system"][0]["cache_control"] == {"type": "ephemeral"}


# ------------------------------------------------------------------------------ response parsing

def test_response_text_usage_and_ids_are_parsed():
    c, _ = client(sdk_message('{"ok": true}', input_tokens=1200, output_tokens=340, cache_read=50, cache_write=7, request_id="req_9"))
    r = c.complete(request())
    assert r.text == '{"ok": true}' and r.stop_reason == "end_turn" and r.request_id == "req_9"
    assert r.usage == LLMUsage(1200, 340, 50, 7) and r.usage.total_input == 1257
    assert r.thinking_mode == "disabled" and r.effort == "low" and r.param_fallback is None and r.latency_ms >= 0


def test_missing_usage_fields_default_to_zero():
    msg = sdk_message()
    msg.usage.cache_read_input_tokens = None
    msg.usage.cache_creation_input_tokens = None
    r = client(msg)[0].complete(request())
    assert r.usage.cache_read_tokens == 0 and r.usage.cache_write_tokens == 0


def test_only_text_blocks_are_concatenated():
    msg = sdk_message("A")
    msg.content = [type("B", (), {"type": "thinking", "thinking": "..."})(), msg.content[0],
                   type("T", (), {"type": "text", "text": "B"})()]
    assert client(msg)[0].complete(request()).text == "AB"


def test_ensure_usable_raises_typed_errors_for_refusal_and_truncation():
    ok = LLMResponse(text="{}", usage=LLMUsage(), stop_reason="end_turn", model="m")
    assert ok.ensure_usable() is ok
    with pytest.raises(LLMRefusedError):
        LLMResponse(text="", usage=LLMUsage(), stop_reason="refusal", model="m").ensure_usable()
    with pytest.raises(LLMTruncatedError):
        LLMResponse(text="{", usage=LLMUsage(), stop_reason="max_tokens", model="m").ensure_usable()


# ------------------------------------------------------------------------------ the key

def test_missing_key_is_a_clear_config_error_not_a_crash():
    with pytest.raises(LLMConfigError) as exc:
        AnthropicClient(settings())
    assert exc.value.message == MISSING_KEY_MESSAGE and "ANTHROPIC_API_KEY" in exc.value.message and ".env" in exc.value.message
    assert exc.value.code == "config"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_key_counts_as_missing(blank):
    with pytest.raises(LLMConfigError):
        AnthropicClient(settings(anthropic_api_key=blank))


def test_factory_reports_the_missing_key_the_same_way():
    with pytest.raises(LLMConfigError):
        build_llm_client(settings())


def test_the_key_is_read_from_settings_and_passed_to_the_sdk(monkeypatch):
    import anthropic

    captured = {}

    class Spy:
        def __init__(self, **kw):
            captured.update(kw)
            self.messages = FakeSDK().messages

    monkeypatch.setattr(anthropic, "Anthropic", Spy)
    AnthropicClient(settings(anthropic_api_key=CANARY, llm_timeout_s=42, llm_max_retries=3))
    assert captured == {"api_key": CANARY, "timeout": 42.0, "max_retries": 3}


def test_repr_never_contains_the_key():
    c = AnthropicClient(settings(anthropic_api_key=CANARY), sdk_client=FakeSDK())
    assert CANARY not in repr(c) and CANARY not in str(c)


def test_key_is_scrubbed_from_error_messages_and_logs(caplog):
    leaky = sdk_error("bad_request", f"invalid request for key {CANARY}, also sk-ant-other-SECRET99")
    c, _ = client(leaky, anthropic_api_key=CANARY)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMBadRequestError) as exc:
            c.complete(request())
    assert CANARY not in exc.value.message and "sk-ant-other-SECRET99" not in exc.value.message
    assert "[redacted]" in exc.value.message
    assert CANARY not in caplog.text and CANARY not in repr(exc.value)


# ------------------------------------------------------------------------------ error mapping

@pytest.mark.parametrize("kind,expected,fragment", [
    ("timeout", LLMTimeoutError, "timed out"),
    ("connection", LLMTransientError, "Could not reach"),
    ("rate_limit", LLMTransientError, "rate limit"),
    ("server", LLMTransientError, "server error"),
    ("auth", LLMAuthError, "ANTHROPIC_API_KEY"),
    ("permission", LLMAuthError, "permissions"),
    ("bad_request", LLMBadRequestError, "rejected the request"),
    ("too_large", LLMBadRequestError, "HTTP 413"),
])
def test_sdk_errors_map_to_typed_llm_errors(kind, expected, fragment):
    c, sdk = client(sdk_error(kind))
    with pytest.raises(expected) as exc:
        c.complete(request())
    assert fragment in exc.value.message and len(sdk.calls) == 1           # no home-grown retry loop


def test_timeout_is_not_confused_with_a_generic_connection_error():
    c, _ = client(sdk_error("timeout"))
    with pytest.raises(LLMTimeoutError):
        c.complete(request())


def test_unknown_exceptions_are_not_swallowed():
    c, _ = client(RuntimeError("programming error"))
    with pytest.raises(RuntimeError):
        c.complete(request())


# ------------------------------------------------------------------------------ thinking=disabled + effort fallback

def test_when_the_api_rejects_thinking_disabled_with_effort_the_client_falls_back_and_says_so(caplog):
    rejection = sdk_error("bad_request", "thinking: disabled is not supported together with output_config.effort")
    c, sdk = client(rejection, sdk_message())
    with caplog.at_level(logging.WARNING):
        r = c.complete(request())
    assert len(sdk.calls) == 2 and "thinking" in sdk.calls[0] and "thinking" not in sdk.calls[1]
    assert sdk.calls[1]["output_config"]["effort"] == "low"                 # effort=low is kept
    assert r.param_fallback == "thinking_omitted" and "not supported" in r.param_fallback_reason
    assert r.thinking_mode == "omit" and "retrying with thinking omitted" in caplog.text


def test_the_fallback_sticks_so_later_calls_do_not_repeat_the_failed_attempt():
    c, sdk = client(sdk_error("bad_request", "effort is incompatible with thinking"), sdk_message(), sdk_message())
    c.complete(request())
    r2 = c.complete(request())
    assert len(sdk.calls) == 3 and "thinking" not in sdk.calls[2] and r2.param_fallback is None
    assert c.thinking_mode == "omit"


def test_an_unrelated_bad_request_is_not_retried():
    c, sdk = client(sdk_error("bad_request", "image exceeds 5 MB maximum"))
    with pytest.raises(LLMBadRequestError) as exc:
        c.complete(request())
    assert len(sdk.calls) == 1 and "5 MB" in exc.value.message


def test_a_bad_request_when_thinking_is_already_omitted_is_not_retried():
    c, sdk = client(sdk_error("bad_request", "effort is not supported"), llm_thinking="omit")
    with pytest.raises(LLMBadRequestError):
        c.complete(request())
    assert len(sdk.calls) == 1


def test_if_the_fallback_attempt_also_fails_the_error_is_reported():
    c, sdk = client(sdk_error("bad_request", "thinking not supported"), sdk_error("auth"))
    with pytest.raises(LLMAuthError):
        c.complete(request())
    assert len(sdk.calls) == 2
