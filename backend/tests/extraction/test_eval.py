"""The eval script: it refuses to spend money without --live, scores only verified entries, and never verifies anything itself."""
import ast
import json
import shutil
from pathlib import Path

import pytest

from app.config import get_settings
from app.extraction import eval as eval_module
from app.extraction.extractor import extract_invoice
from app.extraction.manifest import load_manifest
from app.ingest.stage import run_ingest_stage
from app.llm.errors import LLMSchemaError
from app.llm.replay import RecordingClient
from app.models import RunContext
from tests.extraction.helpers import make_source, settings as make_settings
from tests.extraction.real import REAL, real_reply
from tests.llm.fakes import FakeLLMClient, ok_response

CANARY = "sk-ant-api03-EVAL-CANARY-do-not-leak-0123456789"
KEY_10963 = {
    "verified": True, "vendor_name": "SuperStore", "invoice_number": "10963", "invoice_date": "2013-03-07", "currency": "USD",
    "document_type": "invoice", "subtotal": "5141.76", "tax": None, "total": "5338.08", "po_reference": None,
    "line_items": [{"description": "Hewlett Fax Machine, Color", "amount": "5141.76"}],
    "adjustments": [{"kind": "shipping", "amount": "196.32"}]}


def entry(name, body):
    return f"## {name}\n```expected\n{json.dumps(body)}\n```\n\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def no_live_client(monkeypatch):
    """Any attempt to build the real API client fails the test."""
    def boom(*a, **k):
        raise AssertionError("the real API client was constructed")

    monkeypatch.setattr(eval_module, "AnthropicClient", boom)


def record(tmp_path, source: Path, reply: dict, rec: Path, in_tokens=6800, out_tokens=920):
    ctx = RunContext(run_id="rec", source_file=source.name)
    cfg = make_settings(tmp_path / f"rec-{source.stem}")
    run_ingest_stage(ctx, source, cfg)
    extract_invoice(ctx.ingest, client=RecordingClient(FakeLLMClient(ok_response(json.dumps(reply), input_tokens=in_tokens,
                                                                                output_tokens=out_tokens)), rec), settings=cfg)


def make_folder(tmp_path):
    """data/invoices with the two REAL PDFs + a recording of the real replies; returns (folder, recordings)."""
    folder, rec = tmp_path / "invoices", tmp_path / "rec"
    folder.mkdir()
    for name in ("superstore_10963", "superstore_24429"):
        shutil.copy(REAL / f"{name}.pdf", folder / f"{name}.pdf")
        record(tmp_path, folder / f"{name}.pdf", real_reply(name), rec)
    return folder, rec


def run_main(capsys, *argv):
    code = eval_module.main([str(a) for a in argv])
    return code, capsys.readouterr().out


# ------------------------------------------------------------------------------------ no --live, no live call

@pytest.mark.parametrize("extra", [[], ["--max-cost", "1"], ["--draft-manifest"], ["--mode", "vision"], ["-v"]])
def test_without_live_or_replay_it_refuses_and_builds_no_client(env, capsys, no_live_client, extra):
    folder = make_folder(env)[0]
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md", *extra)
    assert code == eval_module.EXIT_LIVE_REFUSED == 5
    assert "REFUSED: no live API call was made (nothing was sent, nothing was spent)" in out and "--live" in out and "--replay DIR" in out
    assert "2 invoice(s)" in out
    assert not (env / "runs").exists() and not (env / "m.md").exists()                   # not even a run folder was created


def test_record_without_live_is_refused_too(env, capsys, no_live_client):
    folder = make_folder(env)[0]
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md", "--record", env / "new-rec")
    assert code == 5 and "REFUSED" in out and "record the responses" in out and not (env / "new-rec").exists()


def test_live_without_a_key_is_a_clear_error_and_no_call(env, capsys):
    folder = make_folder(env)[0]
    code, out = run_main(capsys, "--dir", folder, "--live", "--manifest", env / "m.md")
    assert code == eval_module.EXIT_NOT_CONFIGURED == 3 and "NOT CONFIGURED" in out and "ANTHROPIC_API_KEY" in out and "--replay" in out


