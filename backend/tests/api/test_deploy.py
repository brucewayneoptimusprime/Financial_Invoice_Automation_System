"""Deployment configuration (deployment stage 1): DATA_DIR, CORS from the environment, SERVE_MODE, /health, the access token,
and that the server never creates or resets a database by itself. Everything defaults to the local behaviour when unset."""
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.api import serve
from app.config import ROOT_DIR, Settings, get_settings
from tests.api.helpers import SS_10963, api, api_settings, run_and_wait, sse_events

TOKEN = "s3cret-token-for-tests"


# ------------------------------------------------------------------------------------------ DATA_DIR

def test_data_dir_moves_every_writable_path_and_explicit_paths_win(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path)
    assert (s.db_path, s.runs_dir, s.api_upload_dir, s.po_drafts_dir) == (tmp_path / "app.db", tmp_path / "runs", tmp_path / "uploads",
                                                                          tmp_path / "po_drafts")
    s = Settings(_env_file=None, data_dir=tmp_path, db_path=tmp_path / "other.db")
    assert s.db_path == tmp_path / "other.db" and s.runs_dir == tmp_path / "runs"


def test_data_dir_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "elsewhere"))
    s = Settings(_env_file=None)
    assert s.db_path == tmp_path / "app.db" and s.runs_dir == tmp_path / "elsewhere"


def test_without_data_dir_the_local_paths_are_unchanged():
    s = Settings(_env_file=None)
    assert (s.db_path, s.runs_dir, s.api_upload_dir, s.po_drafts_dir) == (ROOT_DIR / "data" / "app.db", ROOT_DIR / "data" / "runs",
                                                                          ROOT_DIR / "data" / "uploads", ROOT_DIR / "data" / "po_drafts")


# ------------------------------------------------------------------------------------------ CORS

@pytest.mark.parametrize("value, expected", [
    ("https://a.vercel.app, https://b.example/", ["https://a.vercel.app", "https://b.example"]),
    ('["https://j.example"]', ["https://j.example"]),
])
def test_cors_origins_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv("API_CORS_ORIGINS", value)
    assert Settings(_env_file=None).api_cors_origins == expected


def test_cors_default_is_the_local_vite_dev_server():
    assert Settings(_env_file=None).api_cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_cors_regex_admits_preview_urls_and_nothing_else(tmp_path):
    settings = api_settings(tmp_path, api_cors_origins=["https://invoice-agent.vercel.app"],
                            api_cors_origin_regex=r"https://invoice-agent-[a-z0-9-]+\.vercel\.app")
    with api(tmp_path, settings=settings) as c:
        exact = c.get("/api/health", headers={"Origin": "https://invoice-agent.vercel.app"})
        preview = c.get("/api/health", headers={"Origin": "https://invoice-agent-git-main-me.vercel.app"})
        other = c.get("/api/health", headers={"Origin": "https://evil.vercel.app"})
    assert exact.headers.get("access-control-allow-origin") == "https://invoice-agent.vercel.app"
    assert preview.headers.get("access-control-allow-origin") == "https://invoice-agent-git-main-me.vercel.app"
    assert "access-control-allow-origin" not in other.headers


# ------------------------------------------------------------------------------------------ SERVE_MODE

@pytest.fixture
def captured(monkeypatch):
    got = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: got.update(app=app, host=host, port=port))
    return got


def fresh_settings():
    get_settings.cache_clear()


def test_serve_mode_starts_the_server_in_that_mode(tmp_path, monkeypatch, captured):
    monkeypatch.setenv("SERVE_MODE", "offline")
    fresh_settings()
    assert serve.main(["--db", str(tmp_path / "app.db"), "--reset-demo", "--host", "0.0.0.0", "--port", "10000"]) == 0
    assert captured["app"].state.api.mode == "offline" and (captured["host"], captured["port"]) == ("0.0.0.0", 10000)


def test_a_flag_wins_over_serve_mode(tmp_path, monkeypatch, captured):
    monkeypatch.setenv("SERVE_MODE", "live")                             # no key: live would stop; the flag keeps it offline
    fresh_settings()
    assert serve.main(["--offline", "--db", str(tmp_path / "app.db"), "--reset-demo"]) == 0
    assert captured["app"].state.api.mode == "offline"


def test_neither_flag_nor_serve_mode_still_refuses(tmp_path, monkeypatch, captured):
    monkeypatch.delenv("SERVE_MODE", raising=False)
    fresh_settings()
    assert serve.main(["--db", str(tmp_path / "app.db")]) == serve.EXIT_LIVE_REFUSED and captured == {}


def test_serve_mode_replay_needs_replay_dir(tmp_path, monkeypatch, captured):
    monkeypatch.setenv("SERVE_MODE", "replay")
    fresh_settings()
    assert serve.main(["--db", str(tmp_path / "app.db"), "--reset-demo"]) == serve.EXIT_USAGE and captured == {}
    rec = tmp_path / "rec"
    rec.mkdir()
    monkeypatch.setenv("REPLAY_DIR", str(rec))
    fresh_settings()
    assert serve.main(["--db", str(tmp_path / "app.db"), "--reset-demo"]) == 0 and captured["app"].state.api.mode == "replay"


def test_serve_live_without_a_key_stops(tmp_path, monkeypatch, captured):
    monkeypatch.setenv("SERVE_MODE", "live")
    fresh_settings()
    assert serve.main(["--db", str(tmp_path / "app.db"), "--reset-demo"]) == serve.EXIT_NOT_CONFIGURED and captured == {}


