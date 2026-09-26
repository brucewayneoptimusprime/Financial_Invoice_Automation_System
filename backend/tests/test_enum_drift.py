"""The DB CHECK constraints and the Python enums are two copies of the same vocabularies.
This test fails if they drift apart, or if a new CHECK / enum is added without being classified here."""
import enum
import re

import pytest

from app import enums
from app.enums import (
    Decision, DraftKind, DraftStatus, InvoiceStatus, LedgerType, LineMatchStatus, MatchedBy, Outcome, POStatus,
    QueueStatus, Resolution, RuleSource, RunStatus, VendorStatus,
)

# (table, column) -> enum that the column's CHECK ... IN (...) must match exactly.
CHECK_TO_ENUM = {
    ("vendors", "status"): VendorStatus,
    ("purchase_orders", "status"): POStatus,
    ("runs", "status"): RunStatus,
    ("runs", "final_decision"): Decision,
    ("invoices", "decision"): Decision,
    ("invoices", "status"): InvoiceStatus,
    ("ledger_entries", "type"): LedgerType,
    ("audit_events", "outcome"): Outcome,
    ("rules", "source"): RuleSource,
    ("review_queue", "status"): QueueStatus,
    ("review_queue", "resolution"): Resolution,
    ("drafts", "kind"): DraftKind,
    ("drafts", "status"): DraftStatus,
    ("po_consumption", "type"): LedgerType,
    ("po_consumption", "matched_by"): MatchedBy,
    ("invoice_line_matches", "status"): LineMatchStatus,
}

# Enums that intentionally have no DB column (they only exist on in-memory models).
ENUMS_WITHOUT_DB_COLUMN = {"StageStatus", "MatchStatus", "GroundingStatus"}

_IN_CLAUSE = re.compile(r'"?(\w+)"?\s+IN\s*\(([^)]*)\)', re.IGNORECASE)


def _string_checks(conn):
    """{(table, column): {values}} for every `col IN ('a', 'b')` CHECK of string literals."""
    found = {}
    tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    for table, sql in tables.fetchall():
        for column, body in _IN_CLAUSE.findall(sql):
            values = re.findall(r"'([^']*)'", body)
            if values:  # skips numeric lists such as enabled IN (0, 1)
                found[(table, column)] = set(values)
    return found


def test_every_sql_check_matches_its_python_enum(conn):
    for key, enum_cls in CHECK_TO_ENUM.items():
        sql_values = _string_checks(conn).get(key)
        assert sql_values is not None, f"no CHECK ... IN (...) found for {key}"
        assert sql_values == {m.value for m in enum_cls}, f"{key} drifted from {enum_cls.__name__}"


def test_no_unclassified_sql_checks(conn):
    assert set(_string_checks(conn)) == set(CHECK_TO_ENUM), "new CHECK IN (...) added: map it to an enum here"


def test_no_unclassified_python_enums():
    all_enums = {n for n, o in vars(enums).items() if isinstance(o, type) and issubclass(o, enum.Enum) and o is not enum.Enum
                 and o.__module__ == enums.__name__}
    mapped = {e.__name__ for e in CHECK_TO_ENUM.values()}
    assert all_enums == mapped | ENUMS_WITHOUT_DB_COLUMN, "new enum added: map it to a CHECK or list it as DB-less"


@pytest.mark.parametrize("enum_cls", sorted(set(CHECK_TO_ENUM.values()), key=lambda e: e.__name__))
def test_enum_values_are_unique_lowercase_strings(enum_cls):
    values = [m.value for m in enum_cls]
    assert len(values) == len(set(values))
    assert all(v == v.lower() for v in values)
