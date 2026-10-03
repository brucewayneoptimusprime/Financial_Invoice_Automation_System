"""Create the schema (idempotent) and seed builtin rules and default settings.

PRAGMA user_version tracks SCHEMA_VERSION. A fresh DB gets schema.sql (version 1), schema_v2.sql (line-item PO consumption) and
schema_v3.sql (Gmail import) and schema_v4.sql (rules settings) applied; an up-to-date DB is left alone. An older DB (version 1, 2
or 3) is NOT migrated silently:
`python -m app.db.migrate` does it, with a backup per step (owner decision 8). Any other version is an error.
"""
import argparse
import json
import sqlite3
from pathlib import Path

from app.builtin_rules import builtin_rules
from app.config import Settings, get_settings
from app.db.connection import connect
from app.db.consumption import schema_statements

SCHEMA_VERSION = 4
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_V2_PATH = Path(__file__).with_name("schema_v2.sql")
SCHEMA_V3_PATH = Path(__file__).with_name("schema_v3.sql")
SCHEMA_V4_PATH = Path(__file__).with_name("schema_v4.sql")
MIGRATABLE_VERSIONS = {1: "line-item PO consumption, Gmail import and rules settings", 2: "Gmail import and rules settings",
                       3: "rules settings"}                              # older version -> what it lacks
MIGRATE_HINT = "python -m app.db.migrate"


class SchemaOutdated(RuntimeError):
    """The database is an older schema version that has a migration."""


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def check_schema(conn: sqlite3.Connection, db_path: Path | str | None = None) -> None:
    """For programs that open an existing database (serve, the pipeline CLI): refuse an old or unknown schema, clearly."""
    version = schema_version(conn)
    where = f" ({db_path})" if db_path else ""
    if version == SCHEMA_VERSION:
        return
    if version in MIGRATABLE_VERSIONS:
        raise SchemaOutdated(f"The database{where} is schema version {version}; this version of the software needs version "
                             f"{SCHEMA_VERSION} ({MIGRATABLE_VERSIONS[version]}). Migrate it (a backup is made first):  {MIGRATE_HINT}"
                             + (f" --db {db_path}" if db_path else "") + "   or start fresh with --reset-demo.")
    raise RuntimeError(f"Database schema version {version}{where} is not supported (expected {SCHEMA_VERSION}).")


def add_missing_builtin_rules(conn: sqlite3.Connection, settings: Settings | None = None) -> list[str]:
    """Insert builtin rules the database does not have yet (INSERT OR IGNORE: an existing rule, edited or not, is never touched).
    Returns the ids that were added, so programs can say so."""
    settings = settings or get_settings()
    before = {r[0] for r in conn.execute("SELECT id FROM rules")}
    _seed_defaults(conn, settings)
    return sorted({r[0] for r in conn.execute("SELECT id FROM rules")} - before)


def init_db(conn: sqlite3.Connection, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    version = schema_version(conn)
    if version == 0:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn:
            for path in (SCHEMA_V2_PATH, SCHEMA_V3_PATH, SCHEMA_V4_PATH):
                for statement in schema_statements(path.read_text(encoding="utf-8")):
                    conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version in MIGRATABLE_VERSIONS:
        raise SchemaOutdated(f"Database schema version {version} needs migrating to {SCHEMA_VERSION}: run {MIGRATE_HINT}")
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
