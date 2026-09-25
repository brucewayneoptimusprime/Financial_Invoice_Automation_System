"""The demo dataset (data/seed_demo.json) beside the untouched M0 placeholder seed."""
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.db.queries import get_po_balance
from app.db.reset import reset_database
from app.db.seed import SeedFile, load_seed, parse_seed
from app.extraction.manifest import load_manifest

D = Decimal


@pytest.fixture
def demo_path():
    return get_settings().demo_seed_path


@pytest.fixture
def demo_conn(conn, demo_path):
    load_seed(conn, demo_path)
    return conn


def test_the_two_seeds_are_distinct_files_with_distinct_markers(demo_path, seed_path):
    assert demo_path != seed_path and demo_path.name == "seed_demo.json" and seed_path.name == "seed.json"
    demo = json.loads(demo_path.read_text(encoding="utf-8"))
    placeholder = json.loads(seed_path.read_text(encoding="utf-8"))
    assert "_DATASET" in demo and "_PLACEHOLDER" not in demo
    assert "_PLACEHOLDER" in placeholder and "_DATASET" not in placeholder
    assert parse_seed(demo_path).kind == "demo" and parse_seed(seed_path).kind == "placeholder"


def test_the_placeholder_seed_is_untouched_and_still_loads(conn, seed_path):
    seed = load_seed(conn, seed_path)
    assert {v.name for v in seed.vendors} == {"Placeholder Supplies Ltd", "Placeholder Widgets Inc", "Placeholder Blocked Co"}


def test_a_seed_needs_exactly_one_notice():
    base = {"vendors": []}
    with pytest.raises(ValidationError):
        SeedFile.model_validate(base)
    with pytest.raises(ValidationError):
        SeedFile.model_validate({**base, "_PLACEHOLDER": "x", "_DATASET": "y"})
    with pytest.raises(ValidationError):
        SeedFile.model_validate({**base, "_DATASET": ""})
    assert SeedFile.model_validate({**base, "_DATASET": "demo"}).kind == "demo"


def test_the_demo_seed_loads_and_declares_itself_a_demo_dataset(demo_conn, demo_path):
    seed = parse_seed(demo_path)
    assert "DEMO DATASET" in seed.notice and "NOT the M0 placeholder" in seed.notice
    count = lambda t: demo_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    assert count("vendors") == 2 and count("purchase_orders") == 6 and count("po_lines") == 6
    assert count("invoices") == 1 and count("ledger_entries") == 1


def test_vendors_the_iq_gstin_and_no_placeholder_names(demo_conn):
    rows = {r["name"]: r for r in demo_conn.execute("SELECT * FROM vendors")}
    assert set(rows) == {"SuperStore", "Electronics Mart India Limited"}
    iq = rows["Electronics Mart India Limited"]
    assert iq["tax_id"] == "36AAFCE1683D1ZT" and iq["country"] == "IN" and iq["status"] == "approved"
    assert "IQ" in json.loads(iq["aliases"])
    assert not any("Placeholder" in n for n in rows)


def test_balances_are_derived_from_the_ledger_with_one_po_partly_consumed(demo_conn):
    balances = {r["po_number"]: get_po_balance(demo_conn, r["id"]) for r in demo_conn.execute("SELECT * FROM purchase_orders")}
    assert balances == {"PO-SS-001": D("6000.00"), "PO-SS-002": D("2500.00"), "PO-SS-003": D("12000.00"), "PO-SS-004": D("10000.00"),
                        "PO-SS-005": D("7500.00"), "PO-IQ-2025-001": D("5000.00")}
    status = dict(demo_conn.execute("SELECT po_number, status FROM purchase_orders").fetchall())
    assert status["PO-SS-005"] == "partially_billed" and status["PO-SS-001"] == "open"
    assert "balance" not in [c[1] for c in demo_conn.execute("PRAGMA table_info(purchase_orders)")]


def test_every_po_line_carries_the_real_invoice_line_text_not_a_shortened_one(demo_conn):
    """Owner rule: PO descriptions must resemble the real invoice line text (a shortened line scored 0 overlap on the IQ scan)."""
    manifest = load_manifest(get_settings().seed_path.parent / "manifest.md")
    verified = {n.split("_")[-1].removesuffix(".pdf"): e for n, e in manifest.verified.items() if n.endswith(".pdf")}
    lines = {r["po_number"]: r for r in demo_conn.execute(
        "SELECT po.po_number, po.meta, l.description, l.quantity, l.unit_price FROM purchase_orders po JOIN po_lines l ON l.po_id = po.id")}
    checked = 0
    for po_number, row in lines.items():
        invoice = json.loads(row["meta"])["built_around_invoice"]
        if invoice in verified:
            printed = verified[invoice].expected["line_items"][0]
            assert row["description"] == printed["description"] and D(row["quantity"]) == D(printed["quantity"])
            assert D(row["unit_price"]) == D(printed["unit_price"])
            checked += 1
    assert checked == 5
    assert lines["PO-IQ-2025-001"]["description"].startswith("APPLE IP 16 PRO MAX SL CS MGS PLM MYYW3Z")     # the scan's full product text


def test_pos_have_room_for_their_invoices_and_all_amounts_are_exact_cents(demo_conn):
    assert demo_conn.execute("SELECT typeof(total_amount) FROM purchase_orders LIMIT 1").fetchone()[0] == "integer"
    rows = demo_conn.execute("SELECT po_number, total_amount FROM purchase_orders ORDER BY id").fetchall()
    assert [r["total_amount"] for r in rows] == [600000, 250000, 1200000, 1000000, 900000, 500000]


def test_reset_can_load_either_seed(db_path, demo_path, seed_path):
    from app.db.connection import connect

    reset_database(db_path, demo_path)
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM vendors WHERE name = 'SuperStore'").fetchone()[0] == 1
    c.close()
    reset_database(db_path, seed_path)                                            # back to the placeholder: nothing of the demo remains
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM vendors WHERE name = 'SuperStore'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM vendors WHERE name LIKE 'Placeholder%'").fetchone()[0] == 3
    c.close()


def test_the_reset_command_has_a_demo_flag(monkeypatch, db_path, demo_path):
    from app.db import reset

    monkeypatch.setattr("sys.argv", ["reset", "--db", str(db_path), "--demo"])
    reset.main()
    from app.db.connection import connect
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 6
    c.close()
    monkeypatch.setattr("sys.argv", ["reset", "--db", str(db_path), "--demo", "--seed", str(demo_path)])
    with pytest.raises(SystemExit):
        reset.main()
