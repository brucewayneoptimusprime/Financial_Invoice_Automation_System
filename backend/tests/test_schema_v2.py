"""Schema v2 (line-item PO consumption), stage 1: the migration with a backup, the backfill of old whole-PO commits, the
allocation invariant, the constraints, and the refusal of a version-1 database by the programs that open one."""
import sqlite3
from contextlib import closing

import pytest

from app.api import serve
from app.db import migrate as migrate_mod
from app.db.connection import connect
from app.db.consumption import consumption_problems
from app.db.init_db import SCHEMA_PATH, SCHEMA_VERSION, SchemaOutdated, check_schema, init_db, schema_version
from app.db.queries import get_po_balance_minor
from app.db.reset import reset_database
from app.pipeline import cli as pipeline_cli
from tests.pipeline.helpers import DEMO, controlled_variant_reply, demo_db, run_real


def make_v1(path):
    """A database exactly as the previous software left it: schema.sql only, version 1, with commits and a reversal."""
    c = sqlite3.connect(path)
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute("PRAGMA user_version = 1")
    c.executescript("""
        INSERT INTO vendors (id, name, status) VALUES (1, 'Acme', 'approved');
        INSERT INTO purchase_orders (id, po_number, vendor_id, currency, total_amount, status) VALUES (1, 'PO-1', 1, 'USD', 100000, 'partially_billed');
        INSERT INTO purchase_orders (id, po_number, vendor_id, currency, total_amount, status) VALUES (2, 'PO-2', 1, 'USD', 50000, 'open');
        INSERT INTO po_lines (po_id, line_no, description, quantity, unit_price, amount) VALUES (1, 1, 'Widget', '10', '100.00', 100000);
        INSERT INTO runs (id, source_file, status, final_decision) VALUES ('run-a', 'a.pdf', 'completed', 'approve');
        INSERT INTO invoices (id, run_id, vendor_id, invoice_number, currency, total, po_id, decision, status)
            VALUES (1, 'run-a', 1, 'INV-1', 'USD', 30000, 1, 'approve', 'approved');
        INSERT INTO invoices (id, run_id, vendor_id, invoice_number, currency, total, po_id, decision, status)
            VALUES (2, NULL, 1, 'HIST-1', 'USD', 12000, 1, NULL, 'approved');
        INSERT INTO ledger_entries (id, po_id, invoice_id, amount, type, created_at) VALUES (1, 1, 1, 30000, 'commit', '2026-01-01T00:00:00Z');
        INSERT INTO ledger_entries (id, po_id, invoice_id, amount, type, created_at) VALUES (2, 1, 2, 12000, 'commit', '2026-01-02T00:00:00Z');
        INSERT INTO ledger_entries (id, po_id, invoice_id, amount, type, created_at) VALUES (3, 1, 2, -12000, 'reversal', '2026-01-03T00:00:00Z');
    """)
    c.commit()
    c.close()
    return path


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ------------------------------------------------------------------------------------------ migration

