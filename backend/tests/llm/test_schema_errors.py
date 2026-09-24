"""The client recognises the API refusing a structured-output schema, and the CLI turns it into an actionable message."""
import pytest

from app.config import Settings
from app.extraction import cli
from app.llm.client import SCHEMA_REJECTED_ADVICE, AnthropicClient, LLMRequest, text_part
from app.llm.errors import LLMBadRequestError, LLMSchemaError
from tests.extraction.helpers import make_source
from tests.llm.fakes import FakeSDK, sdk_error, sdk_message

UNION = "Schemas contains too many parameters with union types (49 parameters with type arrays or anyOf) which exceeds the limit: 16"
GRAMMAR = ("The compiled grammar is too large, which would cause performance issues. Simplify your tool schemas or reduce "
           "the number of strict tools.")


def request():
    return LLMRequest(system="s", parts=(text_part("hi"),), model="claude-sonnet-5", max_output_tokens=100, schema={"type": "object"})


def client(*script):
    sdk = FakeSDK(*script)
    return AnthropicClient(Settings(_env_file=None), sdk_client=sdk), sdk


@pytest.mark.parametrize("api_message", [UNION, GRAMMAR, "Invalid schema for output_config.format: bad", "grammar compilation failed",
                                         "the structured output schema is not supported"])
def test_schema_and_grammar_rejections_become_llm_schema_error(api_message):
    c, sdk = client(sdk_error("bad_request", api_message))
    with pytest.raises(LLMSchemaError) as exc:
        c.complete(request())
    assert exc.value.code == "schema_rejected" and len(sdk.calls) == 1                  # no retry, no thinking fallback
    assert api_message[:40] in exc.value.message
    assert "LLM_STRUCTURED_OUTPUT=prompt_json" in exc.value.message and SCHEMA_REJECTED_ADVICE in exc.value.message
    assert isinstance(exc.value, LLMBadRequestError)                                    # still a bad request for generic handlers


@pytest.mark.parametrize("api_message", ["image exceeds 5 MB maximum", "model: claude-nope not found", "max_tokens: too large",
                                         "messages: first message must be user"])
def test_other_bad_requests_stay_plain_bad_requests(api_message):
    c, _ = client(sdk_error("bad_request", api_message))
    with pytest.raises(LLMBadRequestError) as exc:
        c.complete(request())
    assert not isinstance(exc.value, LLMSchemaError) and exc.value.code == "bad_request"


def test_only_http_400_counts_as_a_schema_rejection():
    c, _ = client(sdk_error("server", "internal error while compiling the grammar"))
    with pytest.raises(Exception) as exc:
        c.complete(request())
    assert not isinstance(exc.value, LLMSchemaError)


def test_the_schema_message_is_scrubbed_of_keys():
    c, _ = client(sdk_error("bad_request", GRAMMAR + " sk-ant-api03-LEAKY-123456"))
    with pytest.raises(LLMSchemaError) as exc:
        c.complete(request())
    assert "LEAKY" not in exc.value.message


# ------------------------------------------------------------------------------ the CLI banner

class RejectingClient:
    def __init__(self, *a, **k):
        pass

    def complete(self, request):
        raise LLMSchemaError("The API rejected the structured-output schema (HTTP 400): " + GRAMMAR + " " + SCHEMA_REJECTED_ADVICE)


@pytest.fixture
def cli_rejecting(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "cli-runs"))
    get_settings.cache_clear()
    monkeypatch.setattr(cli, "ReplayClient", RejectingClient)
    return tmp_path


def test_cli_prints_a_clear_message_naming_the_switch_instead_of_a_degraded_run(cli_rejecting, capsys):
    source = make_source(cli_rejecting / "docs", "native")
    assert cli.main([str(source), "--replay", str(cli_rejecting / "x")]) == cli.SCHEMA_EXIT_CODE == 4
    out = capsys.readouterr().out
    assert "THE API REJECTED THE EXTRACTION SCHEMA" in out and "LLM_STRUCTURED_OUTPUT=prompt_json" in out
    assert "not a problem with the invoice" in out and "compiled grammar is too large" in out
    assert "python -m app.llm.probe --schema --all" in out and "nothing was spent" in out
    assert "DEGRADED" not in out and "extracted invoice (JSON)" not in out and "RESULT:" not in out


def test_cli_still_reports_ordinary_degradation_the_old_way(cli_rejecting, capsys, monkeypatch):
    class Timeout(RejectingClient):
        def complete(self, request):
            from app.llm.errors import LLMTimeoutError
            raise LLMTimeoutError("timed out")

    monkeypatch.setattr(cli, "ReplayClient", Timeout)
    source = make_source(cli_rejecting / "docs", "native")
    assert cli.main([str(source), "--replay", str(cli_rejecting / "x")]) == 1
    assert "DEGRADED [system_side/timeout]" in capsys.readouterr().out


def test_cli_shows_the_structured_output_mode_in_its_summary(tmp_path, monkeypatch, capsys):
    from app.config import get_settings
    from tests.extraction.helpers import load_reply, reply_text
    from tests.extraction.test_stage_cli import record_replay

    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "cli-runs"))
    get_settings.cache_clear()
    source = make_source(tmp_path / "docs", "native")
    rec = record_replay(tmp_path, source, load_reply("us_native_invoice"))
    assert cli.main([str(source), "--replay", str(rec)]) == 0
    assert "structured output: json_schema" in capsys.readouterr().out

