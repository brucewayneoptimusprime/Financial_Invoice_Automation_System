import json

import pytest
from pydantic import ValidationError

from app.db.seed import load_seed, parse_seed


def test_seed_file_is_marked_placeholder(seed_path):
    raw = json.loads(seed_path.read_text(encoding="utf-8"))
    assert "PLACEHOLDER" in raw["_PLACEHOLDER"]


def test_seed_loads_expected_row_counts(seeded_conn, seed_path):
    seed = parse_seed(seed_path)
    count = lambda t: seeded_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    assert count("vendors") == len(seed.vendors) > 0
    assert count("purchase_orders") == len(seed.purchase_orders) > 0
    assert count("po_lines") == sum(len(p.lines) for p in seed.purchase_orders)
    assert count("invoices") == len(seed.invoices) > 0
    assert count("invoice_lines") == sum(len(i.lines) for i in seed.invoices)
    assert count("ledger_entries") == len(seed.ledger_entries) > 0


def test_seed_stores_money_as_integer_minor_units(seeded_conn):
    row = seeded_conn.execute("SELECT total_amount FROM purchase_orders WHERE po_number = 'PH-PO-0002'").fetchone()
    assert row["total_amount"] == 120050
    assert seeded_conn.execute("SELECT typeof(total_amount) FROM purchase_orders LIMIT 1").fetchone()[0] == "integer"
    assert seeded_conn.execute("SELECT DISTINCT typeof(amount) FROM ledger_entries").fetchall()[0][0] == "integer"


def test_seed_keeps_decision_and_effective_status_separate(seeded_conn):
    row = seeded_conn.execute("SELECT decision, status FROM invoices WHERE invoice_number = 'PH-INV-0002'").fetchone()
    assert (row["decision"], row["status"]) == ("approve", "rejected")


def test_seed_leaves_runtime_tables_empty(seeded_conn):
    for t in ("runs", "audit_events", "review_queue", "drafts"):
        assert seeded_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0


def _mutated_seed(tmp_path, seed_path, mutate):
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    mutate(data)
    p = tmp_path / "bad_seed.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_seed_without_placeholder_marker_is_rejected(tmp_path, seed_path):
    p = _mutated_seed(tmp_path, seed_path, lambda d: d.pop("_PLACEHOLDER"))
    with pytest.raises(ValidationError):
        parse_seed(p)


def test_seed_rejects_unknown_keys_and_bad_enums(tmp_path, seed_path):
    with pytest.raises(ValidationError):
        parse_seed(_mutated_seed(tmp_path, seed_path, lambda d: d["vendors"][0].update(typo_field=1)))
    with pytest.raises(ValidationError):
        parse_seed(_mutated_seed(tmp_path, seed_path, lambda d: d["vendors"][0].update(status="vip")))


def test_seed_rejects_sub_cent_amounts(conn, tmp_path, seed_path):
    p = _mutated_seed(tmp_path, seed_path, lambda d: d["purchase_orders"][0].update(total_amount=10.005))
    with pytest.raises(ValueError):
        load_seed(conn, p)
    assert conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 0


def test_failed_seed_load_is_all_or_nothing(conn, tmp_path, seed_path):
    # Ledger entry pointing at a missing invoice -> FK failure after vendors/POs were inserted.
    p = _mutated_seed(tmp_path, seed_path, lambda d: d["ledger_entries"][0].update(invoice_id=999))
    with pytest.raises(Exception):
        load_seed(conn, p)
    assert conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
