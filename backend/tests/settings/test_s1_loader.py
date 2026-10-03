"""Rules settings, stage S1: schema v4 + migration, the loader's effective settings (global -> PO override), the engine using them
after matching, the per-run `settings_applied` record, the six-invoice regression with default settings, and escalate-only."""
import random
import sqlite3
from contextlib import closing

import pytest

from app.config import Settings
from app.db import migrate as migrate_mod
from app.db.connection import connect
from app.db.init_db import SCHEMA_VERSION, SchemaOutdated, check_schema, schema_version
from app.db.reset import reset_database
from app.engine.engine import run_validate_stage
from app.engine.loader import load_effective, load_facts, load_rules
from app.rulesettings import catalog
from tests.factories import make_ctx, make_extracted, make_facts
from tests.gmail.test_stage1_schema_crypto import make_v2
from tests.pipeline.helpers import DEMO, controlled_variant_reply, demo_db, run_real
from tests.settings.helpers import events, po_id, set_global_param, set_po, switch_po
from tests.test_schema_v2 import make_v1, tables

SETTINGS_TABLES = {"po_settings", "po_rule_switches", "settings_events"}


def make_v3(path):
    make_v2(path)
    with closing(connect(path)) as c:
        migrate_mod.migrate_2_to_3(c)
    return path


# ------------------------------------------------------------------------------------------ schema v4 and the migration