def test_live_with_a_key_runs_records_and_never_prints_the_key(env, capsys, monkeypatch):
    folder = make_folder(env)[0]
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    get_settings.cache_clear()
    replies = {"10963": real_reply("superstore_10963"), "24429": real_reply("superstore_24429")}
    calls = []

    class FakeLive:
        def __init__(self, settings):
            assert settings.api_key_value() == CANARY

        def complete(self, request):
            calls.append(request)
            text = json.dumps(replies["10963" if len(calls) == 1 else "24429"])
            return ok_response(text, input_tokens=6800, output_tokens=920)

    monkeypatch.setattr(eval_module, "AnthropicClient", FakeLive)
    (env / "m.md").write_text(entry("superstore_10963.pdf", KEY_10963), encoding="utf-8")
    code, out = run_main(capsys, "--dir", folder, "--live", "--record", env / "new-rec", "--manifest", env / "m.md", "--max-cost", "0.5")
    assert code == 0 and len(calls) == 2 and CANARY not in out
    assert "LIVE MODE: up to 2 paid API call(s)" in out and "cost ceiling $0.5" in out and "EVAL REPORT (LIVE API calls)" in out
    assert len(list((env / "new-rec").glob("*.json"))) == 2
    # and what was recorded replays offline
    code, out = run_main(capsys, "--dir", folder, "--replay", env / "new-rec", "--manifest", env / "m.md")
    assert code == 0 and "REPLAY MODE" in out and "1 scored (verified)" in out


