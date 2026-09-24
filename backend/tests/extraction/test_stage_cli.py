import json
from decimal import Decimal

import pytest

from app.config import get_settings
from app.engine.engine import run_decide_stage, run_validate_stage
from app.engine.facts import POLineFact
from app.engine.matching import run_match_stage
from app.enums import Decision, StageStatus
from app.extraction import cli
from app.extraction.extractor import extract_invoice
from app.extraction.stage import run_extract_stage
from app.ingest.stage import run_ingest_stage
from app.llm.replay import RecordingClient
from app.models import RunContext
from app.models.extraction_meta import ExtractionMeta
from tests.engine.real import BUILTIN
from tests.extraction.helpers import load_reply, make_source, reply_text, settings
from tests.extraction.wire_convert import set_field
from tests.factories import make_facts, make_po, make_vendor
from tests.llm.fakes import FakeLLMClient, ok_response

D = Decimal
GOOD = reply_text(load_reply("us_native_invoice"))


def ingest_ctx(tmp_path, kind="native", run_id="run-1"):
    ctx = RunContext(run_id=run_id, source_file="x")
    run_ingest_stage(ctx, make_source(tmp_path, kind), settings(tmp_path))
    return ctx


# ------------------------------------------------------------------------------ the extract stage

def test_stage_sets_the_context_and_writes_run_artefacts(tmp_path):
    ctx = ingest_ctx(tmp_path)
    stage = run_extract_stage(ctx, FakeLLMClient(ok_response(GOOD, input_tokens=4000, output_tokens=700)), settings(tmp_path))
    assert stage.stage == "extract" and stage.status is StageStatus.OK
    assert ctx.extracted.total.value == D("1105.00") and ctx.extraction_meta.path == "text_and_vision"
    run_dir = tmp_path / "runs" / "run-1"
    assert json.loads((run_dir / "extracted.json").read_text(encoding="utf-8"))["total"]["value"] == "1105.00"
    assert (run_dir / "llm" / "reply-1.txt").read_text(encoding="utf-8") == GOOD
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["ingest"]["sha256"] == ctx.file_hash and meta["extraction"]["tokens_in"] == 4000
    assert stage.outputs["path"] == "text_and_vision" and stage.outputs["degraded"] is False and stage.outputs["tokens_out"] == 700


def test_stage_result_and_artefacts_are_json_and_contain_no_secrets(tmp_path):
    ctx = ingest_ctx(tmp_path)
    stage = run_extract_stage(ctx, FakeLLMClient(ok_response(GOOD)), settings(tmp_path))
    json.loads(stage.model_dump_json())
    for path in (tmp_path / "runs" / "run-1").rglob("*.json"):
        assert "sk-ant" not in path.read_text(encoding="utf-8") and "api_key" not in path.read_text(encoding="utf-8").lower()


def test_a_degraded_extraction_is_a_failed_stage_that_still_yields_an_all_null_invoice(tmp_path):
    ctx = ingest_ctx(tmp_path)
    stage = run_extract_stage(ctx, FakeLLMClient(ok_response("nope"), ok_response("nope")), settings(tmp_path))
    assert stage.status is StageStatus.FAILED and stage.outputs["degraded"] and stage.outputs["failure_code"] == "schema_invalid"
    assert ctx.extracted.total.value is None and ctx.extraction_meta.failure_kind == "system_side"
    assert (tmp_path / "runs" / "run-1" / "llm" / "reply-2.txt").is_file()


def test_the_stage_requires_ingest_first(tmp_path):
    with pytest.raises(ValueError, match="ingest stage must run"):
        run_extract_stage(RunContext(run_id="r", source_file="x"), FakeLLMClient(), settings(tmp_path))


def test_unreadable_documents_flow_through_the_stage_without_a_model_call(tmp_path):
    ctx = ingest_ctx(tmp_path, "locked")
    fake = FakeLLMClient(ok_response(GOOD))
    stage = run_extract_stage(ctx, fake, settings(tmp_path))
    assert stage.status is StageStatus.FAILED and fake.requests == [] and ctx.extraction_meta.failure_code == "password_protected"
    assert not (tmp_path / "runs" / "run-1" / "llm").exists()


def test_a_missing_key_never_crashes_the_stage(tmp_path):
    ctx = ingest_ctx(tmp_path)
    stage = run_extract_stage(ctx, None, settings(tmp_path))
    assert stage.status is StageStatus.FAILED and stage.outputs["failure_code"] == "config"
    assert "ANTHROPIC_API_KEY" in stage.outputs["failure_reason"]