def test_serve_never_creates_or_resets_a_database_by_itself(tmp_path, monkeypatch, captured, capsys):
    monkeypatch.setenv("SERVE_MODE", "offline")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "disk"))
    fresh_settings()
    assert serve.main([]) == serve.EXIT_USAGE and captured == {}
    assert not (tmp_path / "disk" / "app.db").exists() and "does not exist" in capsys.readouterr().out
    # an existing database is opened as it is: its rows survive a restart
    (tmp_path / "disk").mkdir()
    from app.db.reset import reset_database
    reset_database(tmp_path / "disk" / "app.db", get_settings().demo_seed_path)
    with closing(sqlite3.connect(tmp_path / "disk" / "app.db")) as c:
        c.execute("INSERT INTO vendors (name, status) VALUES ('Kept Across Restarts', 'new')")
        c.commit()
    assert serve.main([]) == 0
    with closing(sqlite3.connect(tmp_path / "disk" / "app.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM vendors WHERE name = 'Kept Across Restarts'").fetchone()[0] == 1


# ------------------------------------------------------------------------------------------ /health

def test_health_is_ok_with_a_good_database_and_says_nothing_sensitive(tmp_path):
    settings = api_settings(tmp_path, anthropic_api_key="sk-ant-api03-CANARY-0123456789")
    with api(tmp_path, settings=settings) as c:
        r = c.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "db": "ok", "schema_version": 4, "mode": "offline"}
    assert "CANARY" not in r.text and str(tmp_path) not in r.text and "\\" not in r.text


def test_health_is_503_for_a_missing_or_old_database(tmp_path):
    from tests.api.helpers import build_app
    app, worker, db, settings = build_app(tmp_path)
    with TestClient(app) as c:
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        old = c.get("/health")
        db.unlink()
        missing = c.get("/health")
    assert old.status_code == 503 and "schema version 1" in old.json()["reason"]
    assert missing.status_code == 503 and missing.json()["reason"] == "database missing" and not db.exists()   # never recreated


# ------------------------------------------------------------------------------------------ the access token

def with_token(tmp_path):
    return api(tmp_path, settings=api_settings(tmp_path, access_token=TOKEN, api_cors_origins=["https://app.example"]))


def test_with_a_token_every_api_call_needs_it(tmp_path):
    with with_token(tmp_path) as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
        assert c.get(f"/api/health?access_token={TOKEN}").status_code == 200
        assert c.get("/api/openapi.json").status_code == 401
        assert c.post("/api/runs", files={"file": ("x.pdf", b"%PDF-1.4")}).status_code == 401
        assert c.get("/health").status_code == 200                               # the host's health check needs no token


def test_a_401_carries_cors_headers_and_preflights_pass(tmp_path):
    with with_token(tmp_path) as c:
        r = c.get("/api/dashboard", headers={"Origin": "https://app.example"})
        pre = c.options("/api/dashboard", headers={"Origin": "https://app.example", "Access-Control-Request-Method": "GET",
                                                    "Access-Control-Request-Headers": "authorization"})
    assert r.status_code == 401 and r.headers.get("access-control-allow-origin") == "https://app.example"
    assert pre.status_code == 200 and "authorization" in pre.headers.get("access-control-allow-headers", "").lower()


def test_the_event_stream_and_page_images_work_with_the_query_token(tmp_path):
    with with_token(tmp_path) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        rid = run_and_wait(c, SS_10963)
        del c.headers["Authorization"]
        stream = c.get(f"/api/runs/{rid}/events?access_token={TOKEN}")
        page = c.get(f"/api/runs/{rid}/pages/1?access_token={TOKEN}")
        denied = c.get(f"/api/runs/{rid}/events")
    assert stream.status_code == 200 and sse_events(stream.text)[-1]["event"] == "end"
    assert page.status_code == 200 and page.content[:4] == b"\x89PNG" and denied.status_code == 401


def test_without_a_token_nothing_changes(tmp_path):
    with api(tmp_path) as c:
        assert c.get("/api/health").status_code == 200 and c.get("/api/dashboard").status_code == 200


def test_a_blank_token_counts_as_unset():
    assert Settings(_env_file=None, access_token="   ").access_token_value() is None


def test_render_free_tier_flow_build_seeds_then_start_opens_it_and_a_fresh_filesystem_refuses(tmp_path, monkeypatch, captured, capsys):
    """render.yaml on the free tier: the BUILD runs `python -m app.db.reset --demo` with DATA_DIR=data/render (relative to the
    repository root); the START runs serve, which opens that database. A filesystem without it (the build step not run) refuses:
    serve never creates one by itself."""
    from app.db import reset as reset_cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", "data/render")
    monkeypatch.setenv("SERVE_MODE", "offline")
    fresh_settings()
    assert serve.main(["--host", "0.0.0.0", "--port", "10000"]) == serve.EXIT_USAGE and captured == {}
    assert not (tmp_path / "data" / "render" / "app.db").exists() and "does not exist" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["reset", "--demo"])
    fresh_settings()
    reset_cli.main()                                                      # the build step
    assert (tmp_path / "data" / "render" / "app.db").is_file()
    fresh_settings()
    assert serve.main(["--host", "0.0.0.0", "--port", "10000"]) == 0      # the start step
    state = captured["app"].state.api
    assert Path(state.db_path) == Path("data/render/app.db") and Path(state.settings.runs_dir) == Path("data/render/runs")
