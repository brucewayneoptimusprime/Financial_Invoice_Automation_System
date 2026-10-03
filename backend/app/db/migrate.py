"""Migrate a database to the current schema, with a backup first (owner decision 8).

    python -m app.db.migrate [--db PATH]

Each step first copies the database file to <name>.v<N>-<UTC timestamp>.bak (byte-identical), then runs in ONE transaction;
any problem rolls that step back (the file stays at the version it had before the step) and the backup is kept.
A version-1 database goes 1 -> 2 -> 3 -> 4 in one command, with one backup per step.

Version 1 -> 2 (line-item PO consumption): create po_consumption and invoice_line_matches, backfill one total-only
  consumption row (po_line_id NULL, matched_by 'legacy') per existing ledger entry, verify that every entry is fully
  allocated, set user_version = 2. PO balances do not change (they are still total minus the ledger sum).
Version 2 -> 3 (Gmail import): create oauth_credentials and gmail_imports (both empty), set user_version = 3.
Version 3 -> 4 (rules settings): create po_settings, po_rule_switches and settings_events (all empty), set user_version = 4. The
  global defaults stay in rules / settings, untouched, so every decision is the same until someone changes a setting.
A database at the current version is left alone.

Exit codes: 0 migrated or already current, 1 migration failed (rolled back), 2 usage / not a database.
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.db.connection import connect
from app.db.consumption import backfill_consumption, consumption_problems, schema_statements
from app.db.init_db import SCHEMA_V2_PATH, SCHEMA_V3_PATH, SCHEMA_V4_PATH, SCHEMA_VERSION, add_missing_builtin_rules, schema_version

_SQLITE_MAGIC = b"SQLite format 3\x00"


class MigrationFailed(RuntimeError):
    pass


def backup_path(db_path: Path, version: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return db_path.with_name(f"{db_path.name}.v{version}-{stamp}.bak")


def migrate_1_to_2(conn: sqlite3.Connection) -> int:
    """The migration itself, on an open connection, in one transaction. Returns the number of backfilled rows."""
    if conn.in_transaction:
        raise MigrationFailed("a transaction is already open")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if schema_version(conn) != 1:
            raise MigrationFailed(f"expected schema version 1, found {schema_version(conn)}")
        for statement in schema_statements(SCHEMA_V2_PATH.read_text(encoding="utf-8")):
            conn.execute(statement)
        n = backfill_consumption(conn)
        problems = consumption_problems(conn)
        if problems:
            raise MigrationFailed("the backfilled allocation is inconsistent: " + "; ".join(problems[:5]))
        conn.execute("PRAGMA user_version = 2")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return n


def migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """Version 2 -> 3 on an open connection, in one transaction: the two Gmail import tables, empty."""
    if conn.in_transaction:
        raise MigrationFailed("a transaction is already open")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if schema_version(conn) != 2:
            raise MigrationFailed(f"expected schema version 2, found {schema_version(conn)}")
        for statement in schema_statements(SCHEMA_V3_PATH.read_text(encoding="utf-8")):
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 3")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """Version 3 -> 4 on an open connection, in one transaction: the rules-settings tables, empty."""
    if conn.in_transaction:
        raise MigrationFailed("a transaction is already open")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if schema_version(conn) != 3:
            raise MigrationFailed(f"expected schema version 3, found {schema_version(conn)}")
        for statement in schema_statements(SCHEMA_V4_PATH.read_text(encoding="utf-8")):
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 4")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def migrate(db_path: Path) -> str:
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"{db_path} does not exist")
    with db_path.open("rb") as f:
        if f.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
            raise ValueError(f"{db_path} is not a SQLite database")
    conn = connect(db_path)
    try:
        version = start = schema_version(conn)
        if version == SCHEMA_VERSION:
            return f"{db_path} is already at schema version {SCHEMA_VERSION}; nothing to do."
        if version not in (1, 2, 3):
            raise MigrationFailed(f"{db_path} is schema version {version}; only versions 1, 2 and 3 can be migrated")
        backups, notes = [], []
        while version < SCHEMA_VERSION:
            backup = backup_path(db_path, version)
            conn.close()
            shutil.copy2(db_path, backup)                                # the file is closed: a byte-identical copy
            backups.append(str(backup))
            conn = connect(db_path)
            if version == 1:
                n = migrate_1_to_2(conn)
                notes.append(f"{n} existing ledger entr{'y' if n == 1 else 'ies'} recorded as consumption against the PO total "
                             "(matched_by legacy).")
            elif version == 2:
                migrate_2_to_3(conn)
                notes.append("Gmail import tables created (oauth_credentials, gmail_imports; both empty).")
            else:
                migrate_3_to_4(conn)
                notes.append("Rules-settings tables created (po_settings, po_rule_switches, settings_events; all empty; "
                             "the global defaults are unchanged).")
            version = schema_version(conn)
        added = add_missing_builtin_rules(conn)
        return (f"Migrated {db_path} from schema version {start} to {SCHEMA_VERSION}. Backup{'s' if len(backups) > 1 else ''}: "
                f"{', '.join(backups)}. " + " ".join(notes)
                + (f" Added builtin rule(s): {', '.join(added)}." if added else ""))
    finally:
        conn.close()


def _version_now(db_path: Path) -> str:
    try:
        conn = connect(db_path)
        try:
            return str(schema_version(conn))
        finally:
            conn.close()
    except Exception:                                                    # noqa: BLE001 - only used in an error message
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db.migrate", description="Migrate the database to the current schema.")
    parser.add_argument("--db", type=Path, default=None, help="database path (default from config)")
    args = parser.parse_args(argv)
    db_path = args.db or get_settings().db_path
    try:
        print(migrate(db_path))
        return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"BAD USAGE: {exc}")
        return 2
    except (MigrationFailed, sqlite3.Error) as exc:
        print(f"MIGRATION FAILED (the failing step was rolled back; the database is at schema version {_version_now(db_path)}; "
              f"the backups are kept): {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
