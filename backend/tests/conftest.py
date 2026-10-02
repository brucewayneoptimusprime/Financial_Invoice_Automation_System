import pytest

from app.config import get_settings
from app.db.connection import connect
from app.db.init_db import init_db
from app.db.seed import load_seed


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path):
    """Initialised, EMPTY (no seed) database."""
    c = connect(db_path)
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def seeded_conn(conn):
    load_seed(conn)
    return conn


@pytest.fixture
def seed_path():
    return get_settings().seed_path


# --------------------------------------------------------------------------- LLM isolation (M2)

@pytest.fixture(autouse=True)
def _isolate_llm(request, monkeypatch):
    """No test except a `live` one can ever see a real API key (e.g. one in the developer's .env), so a
    mocked test can never spend money by accident. The same holds for the Google OAuth client and the token encryption key
    (Gmail import). Also resets cached settings and the session cost tracker."""
    from app.llm.budget import reset_session_tracker

    get_settings.cache_clear()
    reset_session_tracker()
    if request.node.get_closest_marker("live") is None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")     # shadows .env; blank means "not set"
        for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_ENCRYPTION_KEY", "GMAIL_BACKEND"):
            monkeypatch.setenv(name, "")                 # Gmail import: never the developer's real OAuth client or key
    yield
    get_settings.cache_clear()
    reset_session_tracker()


def pytest_collection_modifyitems(config, items):
    """Live tests are skipped automatically when no API key is configured (they are also deselected by default)."""
    from app.config import Settings

    if Settings().api_key_value():
        return
    skip = pytest.mark.skip(reason="live test: ANTHROPIC_API_KEY is not set")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
