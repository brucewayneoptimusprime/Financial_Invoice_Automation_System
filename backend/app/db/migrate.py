"""Migrate a database to the current schema, with a backup first (owner decision 8).

    python -m app.db.migrate [--db PATH]

Version 1 -> 2 (line-item PO consumption):
  1. copy the database file to <name>.v1-<UTC timestamp>.bak (byte-identical);
  2. in ONE transaction: create po_consumption and invoice_line_matches, backfill one total-only consumption row
     (po_line_id NULL, matched_by 'legacy') per existing ledger entry, verify that every entry is fully allocated,
     set user_version = 2;
  3. any problem rolls the transaction back: the file stays version 1 and the backup is kept.
A version-2 database is left alone. PO balances do not change (they are still total minus the ledger sum).

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
from app.db.init_db import SCHEMA_V2_PATH, SCHEMA_VERSION, schema_version

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


def migrate(db_path: Path) -> str:
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"{db_path} does not exist")
    with db_path.open("rb") as f:
        if f.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
            raise ValueError(f"{db_path} is not a SQLite database")
    conn = connect(db_path)
    try:
        version = schema_version(conn)
        if version == SCHEMA_VERSION:
            return f"{db_path} is already at schema version {SCHEMA_VERSION}; nothing to do."
        if version != 1:
            raise MigrationFailed(f"{db_path} is schema version {version}; only version 1 can be migrated")
        backup = backup_path(db_path, version)
        conn.close()
        shutil.copy2(db_path, backup)                                    # the file is closed: a byte-identical copy
        conn = connect(db_path)
        n = migrate_1_to_2(conn)
        return (f"Migrated {db_path} from schema version 1 to {SCHEMA_VERSION}. Backup: {backup}. "
                f"{n} existing ledger entr{'y' if n == 1 else 'ies'} recorded as consumption against the PO total (matched_by legacy).")
    finally:
        conn.close()


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
        print(f"MIGRATION FAILED (rolled back; the database is unchanged): {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
