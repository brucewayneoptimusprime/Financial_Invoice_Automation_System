import sqlite3
from pathlib import Path

from app.config import get_settings


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced and dict-like rows."""
    path = Path(db_path) if db_path is not None else get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
