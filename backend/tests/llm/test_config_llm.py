from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings

D = Decimal


def s(**kw):
    return Settings(_env_file=None, **kw)


def test_llm_defaults_match_the_approved_plan():
    c = s()
    assert c.model_name == "claude-sonnet-5" and c.llm_thinking == "disabled" and c.llm_effort == "low"
    assert (c.llm_timeout_s, c.llm_max_retries, c.llm_max_output_tokens) == (60.0, 2, 4096)
    assert c.llm_cache_system_prompt is False and c.schema_repair_retries == 1
    assert (c.max_pages, c.max_file_bytes, c.render_max_side_px) == (10, 20 * 1024 * 1024, 1568)
    assert c.extraction_mode == "auto"


def test_currency_symbol_map_includes_rupee_forms():
    m = s().currency_symbol_map
    assert m["$"] == "USD" and m["₹"] == "INR" and m["Rs"] == "INR" and m["Rs."] == "INR"
    assert m["€"] == "EUR" and m["£"] == "GBP" and "¥" not in m         # yen stays ambiguous


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_api_key_is_unset(blank):
    assert s(anthropic_api_key=blank).api_key_value() is None


def test_api_key_value_is_stripped_and_never_in_repr():
    c = s(anthropic_api_key="  sk-ant-secret-123  ")
    assert c.api_key_value() == "sk-ant-secret-123"
    assert "secret-123" not in repr(c) and "secret-123" not in str(c) and "secret-123" not in c.model_dump_json()


@pytest.mark.parametrize("bad", [dict(llm_thinking="enabled"), dict(llm_effort="turbo"), dict(llm_timeout_s=0),
                                 dict(llm_max_output_tokens=0), dict(extraction_mode="magic"), dict(max_pages=0)])
def test_invalid_llm_settings_are_rejected(bad):
    with pytest.raises(ValidationError):
        s(**bad)


def test_settings_can_be_overridden_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_THINKING", "omit")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "2048")
    monkeypatch.setenv("MAX_PAGES", "3")
    c = Settings(_env_file=None)
    assert (c.llm_thinking, c.llm_max_output_tokens, c.max_pages) == ("omit", 2048, 3)
