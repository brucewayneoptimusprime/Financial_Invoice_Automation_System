import json
import logging
import threading
from decimal import Decimal

import pytest

from app.config import DEFAULT_LLM_PRICES, Settings
from app.llm import probe as probe_module
from app.llm.budget import CostTracker
from app.llm.client import AnthropicClient, LLMRequest, MeteredClient, build_llm_client, image_part, text_part
from app.llm.errors import CostCeilingExceeded, LLMError, PriceNotConfigured, ReplayMiss
from app.llm.probe import run_probe
from app.llm.replay import RecordingClient, ReplayClient, request_key
from tests.llm.fakes import FakeLLMClient, FakeSDK, ok_response, sdk_error, sdk_message

D = Decimal
CANARY = "sk-ant-api03-CANARY-do-not-leak-0123456789"


def req(**kw):
    base = dict(system="sys", parts=(text_part("hi"),), model="claude-sonnet-5", max_output_tokens=1000, run_id="run-1")
    base.update(kw)
    return LLMRequest(**base)


def metered(inner, per_run="0.25", per_session="5.00"):
    return MeteredClient(inner, CostTracker(D(per_run), D(per_session)), DEFAULT_LLM_PRICES)


# ------------------------------------------------------------------------------ metering

def test_cost_is_computed_attached_and_tracked():
    m = metered(FakeLLMClient(ok_response(input_tokens=10_000, output_tokens=1_500)))
    r = m.complete(req())
    assert r.cost_usd == D("0.035")
    assert m.tracker.run_spent("run-1") == D("0.035") and m.tracker.session_spent == D("0.035")


def test_the_ceiling_blocks_before_the_inner_client_is_called():
    inner = FakeLLMClient(ok_response())
    m = metered(inner, per_run="0.001")
    with pytest.raises(CostCeilingExceeded):
        m.complete(req(max_output_tokens=4096))
    assert inner.requests == []


def test_a_missing_price_blocks_before_the_inner_client_is_called():
    inner = FakeLLMClient(ok_response())
    with pytest.raises(PriceNotConfigured):
        metered(inner).complete(req(model="claude-unpriced"))
    assert inner.requests == []


def test_a_failed_call_releases_its_reservation_and_costs_nothing():
    m = metered(FakeLLMClient(LLMError("boom")))
    with pytest.raises(LLMError):
        m.complete(req())
    assert m.tracker.session_spent == 0
    m.inner.script.append(ok_response())
    m.complete(req())                                  # the reservation was returned, so this is allowed


def test_a_truncated_or_refused_answer_is_still_billed():
    m = metered(FakeLLMClient(ok_response(stop_reason="max_tokens", output_tokens=4096)))
    r = m.complete(req())
    assert r.cost_usd == D("0.04116") and m.tracker.session_spent == D("0.04116")   # 100 in + 4096 out


def test_the_session_ceiling_stops_a_second_run():
    m = metered(FakeLLMClient(ok_response(input_tokens=100_000, output_tokens=10_000), ok_response()), per_run="1", per_session="0.32")
    m.complete(req(run_id="a"))                                    # $0.30
    with pytest.raises(CostCeilingExceeded) as exc:
        m.complete(req(run_id="b", max_output_tokens=4096))
    assert "Per-session" in exc.value.message


def test_every_call_is_logged_with_tokens_and_cost_but_no_secrets(caplog):
    m = metered(FakeLLMClient(ok_response(input_tokens=1234, output_tokens=56)))
    with caplog.at_level(logging.INFO, logger="app.llm"):
        m.complete(req(purpose="extract"))
    line = next(r.getMessage() for r in caplog.records if "llm_call" in r.getMessage())
    assert "in=1234" in line and "out=56" in line and "cost=$" in line and "purpose=extract" in line and "run=run-1" in line


def test_metered_client_repr_is_safe():
    c = build_llm_client(Settings(_env_file=None, anthropic_api_key=CANARY), CostTracker(D(1), D(1)), sdk_client=FakeSDK())
    assert CANARY not in repr(c)