def test_a_v1_database_migrates_with_a_byte_identical_backup_and_unchanged_balances(tmp_path):
    db = make_v1(tmp_path / "app.db")
    before_bytes = db.read_bytes()
    with closing(connect(db)) as c:
        balances = {po: get_po_balance_minor(c, po) for po in (1, 2)}
    message = migrate_mod.migrate(db)
    backups = list(tmp_path.glob("app.db.v1-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == before_bytes and "3 existing ledger entries" in message
    with closing(connect(db)) as c:
        assert schema_version(c) == SCHEMA_VERSION and {"po_consumption", "invoice_line_matches"} <= tables(c)
        assert {po: get_po_balance_minor(c, po) for po in (1, 2)} == balances
        rows = [dict(r) for r in c.execute("SELECT * FROM po_consumption ORDER BY ledger_entry_id")]
        assert consumption_problems(c) == []
    assert [(r["ledger_entry_id"], r["amount"], r["type"], r["po_line_id"], r["invoice_line_id"], r["quantity"], r["matched_by"], r["run_id"])
            for r in rows] == [(1, 30000, "commit", None, None, None, "legacy", "run-a"),
                               (2, 12000, "commit", None, None, None, "legacy", None),
                               (3, -12000, "reversal", None, None, None, "legacy", None)]
    assert rows[0]["created_at"] == "2026-01-01T00:00:00Z"                  # the allocation keeps the entry's time


def test_migrating_twice_is_a_no_op(tmp_path):
    db = make_v1(tmp_path / "app.db")
    migrate_mod.migrate(db)
    assert "nothing to do" in migrate_mod.migrate(db)
    assert len(list(tmp_path.glob("*.bak"))) == 3                         # one backup per step: v1 -> 2, v2 -> 3, v3 -> 4 (schema v4)
    with closing(connect(db)) as c:
        assert c.execute("SELECT COUNT(*) FROM po_consumption").fetchone()[0] == 3


def test_a_failed_check_rolls_everything_back(tmp_path, monkeypatch):
    db = make_v1(tmp_path / "app.db")
    before = db.read_bytes()
    monkeypatch.setattr(migrate_mod, "consumption_problems", lambda conn: ["ledger entry 9: injected"])
    assert migrate_mod.main(["--db", str(db)]) == 1
    with closing(connect(db)) as c:
        assert schema_version(c) == 1 and "po_consumption" not in tables(c)
    assert list(tmp_path.glob("*.bak"))[0].read_bytes() == before


def test_migrate_refuses_what_it_cannot_migrate(tmp_path, capsys):
    assert migrate_mod.main(["--db", str(tmp_path / "missing.db")]) == 2
    (tmp_path / "x.db").write_text("not sqlite")
    assert migrate_mod.main(["--db", str(tmp_path / "x.db")]) == 2
    bad = tmp_path / "v7.db"
    with closing(sqlite3.connect(bad)) as c:
        c.execute("PRAGMA user_version = 7")
    assert migrate_mod.main(["--db", str(bad)]) == 1


# ------------------------------------------------------------------------------------------ fresh databases and seeds

def test_a_fresh_database_has_the_v2_tables(conn):
    assert schema_version(conn) == SCHEMA_VERSION and {"po_consumption", "invoice_line_matches"} <= tables(conn)


@pytest.mark.parametrize("seed", [None, DEMO])
def test_both_seeds_are_fully_allocated(tmp_path, seed):
    db = tmp_path / "s.db"
    reset_database(db, seed)
    with closing(connect(db)) as c:
        entries = c.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
        rows = c.execute("SELECT matched_by, po_line_id FROM po_consumption").fetchall()
        assert consumption_problems(c) == [] and len(rows) == entries
        assert all(r["matched_by"] == "legacy" and r["po_line_id"] is None for r in rows)


def test_the_demo_seed_history_is_consumption_against_the_po_total(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        r = dict(c.execute("SELECT * FROM po_consumption").fetchone())
        po5 = c.execute("SELECT id FROM purchase_orders WHERE po_number = 'PO-SS-005'").fetchone()[0]
        assert (r["po_id"], r["amount"], r["po_line_id"], r["run_id"], r["matched_by"]) == (po5, 150000, None, None, "legacy")
        assert get_po_balance_minor(c, po5) == 750000                        # unchanged: 9,000.00 - 1,500.00


def test_reset_clears_the_new_tables(tmp_path):
    db = tmp_path / "r.db"
    reset_database(db, DEMO)
    with closing(connect(db)) as c:
        with c:
            c.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (1, 1, 100, 'commit')")
            c.execute("INSERT INTO po_consumption (ledger_entry_id, po_id, invoice_id, amount, type, matched_by) "
                      "VALUES (last_insert_rowid(), 1, 1, 100, 'commit', 'auto')")
    reset_database(db, DEMO)
    with closing(connect(db)) as c:
        assert c.execute("SELECT COUNT(*) FROM po_consumption").fetchone()[0] == 1 and consumption_problems(c) == []


# ------------------------------------------------------------------------------------------ constraints

@pytest.mark.parametrize("po_line, quantity, amount, type_, matched_by", [
    (None, "3", 100, "commit", "auto"),               # a quantity needs a line
    (1, "3", -100, "commit", "auto"),                 # a commit is positive
    (1, "3", 100, "reversal", "auto"),                # a reversal is negative
    (1, "3", 100, "commit", "robot"),                 # unknown source
])
def test_consumption_constraints(tmp_path, po_line, quantity, amount, type_, matched_by):
    with closing(demo_db(tmp_path)) as c:
        with pytest.raises(sqlite3.IntegrityError):
            with c:
                c.execute("INSERT INTO po_consumption (ledger_entry_id, po_id, po_line_id, invoice_id, quantity, amount, type, matched_by) "
                          "VALUES (1, 5, ?, 1, ?, ?, ?, ?)", (po_line, quantity, amount, type_, matched_by))


def test_consumption_problems_are_detected(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        with c:
            c.execute("UPDATE po_consumption SET amount = 1")
            c.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (5, 1, 5, 'commit')")
        problems = consumption_problems(c)
    assert any("consumption 1 != ledger amount 150000" in p for p in problems) and any("has no consumption rows" in p for p in problems)


# ------------------------------------------------------------------------------------------ the approve path keeps it complete

def test_an_approve_writes_one_total_only_auto_consumption_row(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        r = run_real(c, tmp_path, "superstore_24429", controlled_variant_reply("superstore_24429", "PO-SS-002"))   # SYNTHETIC variant
        row = dict(c.execute("SELECT * FROM po_consumption WHERE run_id = ?", (r.run_id,)).fetchone())
        entry = dict(c.execute("SELECT * FROM ledger_entries WHERE id = ?", (row["ledger_entry_id"],)).fetchone())
        assert (row["amount"], row["matched_by"], row["po_line_id"], row["invoice_line_id"], row["quantity"]) == (177061, "auto", None, None, None)
        assert entry["amount"] == 177061 and consumption_problems(c) == []


def test_a_review_writes_no_consumption(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        r = run_real(c, tmp_path, "superstore_10963")
        assert c.execute("SELECT COUNT(*) FROM po_consumption WHERE run_id = ?", (r.run_id,)).fetchone()[0] == 0


# ------------------------------------------------------------------------------------------ programs refuse a v1 database

def test_init_and_check_refuse_v1_with_the_command_to_run(tmp_path):
    db = make_v1(tmp_path / "old.db")
    with closing(connect(db)) as c:
        with pytest.raises(SchemaOutdated, match="app.db.migrate"):
            init_db(c)
        with pytest.raises(SchemaOutdated, match="python -m app.db.migrate --db"):
            check_schema(c, db)


def test_serve_refuses_a_v1_database(tmp_path, capsys, monkeypatch):
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("served a v1 database"))
    db = make_v1(tmp_path / "old.db")
    assert serve.main(["--offline", "--db", str(db)]) == serve.EXIT_USAGE
    assert "python -m app.db.migrate" in capsys.readouterr().out


def test_the_pipeline_cli_refuses_a_v1_database(tmp_path, capsys):
    db = make_v1(tmp_path / "old.db")
    rec = tmp_path / "rec"
    rec.mkdir()
    from tests.extraction.real import real_pdf
    assert pipeline_cli.main([str(real_pdf("superstore_10963")), "--replay", str(rec), "--db", str(db)]) == pipeline_cli.EXIT_USAGE
    assert "python -m app.db.migrate" in capsys.readouterr().out
    with closing(connect(db)) as c:
        assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1          # nothing was run


def test_migration_and_programs_add_the_new_builtin_rule_without_touching_existing_rules(tmp_path, capsys, monkeypatch):
    from app.db.init_db import add_missing_builtin_rules
    db = make_v1(tmp_path / "app.db")
    message = migrate_mod.migrate(db)
    assert "r_po_line_price" in message                    # the v1 fixture has no rules at all: every builtin one is added
    with closing(connect(db)) as c:
        with c:
            c.execute("UPDATE rules SET params = '{\"pct\": 5.0}' WHERE id = 'r_tolerance_pct'")
            c.execute("DELETE FROM rules WHERE id = 'r_po_line_price'")
        assert add_missing_builtin_rules(c) == ["r_po_line_price"]
        assert c.execute("SELECT params FROM rules WHERE id = 'r_tolerance_pct'").fetchone()[0] == '{"pct": 5.0}'
        assert add_missing_builtin_rules(c) == []
