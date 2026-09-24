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
