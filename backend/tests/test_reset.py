import pytest

from app.db.connection import connect
from app.db.reset import reset_database
from tests.helpers import SEED_TABLES, snapshot


def test_reset_creates_seeded_db_from_nothing(db_path):
    reset_database(db_path)
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] > 0
    assert c.execute("SELECT COUNT(*) FROM rules").fetchone()[0] > 0
    c.close()


def test_reset_returns_database_to_exact_seed_state(db_path):
    reset_database(db_path)
    c = connect(db_path)
    expected = snapshot(c, SEED_TABLES)
    builtin_ids = {r[0] for r in c.execute("SELECT id FROM rules")}

    # Mutate everything a live run could touch.
    with c:
        c.execute("UPDATE vendors SET status = 'blocked' WHERE id = 1")
        c.execute("DELETE FROM po_consumption WHERE ledger_entry_id = 1")      # schema v2: the allocation goes with it
        c.execute("DELETE FROM ledger_entries WHERE id = 1")
        c.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (2, 1, 100, 'commit')")
        c.execute("INSERT INTO runs (id, source_file, status) VALUES ('run-1', 'x.pdf', 'completed')")
        c.execute("INSERT INTO audit_events (run_id, seq, stage, event_type, outcome, message) "
                  "VALUES ('run-1', 0, 'extract', 'start', 'info', 'm')")
        c.execute("INSERT INTO review_queue (run_id, reason) VALUES ('run-1', 'r')")
        c.execute("INSERT INTO drafts (run_id, kind, \"to\") VALUES ('run-1', 'vendor_email', 'a@b.c')")
        c.execute("INSERT INTO rules (id, name, type, severity_on_trigger, source) VALUES ('u1', 'n', 't', 2, 'user')")
        c.execute("UPDATE settings SET value = '0.1' WHERE key = 'confidence_threshold'")
    assert snapshot(c, SEED_TABLES) != expected
    c.close()

    reset_database(db_path)

    c = connect(db_path)
    assert snapshot(c, SEED_TABLES) == expected
    for t in ("runs", "audit_events", "review_queue", "drafts"):
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    assert {r[0] for r in c.execute("SELECT id FROM rules")} == builtin_ids  # user rule gone
    assert c.execute("SELECT value FROM settings WHERE key='confidence_threshold'").fetchone()[0] != "0.1"
    c.close()


def test_reset_works_while_another_connection_is_open(db_path):
    reset_database(db_path)
    holder = connect(db_path)  # simulates a running API process (matters on Windows)
    reset_database(db_path)
    assert holder.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] > 0
    holder.close()


def test_reset_refuses_to_wipe_a_non_sqlite_file(tmp_path):
    victim = tmp_path / "important.txt"
    victim.write_text("not a database, do not delete", encoding="utf-8")
    with pytest.raises(RuntimeError):
        reset_database(victim)
    assert victim.read_text(encoding="utf-8") == "not a database, do not delete"
