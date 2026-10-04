"""Gmail import, stage 1: schema v3 and its migration, the token cipher, the key generator, the Gmail settings, the callback-address
check, and the one-scope rule. No Gmail, no OAuth and no model call here."""
import re
import socket
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import serve
from app.config import ROOT_DIR, Settings
from app.db import migrate as migrate_mod
from app.db.connection import connect
from app.db.init_db import SCHEMA_VERSION, SchemaOutdated, check_schema, schema_version
from app.db.queries import get_po_balance_minor
from app.db.reset import reset_database
from app.gmail import keygen
from app.gmail.callback import callback_problems
from app.gmail.crypto import KeyInvalid, KeyMismatch, KeyMissing, TokenCipher, TokenUnreadable, fingerprint
from app.gmail.scopes import GMAIL_READONLY_SCOPE, GMAIL_SCOPES
from tests.pipeline.helpers import DEMO
from tests.test_schema_v2 import make_v1, tables

GMAIL_TABLES = {"oauth_credentials", "gmail_imports"}
V2_TABLES = ["vendors", "purchase_orders", "po_lines", "runs", "invoices", "ledger_entries", "po_consumption"]


def make_v2(path: Path) -> Path:
    """A database exactly as the previous software (schema v2) left it: the v1 fixture migrated by the 1 -> 2 step only."""
    make_v1(path)
    with closing(connect(path)) as c:
        migrate_mod.migrate_1_to_2(c)
    return path


def counts(db: Path) -> dict[str, int]:
    with closing(connect(db)) as c:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in V2_TABLES}


# ------------------------------------------------------------------------------------------ migration 2 -> 3

