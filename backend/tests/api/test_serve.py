"""The server's live-call rule (PLAN M4 section 4): no mode, no server; offline and replay never reach the network; no key leaks."""
import json
import re
from pathlib import Path

import pytest
import uvicorn

from app.api import serve
from app.api.clients import OfflineClient, build_api_client
from app.config import Settings
from app.extraction import eval as eval_mod
from app.llm.budget import CostTracker
from tests.api.helpers import SS_10963, api, api_settings, run_and_wait

CANARY = "sk-ant-api03-CANARY-do-not-leak-0123456789"
API_DIR = Path(__file__).resolve().parents[2] / "app" / "api"


@pytest.fixture
def no_real_client(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a real API client was constructed")
    monkeypatch.setattr(eval_mod, "AnthropicClient", refuse)


@pytest.fixture
def captured(monkeypatch):
    got = {}

    def fake_run(app, host, port, **kw):
        got.update(app=app, host=host, port=port)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    return got


def test_no_mode_refuses_to_start_and_builds_nothing(monkeypatch, capsys, captured, no_real_client):
    monkeypatch.setattr(serve, "build_api_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("client built")))
    assert serve.main([]) == serve.EXIT_LIVE_REFUSED
    assert "REFUSED" in capsys.readouterr().out and captured == {}


@pytest.mark.parametrize("argv", [["--live", "--offline"], ["--replay", ".", "--offline"], ["--offline", "--record", "x"],
                                  ["--replay", "does-not-exist-dir"]])
def test_bad_mode_combinations_are_usage_errors(argv, captured, no_real_client):
    assert serve.main(argv) == serve.EXIT_USAGE and captured == {}


def test_offline_starts_on_localhost_with_an_offline_client(tmp_path, captured, no_real_client):
    db = tmp_path / "app.db"
    assert serve.main(["--offline", "--db", str(db), "--reset-demo"]) == 0
    assert captured["host"] == "127.0.0.1" and captured["port"] == 8000
    state = captured["app"].state.api
    assert state.mode == "offline" and isinstance(state.worker.client.inner, OfflineClient)


def test_replay_starts_with_a_replay_client(tmp_path, captured, no_real_client):
    rec = tmp_path / "rec"
    rec.mkdir()
    assert serve.main(["--replay", str(rec), "--db", str(tmp_path / "app.db"), "--reset-demo"]) == 0
    assert captured["app"].state.api.mode == "replay" and type(captured["app"].state.api.worker.client.inner).__name__ == "ReplayClient"


def test_live_without_a_key_stops_before_serving(tmp_path, captured, capsys):
    assert serve.main(["--live", "--db", str(tmp_path / "app.db"), "--reset-demo"]) == serve.EXIT_NOT_CONFIGURED
    assert "NOT CONFIGURED" in capsys.readouterr().out and captured == {}


def test_a_missing_database_is_a_usage_error(tmp_path, captured):
    assert serve.main(["--offline", "--db", str(tmp_path / "none.db")]) == serve.EXIT_USAGE and captured == {}


def test_the_offline_client_raises_without_any_network():
    from app.llm.errors import LLMConfigError
    with pytest.raises(LLMConfigError, match="Offline mode"):
        OfflineClient().complete(None)


def test_build_api_client_offline_never_touches_the_real_client(tmp_path, no_real_client):
    c = build_api_client("offline", Settings(_env_file=None), CostTracker(1, 1))
    assert isinstance(c.inner, OfflineClient)
    with pytest.raises(ValueError):
        build_api_client("replay", Settings(_env_file=None), CostTracker(1, 1))


def test_the_api_package_builds_clients_only_through_build_client():
    for path in API_DIR.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "AnthropicClient" not in src and "build_llm_client" not in src and "anthropic" not in re.sub(r"#.*", "", src).lower(), path
    uses = [p.name for p in API_DIR.glob("*.py") if re.search(r"\bbuild_client\(", p.read_text(encoding="utf-8"))]
    assert uses == ["clients.py"]


def test_an_offline_run_degrades_to_review_with_a_template_explanation(tmp_path):
    from tests.api.helpers import ScriptedRuns

    class NoScript(ScriptedRuns):                                          # use the app's own OfflineClient for extraction
        def __call__(self, path, conn, **kw):
            from app.pipeline.runner import run_pipeline
            return run_pipeline(path, conn, **kw)

    with api(tmp_path, run_fn=NoScript()) as c:
        v = c.get(f"/api/runs/{run_and_wait(c, SS_10963)}").json()
    assert v["decision"] == "review" and v["explanation"]["source"] == "template"
    extract = next(s for s in v["stages"] if s["stage"] == "extract")
    assert extract["summary"]["degraded"] is True and extract["summary"]["failure_code"] == "config"
    assert v["run"]["cost_usd"] == 0


def test_a_canary_key_never_appears_in_any_response(tmp_path):
    settings = api_settings(tmp_path, anthropic_api_key=CANARY)
    with api(tmp_path, settings=settings) as c:
        rid = run_and_wait(c, SS_10963)
        bodies = [c.get(u).text for u in ("/api/health", "/api/runs", f"/api/runs/{rid}", f"/api/runs/{rid}/events",
                                           "/api/openapi.json")]
    assert all(CANARY not in b and "CANARY" not in b for b in bodies)
