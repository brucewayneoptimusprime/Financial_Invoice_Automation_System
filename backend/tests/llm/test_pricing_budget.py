import io
import threading
from decimal import Decimal

import pytest
from PIL import Image

from app.config import DEFAULT_LLM_PRICES, ModelPrice, Settings, get_settings
from app.llm.budget import CostTracker, get_session_tracker, reset_session_tracker
from app.llm.client import LLMRequest, LLMUsage, image_part, text_part
from app.llm.errors import CostCeilingExceeded, PriceNotConfigured
from app.llm.pricing import cost_usd, estimate_input_tokens, image_tokens, price_for, worst_case_cost

D = Decimal
SONNET = DEFAULT_LLM_PRICES["claude-sonnet-5"]


def png(width, height) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


def req(parts=(text_part("hello"),), max_out=1000, **kw):
    return LLMRequest(system="sys", parts=tuple(parts), model="claude-sonnet-5", max_output_tokens=max_out, **kw)


# ------------------------------------------------------------------------------ prices and cost

def test_default_price_is_sonnet_5_at_2_and_10_dollars():
    assert SONNET.input_per_mtok == D("2.00") and SONNET.output_per_mtok == D("10.00")


def test_cost_is_exact_decimal():
    assert cost_usd(LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000), SONNET) == D("12.00")
    assert cost_usd(LLMUsage(input_tokens=10_000, output_tokens=1_500), SONNET) == D("0.035")
    assert cost_usd(LLMUsage(), SONNET) == D(0)
    assert isinstance(cost_usd(LLMUsage(input_tokens=1), SONNET), Decimal)


def test_cost_has_no_float_drift():
    total = sum((cost_usd(LLMUsage(input_tokens=3, output_tokens=7), SONNET) for _ in range(1000)), D(0))
    assert total == D("0.076")          # 1000 * (3*2 + 7*10) / 1e6, exactly


def test_cache_tokens_bill_at_their_multiples():
    usage = LLMUsage(input_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert cost_usd(usage, SONNET) == D("2.00") + D("0.20") + D("2.50")


def test_unknown_model_has_no_price():
    with pytest.raises(PriceNotConfigured) as exc:
        price_for("claude-mystery-9", DEFAULT_LLM_PRICES)
    assert "llm_prices" in exc.value.message and exc.value.code == "price_not_configured"


def test_prices_are_configurable_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_PRICES", '{"claude-sonnet-5": {"input_per_mtok": "3.5", "output_per_mtok": "17"}}')
    s = Settings(_env_file=None)
    assert s.llm_prices["claude-sonnet-5"].input_per_mtok == D("3.5")
    assert cost_usd(LLMUsage(input_tokens=1_000_000), s.llm_prices["claude-sonnet-5"]) == D("3.5")


def test_negative_prices_are_rejected():
    with pytest.raises(ValueError):
        ModelPrice(input_per_mtok=D("-1"), output_per_mtok=D("1"))


# ------------------------------------------------------------------------------ estimates

def test_image_tokens_follow_width_times_height_over_750():
    assert image_tokens(png(1000, 750)) == 1000
    assert image_tokens(png(1568, 1200)) == 2509      # ceil(1568*1200/750)


def test_unreadable_image_gets_a_fallback_estimate_not_a_crash():
    assert image_tokens(b"not an image") == 1600


def test_input_estimate_counts_system_schema_text_and_images():
    base = estimate_input_tokens(req())
    assert base >= 1
    assert estimate_input_tokens(req(parts=[text_part("x" * 3000)])) > base + 900
    assert estimate_input_tokens(req(parts=[image_part("image/png", png(1000, 750))])) >= 1000
    assert estimate_input_tokens(req(schema={"type": "object"})) > base


def test_worst_case_is_input_estimate_plus_the_full_output_cap():
    r = req(max_out=4096)
    expected = (D(estimate_input_tokens(r)) * D(2) + D(4096) * D(10)) / D(1_000_000)
    assert worst_case_cost(r, SONNET) == expected


def test_worst_case_assumes_cache_write_pricing_when_caching_is_on():
    assert worst_case_cost(req(cache_system=True), SONNET) > worst_case_cost(req(cache_system=False), SONNET)


# ------------------------------------------------------------------------------ ceilings

def test_reserve_settle_accumulates_run_and_session_totals():
    t = CostTracker(D("1.00"), D("5.00"))
    r = t.reserve("run-1", D("0.10"))
    t.settle(r, D("0.04"))
    assert t.run_spent("run-1") == D("0.04") and t.session_spent == D("0.04") and t.run_spent("run-2") == 0


def test_per_run_ceiling_blocks_the_call_before_it_is_made():
    t = CostTracker(D("0.25"), D("5.00"))
    t.settle(t.reserve("r", D("0.10")), D("0.20"))
    with pytest.raises(CostCeilingExceeded) as exc:
        t.reserve("r", D("0.10"))
    assert "Per-run" in exc.value.message and "not made" in exc.value.message and exc.value.code == "cost_ceiling"
    assert t.reserve("other-run", D("0.10"))            # another run is unaffected


def test_per_session_ceiling_is_shared_across_runs():
    t = CostTracker(D("1.00"), D("0.30"))
    t.settle(t.reserve("a", D("0.20")), D("0.20"))
    with pytest.raises(CostCeilingExceeded) as exc:
        t.reserve("b", D("0.20"))
    assert "Per-session" in exc.value.message


def test_in_flight_reservations_count_against_the_ceilings():
    t = CostTracker(D("1.00"), D("0.30"))
    t.reserve("a", D("0.20"))
    with pytest.raises(CostCeilingExceeded):
        t.reserve("b", D("0.20"))


def test_release_gives_the_reservation_back():
    t = CostTracker(D("1.00"), D("0.30"))
    r = t.reserve("a", D("0.20"))
    t.release(r)
    assert t.session_spent == 0 and t.reserve("b", D("0.20"))


def test_a_call_exactly_at_the_ceiling_is_allowed_and_one_cent_over_is_not():
    t = CostTracker(D("0.25"), D("5.00"))
    t.reserve("r", D("0.25"))
    t2 = CostTracker(D("0.25"), D("5.00"))
    with pytest.raises(CostCeilingExceeded):
        t2.reserve("r", D("0.26"))


def test_unattributed_calls_share_one_bucket():
    t = CostTracker(D("0.25"), D("5.00"))
    t.settle(t.reserve(None, D("0.10")), D("0.10"))
    assert t.run_spent(None) == D("0.10")


def test_concurrent_reservations_never_exceed_the_ceiling():
    t = CostTracker(D("100"), D("1.00"))
    granted, denied = [], []

    def worker():
        try:
            granted.append(t.reserve("r", D("0.10")))
        except CostCeilingExceeded:
            denied.append(1)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert len(granted) == 10 and len(denied) == 30


def test_session_tracker_is_a_process_singleton_built_from_settings(monkeypatch):
    monkeypatch.setenv("COST_CEILING_PER_RUN_USD", "0.5")
    monkeypatch.setenv("COST_CEILING_PER_SESSION_USD", "2")
    get_settings.cache_clear()
    reset_session_tracker()
    a = get_session_tracker()
    assert a is get_session_tracker() and a.per_run_ceiling == D("0.5") and a.per_session_ceiling == D(2)
    reset_session_tracker()
    assert get_session_tracker() is not a


def test_default_ceilings_are_25_cents_per_run_and_5_dollars_per_session():
    s = Settings(_env_file=None)
    assert s.cost_ceiling_per_run_usd == D("0.25") and s.cost_ceiling_per_session_usd == D("5.00")