def test_a_v2_database_migrates_to_v3_with_a_byte_identical_backup_and_nothing_else_changed(tmp_path):
    db = make_v2(tmp_path / "app.db")
    before_bytes, before_counts = db.read_bytes(), counts(db)
    with closing(connect(db)) as c:
        balances = {po: get_po_balance_minor(c, po) for po in (1, 2)}
    message = migrate_mod.migrate(db)
    backups = list(tmp_path.glob("app.db.v2-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == before_bytes
    assert f"from schema version 2 to {SCHEMA_VERSION}" in message and "Gmail import tables created" in message
    with closing(connect(db)) as c:
        assert schema_version(c) == SCHEMA_VERSION and GMAIL_TABLES <= tables(c)
        assert c.execute("SELECT COUNT(*) FROM oauth_credentials").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM gmail_imports").fetchone()[0] == 0
        assert {po: get_po_balance_minor(c, po) for po in (1, 2)} == balances
    assert counts(db) == before_counts


def test_a_v1_database_goes_to_v3_in_one_command_with_one_backup_per_step(tmp_path):
    db = make_v1(tmp_path / "app.db")
    v1_bytes = db.read_bytes()
    message = migrate_mod.migrate(db)
    v1_backups, v2_backups = list(tmp_path.glob("app.db.v1-*.bak")), list(tmp_path.glob("app.db.v2-*.bak"))
    assert len(v1_backups) == 1 and len(v2_backups) == 1 and v1_backups[0].read_bytes() == v1_bytes
    assert f"from schema version 1 to {SCHEMA_VERSION}" in message and "3 existing ledger entries" in message and "Gmail import" in message
    with closing(connect(v2_backups[0])) as c:
        assert schema_version(c) == 2 and not GMAIL_TABLES & tables(c)       # the v2 backup is the state between the two steps
    with closing(connect(db)) as c:
        assert schema_version(c) == SCHEMA_VERSION and GMAIL_TABLES <= tables(c)


def test_a_failure_in_the_2_to_3_step_rolls_that_step_back(tmp_path, monkeypatch, capsys):
    db = make_v2(tmp_path / "app.db")
    before = db.read_bytes()
    real = migrate_mod.schema_statements
    monkeypatch.setattr(migrate_mod, "schema_statements", lambda sql: [*real(sql), "INSERT INTO no_such_table VALUES (1)"])
    assert migrate_mod.main(["--db", str(db)]) == 1
    assert "schema version 2" in capsys.readouterr().out
    with closing(connect(db)) as c:
        assert schema_version(c) == 2 and not GMAIL_TABLES & tables(c)
    assert list(tmp_path.glob("app.db.v2-*.bak"))[0].read_bytes() == before


def test_a_v3_database_is_left_alone(tmp_path):
    db = make_v2(tmp_path / "app.db")
    migrate_mod.migrate(db)
    assert "nothing to do" in migrate_mod.migrate(db)
    assert len(list(tmp_path.glob("*.bak"))) == 2                          # one backup per step: v2 -> 3 and v3 -> 4 (schema v4)


def test_migrate_2_to_3_refuses_any_other_version(tmp_path):
    db = make_v1(tmp_path / "app.db")
    with closing(connect(db)) as c:
        with pytest.raises(migrate_mod.MigrationFailed, match="expected schema version 2"):
            migrate_mod.migrate_2_to_3(c)


# ------------------------------------------------------------------------------------------ fresh database, constraints, reset

def test_a_fresh_database_is_current_with_both_gmail_tables(conn):
    assert schema_version(conn) == SCHEMA_VERSION and GMAIL_TABLES <= tables(conn)


def _credential(c, provider="google", email="a@example.com"):
    c.execute("INSERT INTO oauth_credentials (provider, account_email, scopes, refresh_token_enc, key_fingerprint) VALUES (?, ?, ?, ?, ?)",
              (provider, email, GMAIL_READONLY_SCOPE, b"ciphertext", "0" * 16))


def _import(c, sha="ab" * 32, size=10, message="m1"):
    c.execute("INSERT INTO gmail_imports (account_email, message_id, attachment_sha256, part_id, mime_type, size_bytes, run_id) "
              "VALUES ('a@example.com', ?, ?, '1', 'application/pdf', ?, 'r1')", (message, sha, size))


def test_gmail_table_constraints(conn):
    with conn:
        _credential(conn)
        _import(conn)
    for bad in (lambda: _credential(conn, provider="microsoft"),     # only google
                lambda: _credential(conn),                            # one row per (provider, account)
                lambda: _import(conn),                                # one row per (account, message, file hash)
                lambda: _import(conn, sha="cd" * 32, size=0)):        # a real file has bytes
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                bad()
    with conn:
        _import(conn, message="m2")                                   # the same file from another message is a different row


def test_reset_deletes_stored_gmail_credentials_and_import_history(tmp_path):
    db = tmp_path / "r.db"
    reset_database(db, DEMO)
    with closing(connect(db)) as c:
        with c:
            _credential(c)
            _import(c)
    reset_database(db, DEMO)
    with closing(connect(db)) as c:
        assert [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(GMAIL_TABLES)] == [0, 0]


# ------------------------------------------------------------------------------------------ programs refuse a v2 database

def test_check_schema_refuses_v2_with_the_command_to_run(tmp_path):
    db = make_v2(tmp_path / "old.db")
    with closing(connect(db)) as c:
        with pytest.raises(SchemaOutdated, match=r"schema version 2.*Gmail import.*python -m app\.db\.migrate --db"):
            check_schema(c, db)


def test_serve_refuses_a_v2_database(tmp_path, capsys, monkeypatch):
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("served a v2 database"))
    db = make_v2(tmp_path / "old.db")
    assert serve.main(["--offline", "--db", str(db)]) == serve.EXIT_USAGE
    assert "python -m app.db.migrate" in capsys.readouterr().out


def test_health_is_503_for_a_v2_database(tmp_path):
    from tests.api.helpers import build_app
    app, worker, db, settings = build_app(tmp_path)
    with TestClient(app) as c:
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        r = c.get("/health")
    assert r.status_code == 503 and r.json()["reason"] == f"schema version 2, expected {SCHEMA_VERSION}"


# ------------------------------------------------------------------------------------------ the token cipher

def test_the_cipher_round_trips_and_the_ciphertext_hides_the_token():
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    token = "1//0refresh-token-CANARY"
    enc = cipher.encrypt(token)
    assert b"CANARY" not in enc and cipher.decrypt(enc, cipher.fingerprint) == token
    assert re.fullmatch(r"[0-9a-f]{16}", cipher.fingerprint) and cipher.fingerprint == fingerprint(key) and cipher.fingerprint not in key


def test_another_key_is_a_mismatch_and_a_tampered_token_is_unreadable():
    a, b = TokenCipher(Fernet.generate_key().decode()), TokenCipher(Fernet.generate_key().decode())
    enc = a.encrypt("secret")
    with pytest.raises(KeyMismatch, match="connect Gmail again"):
        b.decrypt(enc, a.fingerprint)
    with pytest.raises(TokenUnreadable):
        b.decrypt(enc, b.fingerprint)                         # right fingerprint claimed, wrong key: still refused
    with pytest.raises(TokenUnreadable):
        a.decrypt(enc[:-4] + b"AAAA", a.fingerprint)


def test_a_missing_or_invalid_key_is_reported_without_echoing_it():
    with pytest.raises(KeyMissing, match="app.gmail.keygen"):
        TokenCipher.from_settings(Settings(oauth_encryption_key=""))
    with pytest.raises(KeyInvalid) as err:
        TokenCipher.from_settings(Settings(oauth_encryption_key="not-a-key-CANARY"))
    assert "CANARY" not in err.value.message and "CANARY" not in str(err.value)


# ------------------------------------------------------------------------------------------ the key generator

def test_append_env_writes_one_valid_key_once_and_never_prints_it(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=x\nGOOGLE_CLIENT_ID=y", encoding="utf-8")         # no trailing newline
    assert keygen.main(["--append-env", "--env-file", str(env)]) == 0
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["ANTHROPIC_API_KEY=x", "GOOGLE_CLIENT_ID=y"] and lines[2].startswith("OAUTH_ENCRYPTION_KEY=")
    key = lines[2].split("=", 1)[1]
    TokenCipher(key)                                                                   # a valid Fernet key
    out = capsys.readouterr().out
    assert "written" in out and key not in out
    after = env.read_bytes()
    assert keygen.main(["--append-env", "--env-file", str(env)]) == 1 and env.read_bytes() == after
    assert key not in capsys.readouterr().out


def test_a_blank_key_line_does_not_count_as_a_key(tmp_path):
    assert not keygen.has_key("OAUTH_ENCRYPTION_KEY=\n# OAUTH_ENCRYPTION_KEY=abc\nOAUTH_ENCRYPTION_KEY_OLD=x")
    assert keygen.has_key(" OAUTH_ENCRYPTION_KEY = 'abc' ")


def test_print_mode_prints_a_fresh_valid_key(capsys):
    assert keygen.main([]) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert line.startswith("OAUTH_ENCRYPTION_KEY=")
    TokenCipher(line.split("=", 1)[1])


# ------------------------------------------------------------------------------------------ settings: which backend

SECRETS = {"google_client_id": "id-CANARY.apps.googleusercontent.com", "google_client_secret": "GOCSPX-CANARY",
           "oauth_encryption_key": Fernet.generate_key().decode()}


def test_the_backend_is_disabled_without_the_three_secrets_and_names_what_is_missing():
    s = Settings()
    assert s.gmail_backend_effective() == "disabled"
    assert s.gmail_missing() == ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_ENCRYPTION_KEY"]
    partial = Settings(google_client_id="x", google_client_secret="y")
    assert partial.gmail_missing() == ["OAUTH_ENCRYPTION_KEY"] and partial.gmail_backend_effective() == "disabled"


def test_google_by_default_with_all_secrets_fake_only_when_chosen_and_disabled_wins():
    assert Settings(**SECRETS).gmail_backend_effective() == "google"
    assert Settings(gmail_backend="fake").gmail_backend_effective() == "fake"
    assert Settings(gmail_backend="google").gmail_backend_effective() == "disabled"         # chosen, but not configured
    assert Settings(gmail_backend="disabled", **SECRETS).gmail_backend_effective() == "disabled"


def test_a_blank_gmail_backend_variable_means_unset(monkeypatch):
    monkeypatch.setenv("GMAIL_BACKEND", "  ")
    assert Settings().gmail_backend is None


def test_secrets_never_appear_in_the_settings_repr_or_the_serve_banner():
    s = Settings(**SECRETS)
    assert "CANARY" not in repr(s) and SECRETS["oauth_encryption_key"] not in repr(s)
    for backend in ("google", "fake", "disabled"):
        text = "\n".join(serve.gmail_banner(Settings(gmail_backend=backend, **SECRETS), "127.0.0.1", 8000))
        assert "CANARY" not in text and SECRETS["oauth_encryption_key"] not in text
    assert "missing GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OAUTH_ENCRYPTION_KEY" in serve.gmail_banner(Settings(), "127.0.0.1", 8000)[0]
    assert "FAKE INBOX" in serve.gmail_banner(Settings(gmail_backend="fake"), "127.0.0.1", 8000)[0]


# ------------------------------------------------------------------------------------------ the callback reaches this server

DEFAULT_URI = Settings().gmail_redirect_uri


def test_the_default_callback_is_localhost_8000():
    assert DEFAULT_URI == "http://localhost:8000/api/gmail/oauth/callback"


@pytest.mark.parametrize("uri, host, port, resolved, expect", [
    (DEFAULT_URI, "127.0.0.1", 8000, {"127.0.0.1", "::1"}, None),
    (DEFAULT_URI, "0.0.0.0", 8000, set(), None),
    (DEFAULT_URI, "localhost", 8000, set(), None),
    (DEFAULT_URI, "127.0.0.1", 8001, {"127.0.0.1"}, "Start with --port 8000"),
    (DEFAULT_URI, "127.0.0.1", 8000, {"::1"}, "Start with --host ::1"),
    ("https://example.com/api/gmail/oauth/callback", "127.0.0.1", 8000, {"127.0.0.1"}, None),     # deployed, behind the Vercel proxy
    ("https://example.com/callback", "0.0.0.0", 10000, set(), "must end with /api/gmail/oauth/callback"),
    ("http://example.com/api/gmail/oauth/callback", "127.0.0.1", 8000, {"127.0.0.1"}, "neither an http://localhost address"),
    ("http://localhost:8000/callback", "127.0.0.1", 8000, {"127.0.0.1"}, "must end with /api/gmail/oauth/callback"),
])
def test_callback_problems(uri, host, port, resolved, expect):
    problems = callback_problems(uri, host, port, resolve=lambda: resolved)
    assert (problems == []) if expect is None else any(expect in p for p in problems)


def test_on_this_machine_localhost_reaches_a_server_bound_to_127_0_0_1():
    """What the OAuth callback relies on: a browser going to http://localhost:<port> reaches serve's default 127.0.0.1 binding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("localhost", port), timeout=5):
            pass
    assert callback_problems(f"http://localhost:{port}/api/gmail/oauth/callback", "127.0.0.1", port) == []


# ------------------------------------------------------------------------------------------ the one scope, and no key in git

def test_the_only_google_scope_anywhere_in_the_code_is_gmail_readonly():
    assert GMAIL_SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)
    found = set()
    for base in (ROOT_DIR / "backend" / "app", ROOT_DIR / "frontend" / "src"):
        for path in base.rglob("*"):
            if path.suffix in {".py", ".ts", ".tsx", ".json", ".sql"} and "node_modules" not in path.parts:
                found |= set(re.findall(r"googleapis\.com/auth/[A-Za-z0-9._/-]+", path.read_text(encoding="utf-8", errors="ignore")))
    assert found == {"googleapis.com/auth/gmail.readonly"}


def test_migration_backups_are_gitignored():
    assert "data/*.bak" in (ROOT_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()