# ------------------------------------------------------------------------------ end to end with the real rules engine

def facts_for_northwind():
    lines = (POLineFact(line_no=1, description="Widget A", quantity=D(10), unit_price=D("60.00"), amount=D("600.00")),
             POLineFact(line_no=2, description="Widget B", quantity=D(5), unit_price=D("80.00"), amount=D("400.00")))
    return make_facts(vendors=[make_vendor(1, "Northwind Trading Co")],
                      pos=[make_po(id=1, po_number="PO-5001", vendor_id=1, total="2000.00", lines=lines)])


def pipeline(ctx, client, tmp_path, facts):
    run_extract_stage(ctx, client, settings(tmp_path))
    ctx.facts = facts
    run_match_stage(ctx)
    run_validate_stage(ctx, list(BUILTIN.values()))
    run_decide_stage(ctx)
    return {r.rule_id: r for r in ctx.rule_results}


def test_a_clean_native_invoice_flows_from_file_to_approval(tmp_path):
    reply = load_reply("us_native_invoice")
    reply["adjustments"] = []
    set_field(reply, "total", value="1080.00", source_text="Total Due: 1,080.00")
    ctx = ingest_ctx(tmp_path)
    res = pipeline(ctx, FakeLLMClient(ok_response(reply_text(reply))), tmp_path, facts_for_northwind())
    triggered = {k: v.outcome_key for k, v in res.items() if v.outcome.value in ("flag", "fail")}
    assert ctx.decision is Decision.APPROVE and triggered == {}, triggered
    assert ctx.matched_po.po_number == "PO-5001" and ctx.file_hash == ctx.ingest.sha256


def test_a_prompt_injection_attempt_cannot_approve_an_unknown_vendor(tmp_path):
    ctx = ingest_ctx(tmp_path)
    res = pipeline(ctx, FakeLLMClient(ok_response(reply_text(load_reply("injection_attempt")))), tmp_path, facts_for_northwind())
    assert ctx.decision is not Decision.APPROVE                      # the rules decide, whatever the document says
    assert res["r_vendor_status"].outcome_key == "unknown" and ctx.extracted.document_quality.contains_reader_instructions is True


def test_a_degraded_extraction_is_never_approved(tmp_path):
    ctx = ingest_ctx(tmp_path)
    pipeline(ctx, FakeLLMClient(ok_response("x"), ok_response("y")), tmp_path, facts_for_northwind())
    assert ctx.decision is not Decision.APPROVE and ctx.extracted.total.value is None


# ------------------------------------------------------------------------------ the CLI

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "cli-runs"))
    get_settings.cache_clear()
    return tmp_path


def record_replay(tmp_path, source, reply):
    """Record what the extractor asks for this file, so the CLI can replay it offline."""
    rec = tmp_path / "recordings"
    ctx = RunContext(run_id="rec-run", source_file=source.name)
    st = settings(tmp_path / "rec")
    run_ingest_stage(ctx, source, st)
    extract_invoice(ctx.ingest, client=RecordingClient(FakeLLMClient(ok_response(reply_text(reply), input_tokens=4200, output_tokens=650)), rec), settings=st)
    return rec


def split_json(out):
    body = out.split("=== extracted invoice (JSON) ===\n", 1)[1].split("\n\n=== summary ===", 1)[0]
    return json.loads(body)


def test_cli_replay_prints_json_path_tokens_cost_and_grounding_status(cli_env, capsys):
    source = make_source(cli_env / "docs", "native")
    rec = record_replay(cli_env, source, load_reply("us_native_invoice"))
    code = cli.main([str(source), "--replay", str(rec), "--run-id", "cli-run"])
    out = capsys.readouterr().out
    assert code == 0 and "RESULT:      OK" in out
    assert split_json(out)["total"]["value"] == "1105.00"
    assert "path used:   text_and_vision" in out and "tokens:      in=4200 out=650" in out
    assert "cost:        $0.014900" in out                                        # 4200*2/1e6 + 650*10/1e6
    assert "thinking:    not exercised" in out and "grounding:   not run yet" in out
    assert "=== ingest ===" in out and "text layer:  usable" in out and "run folder:" in out
    assert (cli_env / "cli-runs" / "cli-run" / "extracted.json").is_file()


