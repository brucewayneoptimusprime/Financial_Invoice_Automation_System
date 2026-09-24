"""`python -m app.llm.probe --schema [--structured MODE | --all]`: send the REAL wire schema (or the real prompt-described
shape) with a tiny text-only prompt."""
import json
from decimal import Decimal

import pytest

from app.config import DEFAULT_LLM_PRICES, Settings
from app.extraction.prompts import PROMPT_JSON_SUFFIX
from app.extraction.wire import wire_schema
from app.llm import probe as probe_module
from app.llm.budget import CostTracker
from app.llm.client import AnthropicClient, MeteredClient
from app.llm.errors import LLMBadRequestError, LLMSchemaError
from app.llm.probe import recommendation, run_schema_probe
from tests.extraction.wire_convert import to_wire
from tests.llm.fakes import FakeLLMClient, FakeSDK, ok_response, sdk_error, sdk_message

D = Decimal
CANARY = "sk-ant-api03-CANARY-do-not-leak-0123456789"
UNION_ERROR = ("Schemas contains too many parameters with union types (49 parameters with type arrays or anyOf) "
               "which exceeds the limit: 16")
GRAMMAR_ERROR = ("The compiled grammar is too large, which would cause performance issues. Simplify your tool schemas or "
                 "reduce the number of strict tools.")


def blank_reply() -> str:
    return json.dumps(to_wire({}))


def metered(inner):
    return MeteredClient(inner, CostTracker(D("0.25"), D("5")), DEFAULT_LLM_PRICES)


def cfg(**kw):
    return Settings(_env_file=None, **kw)


# ------------------------------------------------------------------------------ json_schema mode

def test_the_probe_sends_the_real_schema_with_a_tiny_text_only_prompt():
    fake = FakeLLMClient(ok_response(blank_reply()))
    run_schema_probe(fake, cfg())
    (req,) = fake.requests
    assert req.schema == wire_schema()                                    # the REAL schema, not a stand-in
    assert all(p.kind == "text" for p in req.parts) and len(req.parts) == 1        # no images
    assert req.max_output_tokens == 1500 and req.purpose == "probe-schema" and len(req.system) < 400
    assert req.model == "claude-sonnet-5"


def test_an_accepted_schema_whose_reply_converts_is_reported_ok():
    report = run_schema_probe(FakeLLMClient(ok_response(blank_reply())), cfg())
    assert report.accepted and report.converts is True and report.ok and report.mode == "json_schema"
    assert "ACCEPTED" in report.message


def test_a_rejected_schema_reports_the_apis_message():
    err = LLMSchemaError("The API rejected the structured-output schema (HTTP 400): " + GRAMMAR_ERROR)
    report = run_schema_probe(FakeLLMClient(err), cfg())
    assert not report.accepted and report.converts is None and report.response is None and not report.ok
    assert "SCHEMA REJECTED" in report.message and "compiled grammar is too large" in report.message


def test_an_accepted_schema_with_an_unconvertible_reply_is_flagged():
    report = run_schema_probe(FakeLLMClient(ok_response("not json at all")), cfg())
    assert report.accepted and report.converts is False and "did not convert" in report.message and not report.ok
    wrong = json.loads(blank_reply())
    wrong["fields"][0].pop("found")
    report = run_schema_probe(FakeLLMClient(ok_response(json.dumps(wrong))), cfg())
    assert report.converts is False and "found" in report.message


@pytest.mark.parametrize("api_error", [UNION_ERROR, GRAMMAR_ERROR])
def test_the_apis_real_schema_errors_do_not_trigger_the_thinking_fallback(api_error):
    """Neither message mentions thinking or effort, so there must be no wasted second call."""
    sdk = FakeSDK(sdk_error("bad_request", api_error), sdk_message(blank_reply()))
    report = run_schema_probe(metered(AnthropicClient(cfg(), sdk_client=sdk)), cfg())
    assert not report.accepted and len(sdk.calls) == 1 and api_error.split(",")[0][:40] in report.message


# ------------------------------------------------------------------------------ prompt_json mode

def test_prompt_json_probe_sends_no_schema_and_puts_the_shape_in_the_prompt():
    fake = FakeLLMClient(ok_response(blank_reply()))
    report = run_schema_probe(fake, cfg(), "prompt_json")
    (req,) = fake.requests
    assert req.schema is None and PROMPT_JSON_SUFFIX.split("{schema}")[0] in req.system
    assert json.dumps(wire_schema(), separators=(",", ":")) in req.system
    assert all(p.kind == "text" for p in req.parts) and report.ok and report.mode == "prompt_json" and report.message == "works"


def test_prompt_json_probe_tolerates_fences_and_prose():
    reply = "Here you go:\n```json\n" + blank_reply() + "\n```\nDone."
    assert run_schema_probe(FakeLLMClient(ok_response(reply)), cfg(), "prompt_json").ok


def test_the_probe_uses_the_configured_mode_by_default():
    fake = FakeLLMClient(ok_response(blank_reply()))
    run_schema_probe(fake, cfg(llm_structured_output="prompt_json"))
    assert fake.requests[0].schema is None


# ------------------------------------------------------------------------------ recommendations

def reports(js_ok, pj_ok):
    r = []
    for mode, ok in (("json_schema", js_ok), ("prompt_json", pj_ok)):
        r.append(run_schema_probe(FakeLLMClient(ok_response(blank_reply()) if ok else LLMBadRequestError("no")), cfg(), mode))
    return r


