"""python -m app.pipeline.cli: one command, every step printed, everything persisted. It refuses live calls without --live."""
import json
from decimal import Decimal

import pytest

from app.config import get_settings
from app.db.connection import connect
from app.extraction import eval as eval_module
from app.llm.errors import LLMSchemaError
from app.llm.replay import RecordingClient
from app.pipeline import cli, persist
from app.pipeline.runner import run_pipeline
from tests.extraction.real import REAL, real_pdf
from tests.pipeline.doubles import ModelDouble
from tests.pipeline.helpers import LABEL_CONTROLLED, cfg, controlled_variant_reply, demo_db, one

CANARY = "sk-ant-api03-PIPELINE-CANARY-do-not-leak-0123456789"
SS = "superstore_10963"


def v4(name):
    return json.loads((REAL / f"{name}.v4.reply.json").read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def no_live_client(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the real API client was constructed")

    monkeypatch.setattr(eval_module, "AnthropicClient", boom)


def record(env, name, reply, rec, remove_pos=False):
    """Run the pipeline once with a recording model double, on its own demo DB, so the CLI can replay everything (extraction,
    explanation and draft) offline against an identical fresh demo DB."""
    conn = demo_db(env / "recording", f"rec-{name}.db")
    if remove_pos:
        with persist.transaction(conn):
            conn.execute("DELETE FROM po_consumption")          # schema v2: allocation rows first
            conn.execute("DELETE FROM ledger_entries")
            conn.execute("UPDATE invoices SET po_id = NULL")
            conn.execute("DELETE FROM po_lines")
            conn.execute("DELETE FROM purchase_orders")
    run_pipeline(real_pdf(name), conn, client=RecordingClient(ModelDouble(reply), rec), settings=cfg(env / "recording", explain_with_llm=True,
                                                                                                    draft_with_llm=True))
    conn.close()


def run_cli(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    return code, capsys.readouterr().out


# ----------------------------------------------------------------------------------- no --live, no live call

@pytest.mark.parametrize("extra", [[], ["--record", "REC"], ["--max-cost", "1"], ["--json"], ["--reset-demo"]])
def test_without_live_or_replay_it_refuses_and_builds_no_client_and_touches_no_database(env, capsys, no_live_client, extra):
    db = env / "app.db"
    argv = [real_pdf(SS), "--db", db, *[str(env / "rec") if x == "REC" else x for x in extra]]
    code, out = run_cli(capsys, *argv)
    assert code == cli.EXIT_LIVE_REFUSED == 5
    assert "REFUSED: no live API call was made (nothing was sent, nothing was spent)" in out and "--live" in out and "--replay DIR" in out
    assert not db.exists() and not (env / "runs").exists() and not (env / "rec").exists()


def test_live_without_a_key_is_a_clear_error_and_writes_nothing(env, capsys):
    db = env / "app.db"
    code, out = run_cli(capsys, real_pdf(SS), "--db", db, "--reset-demo", "--live")
    assert code == cli.EXIT_NOT_CONFIGURED == 3 and "NOT CONFIGURED" in out and "ANTHROPIC_API_KEY" in out and "--replay" in out
    c = connect(db)
    assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0                                        # the reset happened, no run
    c.close()


def test_replay_cannot_be_combined_and_bad_arguments_are_usage_errors(env, capsys, no_live_client):
    assert run_cli(capsys, real_pdf(SS), "--replay", env, "--live")[0] == 2
    assert run_cli(capsys, real_pdf(SS), "--replay", env, "--record", env / "x")[0] == 2
    code, out = run_cli(capsys, real_pdf(SS), "--replay", env / "missing")
    assert code == 2 and "is not a folder of recorded responses" in out
    assert run_cli(capsys, real_pdf(SS), "--replay", env, "--max-cost", "0")[0] == 2


def test_a_missing_database_is_explained_not_created(env, capsys, no_live_client):
    code, out = run_cli(capsys, real_pdf(SS), "--replay", env, "--db", env / "nope.db")
    assert code == 2 and "does not exist" in out and "--reset-demo" in out and not (env / "nope.db").exists()


def test_a_file_that_is_not_accepted_creates_no_run(env, capsys, no_live_client):
    bad = env / "notes.txt"
    bad.write_text("not an invoice", encoding="utf-8")
    demo_db(env).close()
    code, out = run_cli(capsys, bad, "--replay", env, "--db", env / "app.db")
    assert code == 2 and "REJECTED [" in out and "no run was created" in out
    c = connect(env / "app.db")
    assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    c.close()


# ---------------------------------------------------------------------------------------- the full output

def test_a_replayed_run_prints_every_step_and_writes_to_the_database(env, capsys, no_live_client):
    rec = env / "rec"
    record(env, SS, v4(SS), rec)
    code, out = run_cli(capsys, real_pdf(SS), "--replay", rec, "--db", env / "app.db", "--reset-demo")
    assert code == 0
    for title in ("=== 1. ingest ===", "=== 2. extraction ===", "=== 3. match ===", "=== 4. validation ===", "=== 5. decision ===",
                  "=== 6. explanation ===", "=== 7. actions and drafts (nothing is sent) ===", "=== 8. written to the database ===",
                  "=== 9. tokens and cost ==="):
        assert title in out, title
    assert "REPLAY MODE" in out and "Database reset to the demo dataset" in out
    assert "sha256:" in out and "text layer:  usable (usable=True)" in out
    assert "invoice_number 10963" in out and "5338.08" in out and "USD" in out
    assert "vendor: SuperStore (exact_name, score 1.00)" in out and "PO-SS-001: score" in out
    assert out.count("[ok  ]") == 14 and "[FLAG] r_po_found" in out
    assert "\nREVIEW" in out and "[source: llm, model claude-sonnet-5]" in out and "This invoice needs a person to review it." in out
    assert "review queue: open: Review: r_po_found (matched_without_reference)" in out
    assert "invoices" in out and "review_queue" in out and "runs           id" in out and "final_decision review" in out
    assert "total     in=8100 out=1300  $0.029200" in out
    c = connect(env / "app.db")
    assert c.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 1 and c.execute("SELECT status FROM runs").fetchone()[0] == "completed"
    c.close()


def test_a_request_for_information_prints_the_full_draft_and_says_nothing_was_sent(env, capsys, no_live_client):
    rec = env / "rec"
    record(env, SS, v4(SS), rec, remove_pos=True)
    conn = demo_db(env, "app.db")
    with persist.transaction(conn):
        conn.execute("DELETE FROM po_consumption")          # schema v2: allocation rows first
        conn.execute("DELETE FROM ledger_entries")
        conn.execute("UPDATE invoices SET po_id = NULL")
        conn.execute("DELETE FROM po_lines")
        conn.execute("DELETE FROM purchase_orders")
    conn.close()
    code, out = run_cli(capsys, real_pdf(SS), "--replay", rec, "--db", env / "app.db")
    assert code == 0 and "\nREQUEST_INFO" in out
    assert "draft (vendor_email, status draft, source llm, model claude-sonnet-5): to: (none: no vendor contact on file)" in out
    assert "Subject: Invoice 10963: information needed" in out and "Dear SuperStore," in out and "Kind regards,\nAccounts Payable" in out
    assert "drafts" in out and "vendor_email (status draft, never sent)" in out


def test_json_flag_prints_the_extracted_invoice(env, capsys, no_live_client):
    rec = env / "rec"
    record(env, SS, v4(SS), rec)
    code, out = run_cli(capsys, real_pdf(SS), "--replay", rec, "--db", env / "app.db", "--reset-demo", "--json")
    assert code == 0 and '"invoice_number": {' in out and '"value": "5338.08"' in out


def test_the_synthetic_controlled_variant_is_labelled_in_the_output_it_produces(env, capsys, no_live_client):
    """%s""" % LABEL_CONTROLLED
    name = "superstore_24429"
    reply = controlled_variant_reply(name, "PO-SS-002")
    reply.update({k: v for k, v in v4(name).items() if k in ("line_items", "adjustments", "document_quality")})
    rec = env / "rec"
    record(env, name, reply, rec)
    code, out = run_cli(capsys, real_pdf(name), "--replay", rec, "--db", env / "app.db", "--reset-demo", "--json")
    assert code == 0 and "\nAPPROVE" in out and "ledger: committed 1,770.61 on PO-SS-002; derived PO balance 2,500.00 -> 729.39" in out
    assert "SYNTHETIC CONTROLLED VARIANT" in out                                                            # visible in the extraction notes (--json)


def test_a_failed_run_is_reported_and_exits_1(env, capsys, monkeypatch, no_live_client):
    rec = env / "rec"
    record(env, SS, v4(SS), rec)
    monkeypatch.setattr(persist, "save_invoice", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    code, out = run_cli(capsys, real_pdf(SS), "--replay", rec, "--db", env / "app.db", "--reset-demo")
    assert code == 1 and "RUN FAILED (rolled back): RuntimeError: disk on fire" in out and "NONE (run failed)" in out


def test_reset_demo_wipes_earlier_runs(env, capsys, no_live_client):
    rec = env / "rec"
    record(env, SS, v4(SS), rec)
    for _ in range(2):
        assert run_cli(capsys, real_pdf(SS), "--replay", rec, "--db", env / "app.db", "--reset-demo")[0] == 0
    c = connect(env / "app.db")
    assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1                                       # the second reset removed the first run
    c.close()


def test_running_twice_without_reset_finds_the_duplicate_and_drafts_a_reject(env, capsys, no_live_client):
    rec = env / "rec"
    record(env, SS, v4(SS), rec)
    run_cli(capsys, real_pdf(SS), "--replay", rec, "--db", env / "app.db", "--reset-demo")
    code, out = run_cli(capsys, real_pdf(SS), "--replay", rec, "--db", env / "app.db")
    assert code == 0 and "\nREJECT" in out and "[FAIL] r_duplicate_exact" in out
    c = connect(env / "app.db")
    assert c.execute("SELECT COUNT(*) FROM runs WHERE final_decision = 'reject'").fetchone()[0] == 1
    c.close()


# ------------------------------------------------------------------------------------------ the live path

def test_live_with_a_key_records_and_never_prints_the_key(env, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    get_settings.cache_clear()
    double = ModelDouble(v4(SS))

    class FakeLive:
        def __init__(self, settings):
            assert settings.api_key_value() == CANARY

        def complete(self, request):
            return double.complete(request)

    monkeypatch.setattr(eval_module, "AnthropicClient", FakeLive)
    code, out = run_cli(capsys, real_pdf(SS), "--live", "--record", env / "rec", "--db", env / "app.db", "--reset-demo", "--max-cost", "0.5")
    assert code == 0 and CANARY not in out and "LIVE MODE: this run calls the paid API" in out
    assert double.calls == {"extract": 1, "explain": 1, "draft": 0} and len(list((env / "rec").glob("*.json"))) == 2
    # what was recorded replays offline into a fresh database
    code, out = run_cli(capsys, real_pdf(SS), "--replay", env / "rec", "--db", env / "again.db", "--reset-demo")
    assert code == 0 and "[source: llm" in out


def test_a_schema_rejection_shows_the_actionable_message_and_exits_4(env, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    get_settings.cache_clear()

    class Rejecting:
        def __init__(self, settings):
            pass

        def complete(self, request):
            raise LLMSchemaError("The API rejected the structured-output schema (HTTP 400): The compiled grammar is too large")

    monkeypatch.setattr(eval_module, "AnthropicClient", Rejecting)
    code, out = run_cli(capsys, real_pdf(SS), "--live", "--db", env / "app.db", "--reset-demo")
    assert code == cli.EXIT_SCHEMA == 4 and "THE API REJECTED THE EXTRACTION SCHEMA" in out and "LLM_STRUCTURED_OUTPUT=prompt_json" in out and CANARY not in out