def test_replay_cannot_be_combined_with_live_or_record(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    assert run_main(capsys, "--dir", folder, "--replay", rec, "--live")[0] == eval_module.EXIT_USAGE
    assert run_main(capsys, "--dir", folder, "--replay", rec, "--record", env / "x")[0] == eval_module.EXIT_USAGE
    code, out = run_main(capsys, "--dir", folder, "--replay", env / "missing")
    assert code == 2 and "is not a folder of recorded responses" in out
    assert run_main(capsys, "--dir", folder, "--replay", rec, "--max-cost", "0")[0] == 2


def test_an_empty_or_missing_folder_is_fine_and_needs_no_flags(env, capsys, no_live_client):
    (env / "empty").mkdir()
    for folder in (env / "empty", env / "nope"):
        code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md")
        assert code == 0 and f"0 invoices found in {folder}" in out and "--draft-manifest" in out
    (env / "m.md").write_text("## a.pdf\n```expected\n{bad}\n```\n", encoding="utf-8")
    code, out = run_main(capsys, "--dir", env / "empty", "--manifest", env / "m.md")
    assert code == 0 and "WARNING: a.pdf: the expected block is not valid JSON" in out


def test_folders_with_no_invoice_files_count_as_empty(env, capsys, no_live_client):
    folder = env / "docs"
    folder.mkdir()
    (folder / ".gitkeep").write_text("", encoding="utf-8")
    (folder / "notes.txt").write_text("hi", encoding="utf-8")
    assert run_main(capsys, "--dir", folder, "--manifest", env / "m.md")[0] == 0


def test_the_real_client_is_built_in_exactly_one_place_behind_the_live_flag():
    """Structural guard: `AnthropicClient(...)` is called only inside build_client, and only after the allow_live check."""
    tree = ast.parse(Path(eval_module.__file__).read_text(encoding="utf-8"))
    sites = []
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "AnthropicClient":
                sites.append((func.name, node.lineno))
    assert [name for name, _ in sites] == ["build_client"]
    source = Path(eval_module.__file__).read_text(encoding="utf-8").splitlines()
    check = next(i for i, line in enumerate(source) if "if not allow_live:" in line)
    assert check < sites[0][1] - 1


def test_build_client_refuses_a_live_client_without_permission(monkeypatch):
    from app.config import Settings
    from app.llm.budget import CostTracker

    monkeypatch.setattr(eval_module, "AnthropicClient", lambda *a, **k: pytest.fail("constructed"))
    with pytest.raises(eval_module.LiveNotAllowed):
        eval_module.build_client(Settings(_env_file=None), CostTracker(1, 1))
    with pytest.raises(eval_module.LiveNotAllowed):
        eval_module.build_client(Settings(_env_file=None), CostTracker(1, 1), record=Path("x"))


# -------------------------------------------------------------------------------------- scoring and reporting

def test_replay_scores_only_verified_entries_and_reports_the_rest(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    (env / "m.md").write_text("Notes.\n\n" + entry("superstore_10963.pdf", KEY_10963)
                              + entry("superstore_24429.pdf", {"verified": False, "total": "999.00"}), encoding="utf-8")
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md", "--replay", rec)
    assert code == 0 and "EVAL REPORT (replayed responses, no API calls)" in out
    assert "2 processed, 2 extracted, 0 failed | 1 scored (verified), 1 unverified (NOT scored), 0 unlabelled (NOT scored)" in out
    assert "superstore_24429.pdf: ok" in out and "scored:" not in out.split("superstore_24429.pdf: ok")[1].splitlines()[0]
    assert "ALL" in out and "100.0%" in out and "files with every scored field correct: 1 of 1" in out
    assert "DIFFERENCES" not in out and "999.00" not in out                                # the unverified wrong answer key was ignored
    assert "GROUNDING (all extracted files): 11 exact, 8 value_present" in out                # 5+6 exact, 4+4 value_present
    assert "PATHS: 2 text_and_vision" in out and "TOKENS: in=13600 out=1840   COST: $0.045600" in out
    for name in ("vendor_name", "invoice_number", "invoice_date", "currency", "total", "line_items", "adjustments"):
        assert name in out


def test_wrong_and_missing_values_show_up_as_differences_with_confidence(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    wrong = {**KEY_10963, "total": "5338.09", "vendor_tax_id": "12-3456789", "po_reference": "PO-1", "tax": "0",
             "line_items": [{"description": "Stapler", "amount": "5141.76"}]}
    (env / "m.md").write_text(entry("superstore_10963.pdf", wrong), encoding="utf-8")
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md", "--replay", rec)
    assert code == 0 and "DIFFERENCES" in out
    assert "superstore_10963.pdf: total WRONG: expected '5338.09', extracted '5338.08'  (confidence 0.85)" in out
    assert "vendor_tax_id MISSED: expected '12-3456789', extracted None" in out
    assert "po_reference MISSED" in out and "tax MISSED" in out
    assert out.count("line_items") >= 2                                                     # in the table and in the differences
    assert "CONFIDENCE (effective, after grounding | the model's raw score)" in out and "wrong answers:" in out


def test_a_hallucinated_value_is_reported_when_the_key_says_null(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    (env / "m.md").write_text(entry("superstore_10963.pdf", {"verified": True, "vendor_name": None, "invoice_number": "10963"}), encoding="utf-8")
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md", "--replay", rec)
    assert "vendor_name HALLUCINATED: expected None, extracted 'SuperStore'" in out


def test_nothing_verified_means_nothing_scored_and_the_report_says_so(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    (env / "m.md").write_text(entry("superstore_10963.pdf", {**KEY_10963, "verified": False}), encoding="utf-8")
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md", "--replay", rec)
    assert code == 0 and "0 scored (verified), 1 unverified" in out and "nothing scored" in out
    assert "unverified entries waiting for a human check: superstore_10963.pdf" in out and "UNLABELLED (extracted, not scored): superstore_24429.pdf" in out


def test_no_manifest_at_all_still_extracts_everything(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "absent.md", "--replay", rec)
    assert code == 0 and "2 extracted" in out and "2 unlabelled" in out and "nothing scored" in out


def test_manifest_problems_and_missing_files_are_warnings_not_crashes(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    (env / "m.md").write_text(entry("ghost.pdf", {"verified": True, "total": "1"}) + "## superstore_10963.pdf\n```expected\n{oops\n```\n"
                              + "## superstore_24429.pdf\nnotes only\n", encoding="utf-8")
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md", "--replay", rec)
    assert code == 0
    assert "WARNING: manifest entries with no file in the folder: ghost.pdf" in out
    assert "WARNING: superstore_10963.pdf: the expected block is not valid JSON" in out
    assert "WARNING: manifest section 'superstore_24429.pdf' has no expected block, but a file with that name exists" in out


def test_a_failed_extraction_is_listed_not_scored_and_sets_the_exit_code(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    extra = make_source(env / "more", "native")                                # has no recording: the replay misses
    shutil.copy(extra, folder / "zz_no_recording.pdf")
    (folder / "aa_bad.pdf").write_bytes(b"this is not a pdf")
    (env / "m.md").write_text(entry("superstore_10963.pdf", KEY_10963) + entry("zz_no_recording.pdf", {"verified": True, "total": "1105.00"}),
                              encoding="utf-8")
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md", "--replay", rec)
    assert code == eval_module.EXIT_FAILURES == 1
    assert "aa_bad.pdf: REJECTED" in out and "zz_no_recording.pdf: FAILED [system_side/" in out
    assert "FAILURES (not scored)" in out and "4 processed, 2 extracted, 2 failed" in out
    assert "1 scored (verified)" in out and "2 unlabelled" in out                            # 24429 and the rejected file have no entry


def test_the_cost_ceiling_stops_the_run_and_lists_what_was_not_processed(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md", "--replay", rec, "--max-cost", "0.0001")
    assert code == 1 and "STOPPED EARLY: cost ceiling reached" in out and "not processed: superstore_24429.pdf" in out
    assert "superstore_10963.pdf: FAILED [system_side/cost_ceiling]" in out


def test_a_rejected_schema_aborts_before_any_report(env, capsys, monkeypatch, no_live_client):
    folder, rec = make_folder(env)

    class Rejecting:
        def __init__(self, *a, **k):
            pass

        def complete(self, request):
            raise LLMSchemaError("The API rejected the structured-output schema (HTTP 400): The compiled grammar is too large")

    monkeypatch.setattr(eval_module, "ReplayClient", Rejecting)
    code, out = run_main(capsys, "--dir", folder, "--manifest", env / "m.md", "--replay", rec)
    assert code == eval_module.EXIT_SCHEMA == 4
    assert "THE API REJECTED THE EXTRACTION SCHEMA" in out and "LLM_STRUCTURED_OUTPUT=prompt_json" in out and "EVAL REPORT" not in out


# ------------------------------------------------------------------------------------------- draft manifest

def test_draft_manifest_writes_unverified_drafts_and_never_touches_existing_entries(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    manifest = env / "m.md"
    manifest.write_text("My notes.\n\n" + entry("superstore_10963.pdf", KEY_10963), encoding="utf-8")
    before = manifest.read_text(encoding="utf-8")
    code, out = run_main(capsys, "--dir", folder, "--manifest", manifest, "--replay", rec, "--draft-manifest")
    assert code == 0 and "DRAFTS written to m.md" in out and "superstore_24429.pdf" in out
    assert "drafts skipped (the manifest already has an entry; left untouched): superstore_10963.pdf" in out
    after = manifest.read_text(encoding="utf-8")
    assert after.startswith(before.rstrip("\n"))                                            # the existing entry is byte-identical
    m = load_manifest(manifest)
    assert m.problems == [] and m.entries["superstore_10963.pdf"].verified is True
    draft = m.entries["superstore_24429.pdf"]
    assert draft.verified is False and draft.expected["total"] == "1770.61" and "CHECK EVERY VALUE" in draft.notes[0]
    assert draft.expected["adjustments"] == [{"kind": "discount", "amount": "-184.59", "description": "Discount (10%)"},
                                             {"kind": "shipping", "amount": "109.26", "description": "Shipping"}]
    assert after.count('"verified": true') == 1                                              # only the human's entry


def test_drafts_are_never_scored_even_on_the_next_run(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    manifest = env / "m.md"
    run_main(capsys, "--dir", folder, "--manifest", manifest, "--replay", rec, "--draft-manifest")
    m = load_manifest(manifest)
    assert m.problems == [] and len(m.entries) == 2 and not any(e.verified for e in m.entries.values())
    code, out = run_main(capsys, "--dir", folder, "--manifest", manifest, "--replay", rec)
    assert "0 scored (verified), 2 unverified" in out and "nothing scored" in out


def test_draft_manifest_skips_failed_files(env, capsys, no_live_client):
    folder, rec = make_folder(env)
    shutil.copy(make_source(env / "more", "native"), folder / "zz_no_recording.pdf")
    manifest = env / "m.md"
    code, out = run_main(capsys, "--dir", folder, "--manifest", manifest, "--replay", rec, "--draft-manifest")
    assert code == 1 and "zz_no_recording.pdf" not in manifest.read_text(encoding="utf-8")


def test_draft_manifest_on_an_empty_folder_creates_nothing(env, capsys, no_live_client):
    (env / "empty").mkdir()
    code, out = run_main(capsys, "--dir", env / "empty", "--manifest", env / "m.md", "--draft-manifest")
    assert code == 0 and not (env / "m.md").exists()
