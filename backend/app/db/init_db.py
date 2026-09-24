"""Create the schema (idempotent) and seed builtin rules and default settings.

Migrations are deliberately simple: PRAGMA user_version tracks SCHEMA_VERSION. A fresh DB gets
schema.sql applied; an up-to-date DB is left alone; any other version is an error until a real
migration exists.
"""
import argparse
import json
import sqlite3
from pathlib import Path

from app.builtin_rules import builtin_rules
from app.config import Settings, get_settings
from app.db.connection import connect

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def init_db(conn: sqlite3.Connection, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version != SCHEMA_VERSION:
        raise RuntimeError(f"Database schema version {version} != expected {SCHEMA_VERSION}; no migration available")
    _seed_defaults(conn, settings)


def _seed_defaults(conn: sqlite3.Connection, settings: Settings) -> None:
    """INSERT OR IGNORE so re-running init never overwrites edited rules or settings."""
    with conn:
        for rule in builtin_rules(settings):
            conn.execute(
                "INSERT OR IGNORE INTO rules "
                "(id, name, type, params, severity_on_trigger, source, enabled, original_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule.id,
                    rule.name,
                    rule.type,
                    json.dumps(rule.params),
                    rule.severity_on_trigger,
                    rule.source.value,
                    int(rule.enabled),
                    rule.original_text,
                ),
            )
        defaults = {"confidence_threshold": settings.confidence_threshold, "model_override": None}
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the database schema (idempotent).")
    parser.add_argument("--db", type=Path, default=None, help="database path (default from config)")
    args = parser.parse_args()
    conn = connect(args.db)
    try:
        init_db(conn)
    finally:
        conn.close()
    print(f"Database ready: {args.db or get_settings().db_path}")


if __name__ == "__main__":
    main()
