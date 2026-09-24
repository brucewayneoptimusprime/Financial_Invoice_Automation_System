"""PO balance = total_amount - SUM(ledger_entries.amount). Derived every time, never stored."""
import sqlite3
from decimal import Decimal

import pytest

from app.db.queries import get_po_balance, get_po_balance_minor
from app.db.seed import parse_seed
from app.money import to_minor


def _make_po(conn, total_minor):
    conn.execute("INSERT INTO vendors (id, name, status) VALUES (1, 'V', 'approved')")
    conn.execute("INSERT INTO purchase_orders (id, po_number, vendor_id, currency, total_amount, status) "
                 "VALUES (1, 'P1', 1, 'USD', ?, 'open')", (total_minor,))
    conn.execute("INSERT INTO invoices (id, status) VALUES (1, 'pending')")


def _ledger(conn, amount, type_):
    conn.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (1, 1, ?, ?)", (amount, type_))


def test_balance_with_no_ledger_entries_is_full_total(conn):
    _make_po(conn, 100000)
    assert get_po_balance_minor(conn, 1) == 100000
    assert get_po_balance(conn, 1) == Decimal("1000.00")


def test_balance_is_total_minus_sum_of_ledger(conn):
    _make_po(conn, 100000)
    _ledger(conn, 30000, "commit")
    _ledger(conn, 20050, "commit")
    assert get_po_balance_minor(conn, 1) == 100000 - 30000 - 20050


def test_reversal_gives_balance_back(conn):
    _make_po(conn, 100000)
    _ledger(conn, 30000, "commit")
    _ledger(conn, -30000, "reversal")
    assert get_po_balance_minor(conn, 1) == 100000


def test_balance_updates_immediately_when_ledger_changes(conn):
    _make_po(conn, 100000)
    assert get_po_balance_minor(conn, 1) == 100000
    _ledger(conn, 1, "commit")
    assert get_po_balance_minor(conn, 1) == 99999


def test_balance_is_exact_where_floats_are_not(conn):
    _make_po(conn, to_minor("1.00"))
    for _ in range(3):
        _ledger(conn, to_minor("0.10"), "commit")
    assert get_po_balance(conn, 1) == Decimal("0.70")  # 0.1+0.1+0.1 is 0.30000000000000004 as a float


def test_overbilled_po_gives_negative_balance(conn):
    _make_po(conn, 1000)
    _ledger(conn, 1500, "commit")
    assert get_po_balance_minor(conn, 1) == -500


def test_unknown_po_raises(conn):
    with pytest.raises(LookupError):
        get_po_balance(conn, 12345)


def test_balance_is_never_stored_in_any_table(conn):
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        cols = [c["name"] for c in conn.execute(f'PRAGMA table_info("{table}")')]
        assert not any("balance" in c.lower() or "remaining" in c.lower() for c in cols), (table, cols)


@pytest.mark.parametrize("type_,amount", [("commit", -100), ("commit", 0), ("reversal", 100), ("reversal", 0)])
def test_ledger_sign_convention_enforced(conn, type_, amount):
    _make_po(conn, 1000)
    with pytest.raises(sqlite3.IntegrityError):
        _ledger(conn, amount, type_)


def test_seeded_balances_match_seed_file(seeded_conn, seed_path):
    """Recompute every PO balance independently from the seed JSON and compare to the DB-derived value."""
    seed = parse_seed(seed_path)
    for po in seed.purchase_orders:
        consumed = sum((e.amount for e in seed.ledger_entries if e.po_id == po.id), Decimal("0"))
        assert get_po_balance(seeded_conn, po.id) == po.total_amount - consumed
    # The placeholder seed should exercise all three states: untouched, partially consumed, fully consumed.
    balances = [(po.total_amount, get_po_balance(seeded_conn, po.id)) for po in seed.purchase_orders]
    assert any(bal == total for total, bal in balances)
    assert any(0 < bal < total for total, bal in balances)
    assert any(bal == 0 for _, bal in balances)
