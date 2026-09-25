"""One-command reset: return the database to the seed state.

    python -m app.db.reset        (or the `reset-db` console script)

Drops every table in place (so it works even if another process has the file open, which
matters on Windows), then re-creates the schema, builtin rules, default settings and the seed.
"""
import argparse
import logging
import sqlite3
from pathlib import Path

from app.config import get_settings
from app.db.connection import connect
from app.db.init_db import init_db
from app.db.seed import load_seed

_SQLITE_MAGIC = b"SQLite format 3\x00"


def _check_safe_target(db_path: Path) -> None:
    """Refuse to wipe a file that exists, is non-empty and is not a SQLite database."""
    if db_path.exists() and db_path.stat().st_size > 0:
        with db_path.open("rb") as f:
            if f.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
                raise RuntimeError(f"{db_path} exists but is not a SQLite database; refusing to reset it")


def drop_all_tables(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        tables = [
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        ]
        with conn:
            for name in tables:
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.execute("PRAGMA user_version = 0")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def reset_database(db_path: Path | str | None = None, seed_path: Path | str | None = None) -> None:
    settings = get_settings()
    db_path = Path(db_path) if db_path is not None else settings.db_path
    _check_safe_target(db_path)
    conn = connect(db_path)
    try:
        drop_all_tables(conn)
        init_db(conn, settings)
        load_seed(conn, seed_path)
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Reset the database to the seed state.")
    parser.add_argument("--db", type=Path, default=None, help="database path (default from config)")
    parser.add_argument("--seed", type=Path, default=None, help="seed file (default from config: the M0 placeholder seed)")
    parser.add_argument("--demo", action="store_true", help="use the demo dataset (data/seed_demo.json) instead of the placeholder")
    args = parser.parse_args()
    if args.demo and args.seed is not None:
        parser.error("--demo and --seed are mutually exclusive")
    if args.demo:
        args.seed = get_settings().demo_seed_path
    reset_database(args.db, args.seed)
    print(f"Database reset to seed state: {args.db or get_settings().db_path}")


if __name__ == "__main__":
    main()