def test_cli_prints_non_ascii_characters_without_crashing(cli_env, capsys):
    reply = load_reply("us_native_invoice")
    set_field(reply, "vendor_address", value="Frankfurter Straße 1, Köln € ₹")
    source = make_source(cli_env / "docs", "native")
    code = cli.main([str(source), "--replay", str(record_replay(cli_env, source, reply))])
    assert code == 0 and "Frankfurter Straße 1, Köln € ₹" in capsys.readouterr().out


def test_cli_without_a_key_explains_and_exits_3(cli_env, capsys):
    source = make_source(cli_env / "docs", "native")
    assert cli.main([str(source)]) == 3
    out = capsys.readouterr().out
    assert "NOT CONFIGURED" in out and "ANTHROPIC_API_KEY" in out and "--ingest-only" in out
    assert not (cli_env / "cli-runs").exists()                                    # nothing was ingested


def test_cli_ingest_only_needs_no_key_and_makes_no_model_call(cli_env, capsys):
    source = make_source(cli_env / "docs", "native")
    assert cli.main([str(source), "--ingest-only", "--events"]) == 0
    out = capsys.readouterr().out
    assert "=== ingest ===" in out and "extracted invoice" not in out and "document_rendered" in out


def test_cli_reports_a_rejected_file(cli_env, capsys, tmp_path):
    bad = tmp_path / "evil.pdf"
    bad.write_bytes(b"MZ\x90\x00 executable")
    assert cli.main([str(bad), "--ingest-only"]) == 2
    assert "REJECTED [unsupported_type]" in capsys.readouterr().out


def test_cli_replay_miss_degrades_with_exit_1(cli_env, capsys):
    source = make_source(cli_env / "docs", "native")
    assert cli.main([str(source), "--replay", str(cli_env / "empty")]) == 1
    out = capsys.readouterr().out
    assert "DEGRADED [system_side/replay_miss]" in out and "path used:" in out


def test_cli_unreadable_document_is_vendor_side_and_exits_1(cli_env, capsys):
    source = make_source(cli_env / "docs", "locked")
    assert cli.main([str(source), "--replay", str(cli_env / "none")]) == 1
    assert "DEGRADED [vendor_side/password_protected]" in capsys.readouterr().out


def test_cli_max_cost_stops_the_call_before_it_is_made(cli_env, capsys):
    source = make_source(cli_env / "docs", "native")
    rec = record_replay(cli_env, source, load_reply("us_native_invoice"))
    assert cli.main([str(source), "--replay", str(rec), "--max-cost", "0.0001"]) == 1
    assert "DEGRADED [system_side/cost_ceiling]" in capsys.readouterr().out


def test_cli_events_flag_prints_the_audit_trail(cli_env, capsys):
    source = make_source(cli_env / "docs", "native")
    rec = record_replay(cli_env, source, load_reply("us_native_invoice"))
    cli.main([str(source), "--replay", str(rec), "--events"])
    out = capsys.readouterr().out
    for event in ("file_stored", "path_selected", "llm_call", "extraction_complete"):
        assert event in out


def test_cli_record_without_a_key_is_not_configured(cli_env, capsys):
    source = make_source(cli_env / "docs", "native")
    assert cli.main([str(source), "--record", str(cli_env / "rec")]) == 3


def test_cli_replay_and_record_are_mutually_exclusive(cli_env):
    with pytest.raises(SystemExit):
        cli.main(["x.pdf", "--replay", "a", "--record", "b"])


@pytest.mark.parametrize("mode", ["auto", "vision", "text_and_vision", "text"])
def test_cli_accepts_the_four_modes_and_rejects_others(mode):
    assert cli.build_parser().parse_args(["f.pdf", "--mode", mode]).mode == mode
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["f.pdf", "--mode", "magic"])


def test_thinking_line_covers_every_situation():
    assert "not exercised" in cli._thinking_line(ExtractionMeta())
    assert "ACCEPTED" in cli._thinking_line(ExtractionMeta(thinking_mode="disabled", effort="low"))
    line = cli._thinking_line(ExtractionMeta(thinking_mode="omit", effort="low", param_fallback="thinking_omitted"))
    assert "REJECTED" in line and "effort=low only" in line and "LLM_THINKING=omit" in line
    assert "as configured" in cli._thinking_line(ExtractionMeta(thinking_mode="omit", effort="low"))