def test_a_v3_database_migrates_to_v4_with_a_backup_and_nothing_else_changed(tmp_path):
    db = make_v3(tmp_path / "app.db")
    before = db.read_bytes()
    with closing(connect(db)) as c:
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("vendors", "purchase_orders", "ledger_entries")}
    message = migrate_mod.migrate(db)
    backups = list(tmp_path.glob("app.db.v3-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == before and "from schema version 3 to 4" in message
    assert "Rules-settings tables created" in message
    with closing(connect(db)) as c:
        assert schema_version(c) == 4 == SCHEMA_VERSION and SETTINGS_TABLES <= tables(c)
        assert {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts} == counts



def test_the_3_to_4_migration_keeps_a_stored_gmail_connection_and_its_imports(tmp_path):
    """The owner migrates a database that holds a Gmail connection: `migrate` must keep both Gmail tables row for row
    (unlike `--reset-demo`, which rebuilds the database). The token blob here is a dummy, never a real token."""
    db = make_v3(tmp_path / "app.db")
    with closing(connect(db)) as c, c:
        c.execute("INSERT INTO oauth_credentials (provider, account_email, scopes, refresh_token_enc, key_fingerprint) "
                  "VALUES ('google', 'owner@example.com', 'https://www.googleapis.com/auth/gmail.readonly', ?, 'fp')", (b"dummy-ciphertext",))
        c.execute("INSERT INTO gmail_imports (account_email, message_id, attachment_sha256, part_id, filename, mime_type, size_bytes, "
                  "run_id) VALUES ('owner@example.com', 'm1', 'abc', '1', 'a.pdf', 'application/pdf', 10, 'r1')")
    rows = lambda c: ([tuple(r) for r in c.execute("SELECT * FROM oauth_credentials")],
                      [tuple(r) for r in c.execute("SELECT * FROM gmail_imports")])
    with closing(connect(db)) as c:
        before = rows(c)
    migrate_mod.migrate(db)
    with closing(connect(db)) as c:
        assert schema_version(c) == 4 and rows(c) == before and before[0][0][4] == b"dummy-ciphertext"

def test_a_v1_database_goes_to_v4_with_one_backup_per_step(tmp_path):
    db = make_v1(tmp_path / "app.db")
    migrate_mod.migrate(db)
    assert sorted(p.name.split(".")[2][:2] for p in tmp_path.glob("*.bak")) == ["v1", "v2", "v3"]
    with closing(connect(db)) as c:
        assert schema_version(c) == 4


def test_a_failing_3_to_4_step_rolls_back(tmp_path, monkeypatch, capsys):
    db = make_v3(tmp_path / "app.db")
    real = migrate_mod.schema_statements
    monkeypatch.setattr(migrate_mod, "schema_statements", lambda sql: [*real(sql), "INSERT INTO nowhere VALUES (1)"])
    assert migrate_mod.main(["--db", str(db)]) == 1
    assert "schema version 3" in capsys.readouterr().out
    with closing(connect(db)) as c:
        assert schema_version(c) == 3 and not SETTINGS_TABLES & tables(c)


def test_programs_refuse_a_v3_database(tmp_path, capsys, monkeypatch):
    import uvicorn
    from app.api import serve
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("served a v3 database"))
    db = make_v3(tmp_path / "old.db")
    with closing(connect(db)) as c:
        with pytest.raises(SchemaOutdated, match=r"schema version 3.*rules settings.*app\.db\.migrate"):
            check_schema(c, db)
    assert serve.main(["--offline", "--db", str(db)]) == serve.EXIT_USAGE


def test_the_v4_constraints(conn):
    with conn:
        vid = conn.execute("INSERT INTO vendors (name, status) VALUES ('V', 'approved')").lastrowid
        pid = conn.execute("INSERT INTO purchase_orders (po_number, vendor_id, currency, total_amount, status) "
                           "VALUES ('P1', ?, 'USD', 100, 'open')", (vid,)).lastrowid
    bad = [("INSERT INTO po_settings (po_id, tolerance_pct) VALUES (?, 25.01)", (pid,)),
           ("INSERT INTO po_settings (po_id, confidence_threshold) VALUES (?, 0.4)", (pid,)),
           ("INSERT INTO po_settings (po_id, tolerance_mode) VALUES (?, 'whatever')", (pid,)),
           ("INSERT INTO po_settings (po_id, duplicate_days) VALUES (?, 91)", (pid,)),
           ("INSERT INTO po_rule_switches (po_id, rule_id, enabled) VALUES (?, 'r_duplicate_exact', 0)", (pid,)),
           ("INSERT INTO po_rule_switches (po_id, rule_id, enabled) VALUES (?, 'r_vendor_status', 0)", (pid,)),
           ("INSERT INTO settings_events (scope, po_id, key, message) VALUES ('global', ?, 'k', 'm')", (pid,)),
           ("INSERT INTO settings_events (scope, key, message) VALUES ('po', 'k', 'm')", ())]
    for sql, args in bad:
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(sql, args)
    with conn:
        conn.execute("INSERT INTO po_rule_switches (po_id, rule_id, enabled) VALUES (?, 'r_duplicate_exact', 1)", (pid,))  # on is fine


def test_reset_clears_the_settings_tables(tmp_path):
    db = tmp_path / "r.db"
    reset_database(db, DEMO)
    with closing(connect(db)) as c:
        set_po(c, 1, tolerance_pct=5.0)
        with c:
            c.execute("INSERT INTO settings_events (scope, key, message) VALUES ('global', 'k', 'm')")
    reset_database(db, DEMO)
    with closing(connect(db)) as c:
        assert [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(SETTINGS_TABLES)] == [0, 0, 0]


# ------------------------------------------------------------------------------------------ the loader: layering

def test_with_nothing_stored_the_effective_settings_are_exactly_todays(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        for pid in (None, 1, 5):
            eff = load_effective(c, pid)
            assert eff.rules == load_rules(c)                                         # the very same rules
            assert eff.runtime == load_facts(c).settings
            assert set(eff.record["sources"].values()) == {"default"} and set(eff.record["rule_sources"].values()) == {"default"}
        assert load_effective(c, None).record["scope"] == "global" and load_effective(c, 1).record["po_number"] == "PO-SS-001"


def test_a_po_override_wins_and_other_pos_keep_the_default(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        set_po(c, 1, tolerance_pct=5.0, tolerance_abs="100.00", confidence_threshold=0.95, duplicate_days=14)
        switch_po(c, 1, "r_po_line_price", False)
        eff, other = load_effective(c, 1), load_effective(c, 2)
    tol = next(r for r in eff.rules if r.id == "r_tolerance_pct").params
    assert (tol["pct"], tol["abs"], tol["mode"]) == (5.0, 100.0, "lesser_of")
    assert next(r for r in eff.rules if r.id == "r_duplicate_fuzzy").params["days"] == 14
    assert next(r for r in eff.rules if r.id == "r_po_line_price").enabled is False
    assert eff.runtime.confidence_threshold == 0.95
    assert eff.record["sources"] == {"tolerance_pct": "override", "tolerance_abs": "override", "tolerance_mode": "default",
                                     "confidence_threshold": "override", "duplicate_days": "override", "duplicate_amount": "default"}
    assert eff.record["values"]["tolerance_abs"] == "100.00" and eff.record["rule_sources"]["r_po_line_price"] == "override"
    other_tol = next(r for r in other.rules if r.id == "r_tolerance_pct").params
    assert (other_tol["pct"], other_tol["abs"]) == (2.0, 50.0) and other.runtime.confidence_threshold == 0.8


def test_a_global_change_reaches_inherited_keys_but_not_overridden_ones(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        set_po(c, 1, tolerance_pct=5.0)
        set_global_param(c, "r_tolerance_pct", pct=3.0, abs=75.0)
        tol1 = next(r for r in load_effective(c, 1).rules if r.id == "r_tolerance_pct").params
        tol2 = next(r for r in load_effective(c, 2).rules if r.id == "r_tolerance_pct").params
    assert (tol1["pct"], tol1["abs"]) == (5.0, 75.0) and (tol2["pct"], tol2["abs"]) == (3.0, 75.0)


def test_locked_rules_stay_on_whatever_a_switch_says(tmp_path, monkeypatch):
    with closing(demo_db(tmp_path)) as c:
        monkeypatch.setattr(catalog, "po_override_switches", lambda conn, pid: {"r_vendor_status": False, "r_duplicate_exact": False})
        eff = load_effective(c, 1)
    assert all(r.enabled for r in eff.rules if r.id in ("r_vendor_status", "r_duplicate_exact"))
    assert eff.record["rule_sources"]["r_vendor_status"] == "default"


# ------------------------------------------------------------------------------------------ the engine uses the PO's settings

def _over_balance(c, tmp_path, excess_cents: int):
    """SYNTHETIC: invoice 24429 (1,770.61) with PO-SS-002 edited in, against a PO-SS-002 balance `excess_cents` below the invoice."""
    pid = po_id(c, "PO-SS-002")
    with c:
        vid = c.execute("SELECT vendor_id FROM purchase_orders WHERE id = ?", (pid,)).fetchone()[0]
        inv = c.execute("INSERT INTO invoices (vendor_id, invoice_number, currency, total, po_id, status) "
                        "VALUES (?, 'HIST-T', 'USD', 1, ?, 'approved')", (vid, pid)).lastrowid
        c.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (?, ?, ?, 'commit')",
                  (pid, inv, 250000 - 177061 + excess_cents))
        c.execute("INSERT INTO po_consumption (ledger_entry_id, po_id, invoice_id, amount, type, matched_by) "
                  "VALUES (last_insert_rowid(), ?, ?, ?, 'commit', 'legacy')", (pid, inv, 250000 - 177061 + excess_cents))
    return pid, run_real(c, tmp_path, "superstore_24429", controlled_variant_reply("superstore_24429", "PO-SS-002"))


def test_the_po_tolerance_decides_an_over_balance_invoice(tmp_path):
    with closing(demo_db(tmp_path, "a.db")) as c:
        pid, r = _over_balance(c, tmp_path, 6000)                               # 60.00 over: global min(2% = 34.21, 50) -> review
        assert r.ctx.decision.value == "review"
        tol = next(x for x in r.ctx.rule_results if x.rule_id == "r_tolerance_pct")
        assert tol.outcome_key == "over_tolerance"
    with closing(demo_db(tmp_path, "b.db")) as c:
        set_po(c, po_id(c, "PO-SS-002"), tolerance_pct=5.0, tolerance_abs="100.00")
        pid, r = _over_balance(c, tmp_path, 6000)                               # PO: min(5% = 85.53, 100) -> within
        assert r.ctx.decision.value == "approve"
        [applied] = events(c, r.run_id, "settings_applied")
    assert applied["stage"] == "validate" and applied["detail"]["scope"] == "po" and applied["detail"]["po_number"] == "PO-SS-002"
    assert applied["detail"]["values"]["tolerance_pct"] == 5.0 and applied["detail"]["sources"]["tolerance_abs"] == "override"
    assert "2 overridden: tolerance_pct, tolerance_abs" in applied["message"]


def test_a_po_confidence_threshold_and_a_switched_off_rule_apply(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        pid = po_id(c, "PO-SS-002")
        set_po(c, pid, confidence_threshold=0.99)
        switch_po(c, pid, "r_po_line_price", False)
        r = run_real(c, tmp_path, "superstore_24429", controlled_variant_reply("superstore_24429", "PO-SS-002"))
        skipped = [e["rule_id"] for e in events(c, r.run_id, "rule_skipped")]
    assert r.ctx.decision.value == "review" and "r_po_line_price" in skipped
    conf = next(x for x in r.ctx.rule_results if x.rule_id == "r_extraction_confidence")
    assert conf.outcome.value == "flag"


def test_an_unmatched_invoice_uses_the_global_defaults_even_if_pos_have_overrides(tmp_path):
    from tests.extraction.real import real_reply
    from tests.extraction.wire_convert import set_field
    with closing(demo_db(tmp_path)) as c:
        for pid in range(1, 7):
            set_po(c, pid, tolerance_pct=9.0)
        reply = real_reply("superstore_10963")                                  # SYNTHETIC: an unknown vendor and an unrelated line
        set_field(reply, "vendor_name", found=True, value="Nobody Trading Ltd", page=1, source_text="Nobody Trading Ltd", confidence=0.9)
        reply["line_items"][0].update(description="Unrelated consulting hours", item_code="")
        r = run_real(c, tmp_path, "superstore_10963", reply)
        [applied] = events(c, r.run_id, "settings_applied")
    assert r.ctx.match_status.value != "matched"
    assert applied["detail"]["scope"] == "global" and applied["detail"]["values"]["tolerance_pct"] == 2.0
    assert applied["message"] == "Settings used: the global defaults (no confidently matched PO)."


def test_the_record_of_an_old_run_never_changes(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        r1 = run_real(c, tmp_path, "superstore_10963")
        first = events(c, r1.run_id, "settings_applied")[0]
        set_po(c, po_id(c, "PO-SS-001"), tolerance_pct=7.5)
        set_global_param(c, "r_tolerance_pct", abs=10.0)
        again = events(c, r1.run_id, "settings_applied")[0]
        decision = c.execute("SELECT final_decision FROM runs WHERE id = ?", (r1.run_id,)).fetchone()[0]
    assert again == first and first["detail"]["values"]["tolerance_pct"] == 2.0 and decision == "review"


def test_the_review_preview_uses_the_pos_tolerance(tmp_path):
    from tests.po.export_helpers import db
    from tests.api.helpers import api, run_and_wait
    from tests.review.helpers import SS_10963, item_for, runs_with_scenarios
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        iid = item_for(c, run_and_wait(c, SS_10963))
        before = c.get(f"/api/review-queue/{iid}").json()["approve"]["tolerance"]
        with db(c) as conn:
            set_po(conn, 1, tolerance_pct=4.0, tolerance_abs="12.34", tolerance_mode="greater_of")
        after = c.get(f"/api/review-queue/{iid}").json()["approve"]["tolerance"]
    assert before == {"pct": 2.0, "abs": "50.00", "mode": "lesser_of"} and after == {"pct": 4.0, "abs": "12.34", "mode": "greater_of"}


# ------------------------------------------------------------------------------------------ the six real invoices, default settings

EXPECTED = {  # STATUS.md "six real invoices" table (line-item build) and GMAIL_STAGE_REPORT.md section 4
    "superstore_10963": ("review", "PO-SS-001", ["r_po_found:matched_without_reference"]),
    "superstore_24429": ("review", "PO-SS-002", ["r_po_found:matched_without_reference"]),
    "superstore_14021": ("review", "PO-SS-003", ["r_po_found:matched_without_reference"]),
    "superstore_14130": ("review", "PO-SS-005", ["r_po_found:matched_without_reference"]),
    "superstore_6459": ("review", "PO-SS-004", ["r_po_found:matched_without_reference"]),
    "iq_electronics": ("review", "PO-IQ-2025-001", ["engine_floor:floor_applied", "r_extraction_confidence:low_confidence",
                                                     "r_po_found:matched_without_reference"]),
}


def test_the_six_real_invoices_are_unchanged_with_default_settings(tmp_path):
    from tests.gmail.regression import via_upload
    out = via_upload(tmp_path / "six")
    for name, (decision, po, triggered) in EXPECTED.items():
        o = out[name]
        assert (o["decision"], o["matched_po"], o["triggered"], o["rule_results"]) == (decision, po, triggered, 16), name


def test_the_six_runs_record_their_pos_settings_all_inherited(tmp_path):
    from tests.api.helpers import build_app
    from tests.gmail.helpers import gmail_settings
    from tests.gmail.regression import SIX, HashRuns
    from tests.extraction.real import real_pdf
    from fastapi.testclient import TestClient
    app, worker, db, settings = build_app(tmp_path, settings=gmail_settings(tmp_path), run_fn=HashRuns())
    with TestClient(app) as client:
        ids = {n: client.post("/api/runs", files={"file": (f, real_pdf(n).read_bytes(), "application/pdf")}).json()["run_id"]
               for n, (_, _, f) in SIX.items()}
        assert worker.wait_idle(180)
    with closing(connect(db)) as c:
        for name, rid in ids.items():
            [applied] = events(c, rid, "settings_applied")
            assert applied["detail"]["po_number"] == EXPECTED[name][1]
            assert applied["message"].endswith("(all inherited from the global defaults).")


# ------------------------------------------------------------------------------------------ escalate-only, whatever the settings

def _random_overrides(rng: random.Random) -> dict:
    out = {}
    if rng.random() < 0.7:
        out["tolerance_pct"] = round(rng.uniform(0, 25), 2)
    if rng.random() < 0.7:
        out["tolerance_abs"] = f"{rng.uniform(0, 5000):.2f}"
    if rng.random() < 0.5:
        out["tolerance_mode"] = rng.choice(catalog.MODES)
    if rng.random() < 0.7:
        out["confidence_threshold"] = round(rng.uniform(0.5, 0.99), 2)
    if rng.random() < 0.5:
        out["duplicate_days"] = rng.randint(0, 90)
    return out


def _contexts():
    yield make_ctx()
    yield make_ctx(matched=False)
    yield make_ctx(extracted=make_extracted(total=None))
    yield make_ctx(facts=make_facts(threshold=0.99))


def test_escalate_only_holds_for_any_effective_settings_and_switches(tmp_path):
    rng = random.Random(4242)
    with closing(demo_db(tmp_path)) as c:
        for trial in range(25):
            with c:
                c.execute("DELETE FROM po_settings")
                c.execute("DELETE FROM po_rule_switches")
            set_po(c, 1, **_random_overrides(rng))
            off = rng.sample(catalog.SWITCHABLE_RULES, rng.randint(0, 5))
            all_on = load_effective(c, 1)
            for rule_id in off:
                switch_po(c, 1, rule_id, False)
            some_off = load_effective(c, 1)
            for ctx in _contexts():
                ctx.facts = None if ctx.facts is None else ctx.facts.model_copy(update={"settings": some_off.runtime})
                full = run_validate_stage(ctx.model_copy(deep=True), all_on.rules)
                part_ctx = ctx.model_copy(deep=True)
                part = run_validate_stage(part_ctx, some_off.rules)
                results = part_ctx.rule_results
                ids = [x.rule_id for x in results]
                assert "engine_floor" in ids and "engine_floor_reference" in ids                       # floors always present
                triggered = [x.severity for x in results if x.outcome.value in ("flag", "fail")]
                assert part.outputs["final_severity"] == max(triggered, default=0)                     # final = max, never lowered
                full_by_id = {x.rule_id: (x.outcome, x.severity) for x in _results(ctx, all_on.rules)}
                for x in results:                                                                       # switching off only removes
                    assert full_by_id[x.rule_id] == (x.outcome, x.severity)
                assert not set(off) & set(ids)


def _results(ctx, rules):
    c = ctx.model_copy(deep=True)
    run_validate_stage(c, rules)
    return c.rule_results