def test_concurrent_metered_calls_keep_exact_totals():
    m = metered(FakeLLMClient(*[ok_response(input_tokens=1000, output_tokens=100) for _ in range(20)]), per_run="10", per_session="10")
    threads = [threading.Thread(target=lambda: m.complete(req())) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert m.tracker.session_spent == 20 * D("0.003")


# ------------------------------------------------------------------------------ record / replay

def test_record_then_replay_round_trips_the_response(tmp_path):
    image = b"\x89PNG-fake-image-bytes"
    request = req(parts=(text_part("invoice text"), image_part("image/png", image)), schema={"type": "object"})
    original = ok_response('{"a": 1}', input_tokens=321, output_tokens=45)
    RecordingClient(FakeLLMClient(original), tmp_path).complete(request)
    replayed = ReplayClient(tmp_path).complete(request)
    assert replayed.text == '{"a": 1}' and replayed.usage.input_tokens == 321 and replayed.usage.output_tokens == 45
    assert replayed.stop_reason == "end_turn" and replayed.request_id == "req_fake"


def test_a_request_without_a_fixture_is_a_replay_miss_never_a_network_call(tmp_path):
    with pytest.raises(ReplayMiss) as exc:
        ReplayClient(tmp_path).complete(req())
    assert exc.value.code == "replay_miss" and str(tmp_path) in exc.value.message


def test_the_key_depends_on_everything_that_shapes_the_answer():
    base = request_key(req())
    assert base == request_key(req(run_id="another-run", purpose="other", max_output_tokens=1))    # run/purpose/caps are not part of it
    assert base != request_key(req(system="different"))
    assert base != request_key(req(model="other-model"))
    assert base != request_key(req(schema={"type": "object"}))
    assert base != request_key(req(parts=(text_part("bye"),)))
    a = req(parts=(image_part("image/png", b"one"),))
    b = req(parts=(image_part("image/png", b"two"),))
    assert request_key(a) != request_key(b)


def test_fixtures_hold_hashes_and_sizes_never_image_bytes_or_secrets(tmp_path):
    secret_image = b"\x89PNG-SECRET-PIXELS-0123456789"
    request = req(parts=(text_part("hi"), image_part("image/png", secret_image)))
    RecordingClient(FakeLLMClient(ok_response()), tmp_path).complete(request)
    (fixture,) = tmp_path.glob("*.json")
    raw = fixture.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert "SECRET-PIXELS" not in raw and CANARY not in raw and "api_key" not in raw.lower()
    image_summary = data["request"]["parts"][1]
    assert image_summary["kind"] == "image" and image_summary["bytes"] == len(secret_image) and len(image_summary["sha256"]) == 64
    assert data["request"]["parts"][0] == {"kind": "text", "chars": 2}


def test_recording_client_passes_errors_through_without_writing(tmp_path):
    with pytest.raises(LLMError):
        RecordingClient(FakeLLMClient(LLMError("boom")), tmp_path).complete(req())
    assert list(tmp_path.glob("*.json")) == []


# ------------------------------------------------------------------------------ probe

def settings(**kw):
    return Settings(_env_file=None, **kw)


def test_probe_reports_acceptance_when_the_api_takes_thinking_disabled_with_effort_low():
    s = settings()
    sdk = FakeSDK(sdk_message('{"ok": true}'))
    report = run_probe(metered(AnthropicClient(s, sdk_client=sdk)), s)
    assert report.accepted_as_configured and "thinking=disabled + effort=low: ACCEPTED" in report.verdict
    assert sdk.calls[0]["thinking"] == {"type": "disabled"} and sdk.calls[0]["output_config"]["effort"] == "low"
    assert sdk.calls[0]["max_tokens"] == 64                                # a tiny, cheap call


def test_probe_reports_a_rejection_and_the_fallback():
    s = settings()
    sdk = FakeSDK(sdk_error("bad_request", "thinking cannot be disabled when effort is set"), sdk_message('{"ok": true}'))
    report = run_probe(metered(AnthropicClient(s, sdk_client=sdk)), s)
    assert not report.accepted_as_configured
    assert "REJECTED" in report.verdict and "effort=low only" in report.verdict and "LLM_THINKING=omit" in report.verdict
    assert "thinking" not in sdk.calls[1]


def test_probe_reports_the_omit_configuration():
    s = settings(llm_thinking="omit")
    report = run_probe(metered(AnthropicClient(s, sdk_client=FakeSDK(sdk_message()))), s)
    assert report.accepted_as_configured and "thinking omitted" in report.verdict


def test_probe_main_without_a_key_prints_a_clear_message_and_exits_2(capsys):
    assert probe_module.main([]) == 2
    out = capsys.readouterr().out
    assert "NOT CONFIGURED" in out and "ANTHROPIC_API_KEY" in out


def test_probe_main_prints_the_report(monkeypatch, capsys):
    s = settings(anthropic_api_key=CANARY)
    monkeypatch.setattr(probe_module, "get_settings", lambda: s)
    monkeypatch.setattr(probe_module, "build_llm_client",
                        lambda st: metered(AnthropicClient(st, sdk_client=FakeSDK(sdk_message('{"ok": true}', input_tokens=80, output_tokens=9)))))
    assert probe_module.main([]) == 0
    out = capsys.readouterr().out
    assert "ACCEPTED" in out and "in=80 out=9" in out and "cost:       $0.000250" in out and CANARY not in out


def test_probe_main_reports_an_llm_failure_and_exits_1(monkeypatch, capsys):
    s = settings(anthropic_api_key=CANARY)
    monkeypatch.setattr(probe_module, "get_settings", lambda: s)
    monkeypatch.setattr(probe_module, "build_llm_client",
                        lambda st: metered(AnthropicClient(st, sdk_client=FakeSDK(sdk_error("auth")))))
    assert probe_module.main([]) == 1
    assert "PROBE FAILED [auth]" in capsys.readouterr().out