def test_recommendation_keeps_json_schema_when_it_works():
    assert "Keep the default" in recommendation(reports(True, True)) and "Keep the default" in recommendation(reports(True, False))


def test_recommendation_switches_to_prompt_json_when_only_it_works():
    text = recommendation(reports(False, True))
    assert "json_schema does NOT work but prompt_json does" in text and "LLM_STRUCTURED_OUTPUT=prompt_json" in text


def test_recommendation_when_neither_works():
    assert "neither mode passed" in recommendation(reports(False, False))


# ------------------------------------------------------------------------------ main()

def patch_main(monkeypatch, inner, key=CANARY, **kw):
    s = cfg(anthropic_api_key=key, **kw)
    monkeypatch.setattr(probe_module, "get_settings", lambda: s)
    monkeypatch.setattr(probe_module, "build_llm_client", lambda st: metered(AnthropicClient(st, sdk_client=inner)))


def test_main_schema_prints_the_result_tokens_cost_and_conversion(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_message(blank_reply(), input_tokens=1400, output_tokens=590)))
    assert probe_module.main(["--schema"]) == 0
    out = capsys.readouterr().out
    assert "[json_schema] result:     ACCEPTED by the API" in out and "tokens:     in=1400 out=590" in out
    assert "cost:       $0.008700" in out and "conversion: the reply converts to an all-null ExtractedInvoice" in out
    assert "sent:       thinking=disabled, effort=low" in out and CANARY not in out


def test_main_schema_exits_1_and_shows_the_api_message_when_rejected(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_error("bad_request", GRAMMAR_ERROR)))
    assert probe_module.main(["--schema"]) == 1
    out = capsys.readouterr().out
    assert "SCHEMA REJECTED by the API" in out and "compiled grammar is too large" in out and CANARY not in out
    assert "LLM_STRUCTURED_OUTPUT=prompt_json" in out                          # the fix is named right in the message


def test_main_schema_exits_1_when_the_reply_does_not_convert(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_message("garbage")))
    assert probe_module.main(["--schema"]) == 1
    assert "did not convert" in capsys.readouterr().out


def test_main_structured_flag_implies_schema_and_overrides_the_configured_mode(monkeypatch, capsys):
    sdk = FakeSDK(sdk_message(blank_reply()))
    patch_main(monkeypatch, sdk)
    assert probe_module.main(["--structured", "prompt_json"]) == 0
    assert "[prompt_json] result:     works" in capsys.readouterr().out
    assert "format" not in sdk.calls[0]["output_config"]                      # no strict schema went to the API


def test_main_all_tests_both_modes_and_recommends(monkeypatch, capsys):
    sdk = FakeSDK(sdk_message(blank_reply()), sdk_message(blank_reply()))
    patch_main(monkeypatch, sdk)
    assert probe_module.main(["--schema", "--all"]) == 0
    out = capsys.readouterr().out
    assert "[json_schema] result:" in out and "[prompt_json] result:" in out and "RECOMMENDATION: json_schema works" in out
    assert "format" in sdk.calls[0]["output_config"] and "format" not in sdk.calls[1]["output_config"]


def test_main_all_recommends_prompt_json_when_json_schema_is_rejected(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_error("bad_request", GRAMMAR_ERROR), sdk_message(blank_reply())))
    assert probe_module.main(["--schema", "--all"]) == 0                      # at least one mode works
    out = capsys.readouterr().out
    assert "[json_schema] result:     SCHEMA REJECTED" in out and "[prompt_json] result:     works" in out
    assert "json_schema does NOT work but prompt_json does" in out


def test_main_all_exits_1_when_neither_mode_works(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_error("bad_request", GRAMMAR_ERROR), sdk_message("garbage")))
    assert probe_module.main(["--all"]) == 1
    assert "neither mode passed" in capsys.readouterr().out


def test_main_schema_without_a_key_exits_2(capsys):
    assert probe_module.main(["--schema"]) == 2
    out = capsys.readouterr().out
    assert "NOT CONFIGURED" in out and "ANTHROPIC_API_KEY" in out
    assert probe_module.main(["--schema", "--all"]) == 2


def test_main_schema_reports_other_llm_errors(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_error("timeout")))
    assert probe_module.main(["--schema"]) == 1
    assert "PROBE FAILED [timeout]" in capsys.readouterr().out


def test_the_default_probe_is_unchanged_and_the_flags_are_optional(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_message('{"ok": true}')))
    assert probe_module.main([]) == 0
    assert "ACCEPTED" in capsys.readouterr().out


def test_schema_probe_worst_case_cost_stays_small():
    """1500 output tokens at $10/M plus a small input: the ceiling check allows it, and it is a cent or two at most."""
    from app.llm.client import LLMRequest, text_part
    from app.llm.pricing import worst_case_cost

    req = LLMRequest(system=probe_module.SCHEMA_PROBE_SYSTEM, parts=(text_part("Fill the form for an empty document."),),
                     model="claude-sonnet-5", max_output_tokens=1500, schema=wire_schema())
    assert worst_case_cost(req, DEFAULT_LLM_PRICES["claude-sonnet-5"]) < D("0.025")


def test_the_observed_live_probe_cost_matches_the_price_table():
    """The real json_schema probe measured 1399 in / 590 out: $0.0087 at $2 / $10 per million tokens."""
    from app.llm.pricing import cost_usd
    from app.llm.types import LLMUsage

    assert cost_usd(LLMUsage(input_tokens=1399, output_tokens=590), DEFAULT_LLM_PRICES["claude-sonnet-5"]) == D("0.008698")
