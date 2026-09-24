"""A currency read from a bare symbol is a mapping: its effective confidence comes from config, the model's own score
stays in model_confidence, and a note says so."""
import pytest

from app.config import Settings
from app.extraction.postprocess import postprocess

CFG = Settings(_env_file=None)


def cur(value, conf, source="$1,770.61"):
    return {"currency": {"value": value, "page": 1, "source_text": source, "confidence": conf}}


@pytest.mark.parametrize("symbol,code", [("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("Rs", "INR"), ("Rs.", "INR")])
def test_every_mapped_symbol_gets_the_configured_confidence(symbol, code):
    result = postprocess(cur(symbol, 0.7), CFG)
    f = result.invoice.currency
    assert f.value == code and f.confidence == 0.85 and f.model_confidence == 0.7
    assert f"currency confidence set to 0.85 from configuration (symbol-derived; the model reported 0.7)" in result.notes
    assert f"currency {symbol!r} was mapped to {code} by configuration" in result.notes
    assert "[system] currency confidence set to 0.85" in result.invoice.extraction_notes


def test_the_confidence_is_a_config_value_and_replaces_a_higher_model_score_too():
    high = postprocess(cur("$", 0.99), Settings(_env_file=None, currency_symbol_confidence=0.9)).invoice.currency
    assert high.confidence == 0.9 and high.model_confidence == 0.99
    low = postprocess(cur("$", 0.5), Settings(_env_file=None, currency_symbol_confidence=0.6)).invoice.currency
    assert low.confidence == 0.6 and low.model_confidence == 0.5


def test_the_default_symbol_confidence_is_above_the_review_threshold():
    assert CFG.currency_symbol_confidence == 0.85 > CFG.confidence_threshold


def test_a_three_letter_code_is_a_reading_and_keeps_the_models_confidence():
    f = postprocess(cur("usd", 0.7, "Amounts in USD"), CFG).invoice.currency
    assert f.value == "USD" and f.confidence == 0.7 and f.model_confidence == 0.7


def test_a_model_that_reported_no_confidence_is_respected():
    f = postprocess(cur("$", 0.0), CFG).invoice.currency
    assert f.confidence == 0.0 and f.model_confidence == 0.0


def test_an_unmapped_symbol_is_dropped_not_boosted():
    f = postprocess(cur("¥", 0.9), CFG).invoice.currency
    assert f.value is None and f.confidence == 0.0


def test_no_currency_no_note():
    result = postprocess({}, CFG)
    assert result.invoice.currency.value is None and not any("currency confidence" in n for n in result.notes)


def test_the_symbol_map_is_config():
    cfg = Settings(_env_file=None, currency_symbol_map={"kr": "SEK"})
    f = postprocess(cur("kr", 0.6), cfg).invoice.currency
    assert f.value == "SEK" and f.confidence == 0.85
