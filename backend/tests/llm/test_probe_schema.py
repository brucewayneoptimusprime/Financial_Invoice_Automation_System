"""`python -m app.llm.probe --schema`: send the REAL wire schema with a tiny text-only prompt."""
import json
from decimal import Decimal

from app.config import DEFAULT_LLM_PRICES, Settings
from app.extraction.wire import wire_schema
from app.llm import probe as probe_module
from app.llm.budget import CostTracker
from app.llm.client import AnthropicClient, MeteredClient
from app.llm.errors import LLMBadRequestError, LLMTimeoutError
from app.llm.probe import run_schema_probe
from tests.extraction.wire_convert import to_wire
from tests.llm.fakes import FakeLLMClient, FakeSDK, ok_response, sdk_error, sdk_message

D = Decimal
CANARY = "sk-ant-api03-CANARY-do-not-leak-0123456789"
UNION_ERROR = ("Schemas contains too many parameters with union types (49 parameters with type arrays or anyOf) "
               "which exceeds the limit: 16")


def blank_reply() -> str:
    return json.dumps(to_wire({}))


def metered(inner):
    return MeteredClient(inner, CostTracker(D("0.25"), D("5")), DEFAULT_LLM_PRICES)


def test_the_probe_sends_the_real_schema_with_a_tiny_text_only_prompt():
    fake = FakeLLMClient(ok_response(blank_reply()))
    run_schema_probe(fake, Settings(_env_file=None))
    (req,) = fake.requests
    assert req.schema == wire_schema()                                    # the REAL schema, not a stand-in
    assert all(p.kind == "text" for p in req.parts) and len(req.parts) == 1        # no images
    assert req.max_output_tokens == 1500 and req.purpose == "probe-schema" and len(req.system) < 400
    assert req.model == "claude-sonnet-5"


def test_an_accepted_schema_whose_reply_converts_is_reported_ok():
    report = run_schema_probe(FakeLLMClient(ok_response(blank_reply())), Settings(_env_file=None))
    assert report.accepted and report.converts is True and "ACCEPTED" in report.message


def test_a_rejected_schema_reports_the_apis_message():
    report = run_schema_probe(FakeLLMClient(LLMBadRequestError("The API rejected the request (HTTP 400): " + UNION_ERROR)), Settings(_env_file=None))
    assert not report.accepted and report.converts is None and report.response is None
    assert "SCHEMA REJECTED" in report.message and "union types" in report.message and "limit: 16" in report.message


def test_an_accepted_schema_with_an_unconvertible_reply_is_flagged():
    report = run_schema_probe(FakeLLMClient(ok_response("not json at all")), Settings(_env_file=None))
    assert report.accepted and report.converts is False and "did not convert" in report.message
    wrong = json.loads(blank_reply())
    wrong["total"].pop("found")
    report = run_schema_probe(FakeLLMClient(ok_response(json.dumps(wrong))), Settings(_env_file=None))
    assert report.converts is False and "total: missing 'found'" in report.message


def test_the_apis_real_union_error_does_not_trigger_the_thinking_fallback():
    """The rejection text mentions neither thinking nor effort, so there must be no wasted second call."""
    sdk = FakeSDK(sdk_error("bad_request", UNION_ERROR), sdk_message(blank_reply()))
    client = metered(AnthropicClient(Settings(_env_file=None), sdk_client=sdk))
    report = run_schema_probe(client, Settings(_env_file=None))
    assert not report.accepted and len(sdk.calls) == 1 and "limit: 16" in report.message


def patch_main(monkeypatch, inner, key=CANARY):
    s = Settings(_env_file=None, anthropic_api_key=key)
    monkeypatch.setattr(probe_module, "get_settings", lambda: s)
    monkeypatch.setattr(probe_module, "build_llm_client", lambda st: metered(AnthropicClient(st, sdk_client=inner)))


def test_main_schema_prints_the_result_tokens_cost_and_conversion(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_message(blank_reply(), input_tokens=2600, output_tokens=520)))
    assert probe_module.main(["--schema"]) == 0
    out = capsys.readouterr().out
    assert "result:     schema ACCEPTED by the API" in out and "tokens:     in=2600 out=520" in out
    assert "cost:       $0.010400" in out and "conversion: the reply converts to an all-null ExtractedInvoice" in out
    assert "sent:       thinking=disabled, effort=low" in out and CANARY not in out


def test_main_schema_exits_1_and_shows_the_api_message_when_rejected(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_error("bad_request", UNION_ERROR)))
    assert probe_module.main(["--schema"]) == 1
    out = capsys.readouterr().out
    assert "SCHEMA REJECTED by the API" in out and "union types" in out and CANARY not in out


def test_main_schema_exits_1_when_the_reply_does_not_convert(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_message("garbage")))
    assert probe_module.main(["--schema"]) == 1
    assert "did not convert" in capsys.readouterr().out


def test_main_schema_without_a_key_exits_2(capsys):
    assert probe_module.main(["--schema"]) == 2
    out = capsys.readouterr().out
    assert "NOT CONFIGURED" in out and "ANTHROPIC_API_KEY" in out


def test_main_schema_reports_other_llm_errors(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_error("timeout")))
    assert probe_module.main(["--schema"]) == 1
    assert "PROBE FAILED [timeout]" in capsys.readouterr().out


def test_the_default_probe_is_unchanged_and_the_flag_is_optional(monkeypatch, capsys):
    patch_main(monkeypatch, FakeSDK(sdk_message('{"ok": true}')))
    assert probe_module.main([]) == 0
    assert "ACCEPTED" in capsys.readouterr().out


def test_schema_probe_worst_case_cost_stays_small():
    """1500 output tokens at $10/M plus a small input: the ceiling check would allow it well under a cent or two."""
    from app.llm.client import LLMRequest, text_part
    from app.llm.pricing import worst_case_cost

    req = LLMRequest(system=probe_module.SCHEMA_PROBE_SYSTEM, parts=(text_part("Fill the form for an empty document."),),
                     model="claude-sonnet-5", max_output_tokens=1500, schema=wire_schema())
    assert worst_case_cost(req, DEFAULT_LLM_PRICES["claude-sonnet-5"]) < D("0.025")
