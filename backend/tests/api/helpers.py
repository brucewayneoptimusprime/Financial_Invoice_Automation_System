"""Shared setup for API tests: a demo database, the real app factory, and a worker whose runs are scripted per uploaded file name.

No test here can reach the network: the app gets an OfflineClient, and each run's extraction reply comes from a recorded fixture
chosen by the upload's name (or the offline client, for unknown names).
"""
import json
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.clients import OfflineClient
from app.api.main import create_app
from app.api.worker import RunWorker
from app.llm.budget import CostTracker
from app.llm.client import MeteredClient
from app.pipeline.runner import run_pipeline
from tests.extraction.real import real_pdf, real_reply
from tests.pipeline.helpers import DEMO, cfg, scripted
from app.db.reset import reset_database

SS_10963, SS_24429, IQ = "superstore_10963", "superstore_24429", "iq_electronics"


def api_settings(tmp_path, **kw):
    kw.setdefault("api_upload_dir", tmp_path / "uploads")
    kw.setdefault("sse_poll_ms", 20)
    kw.setdefault("sse_heartbeat_s", 0.2)
    return cfg(tmp_path, **kw)


class ScriptedRuns:
    """run_fn for the worker: the extraction reply is picked by the upload's display name (`<fixture>.<ext>`)."""

    def __init__(self, replies: dict[str, dict] | None = None):
        self.replies = replies or {}
        self.calls: list[str] = []

    def __call__(self, path, conn, *, client, settings, run_id, source_name):
        self.calls.append(source_name)
        stem = Path(source_name).stem
        reply = self.replies.get(stem)
        if reply is None and (Path(__file__).parents[1] / "fixtures" / "real" / f"{stem}.reply.json").is_file():
            reply = real_reply(stem)
        run_client = client if reply is None else scripted(reply)
        return run_pipeline(path, conn, client=run_client, settings=settings, run_id=run_id, source_name=source_name)


def build_app(tmp_path, *, run_fn=None, settings=None, mode="offline"):
    settings = settings or api_settings(tmp_path)
    db = tmp_path / "app.db"
    reset_database(db, DEMO)
    tracker = CostTracker(Decimal("0.25"), Decimal("5"))
    client = MeteredClient(OfflineClient(), tracker, settings.llm_prices)
    worker = RunWorker(db, client, settings, run_fn=run_fn or ScriptedRuns())
    app = create_app(settings, mode=mode, client=client, db_path=db, tracker=tracker, worker=worker)
    return app, worker, db, settings


@contextmanager
def api(tmp_path, **kw):
    app, worker, db, settings = build_app(tmp_path, **kw)
    with TestClient(app) as c:
        c.worker, c.db_path, c.settings = worker, db, settings
        yield c


@contextmanager
def live_server(app):
    """The app under a real uvicorn server on a free localhost port (TestClient buffers streams; a browser does not)."""
    import socket
    import threading
    import time

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(15)


def upload(c, name: str, data: bytes | None = None, filename: str | None = None, content_type="application/octet-stream"):
    body = real_pdf(name).read_bytes() if data is None else data
    fname = filename or (real_pdf(name).name if data is None else name)
    return c.post("/api/runs", files={"file": (fname, body, content_type)})


def run_and_wait(c, name: str, **kw) -> str:
    r = upload(c, name, **kw)
    assert r.status_code == 202, r.text
    assert c.worker.wait_idle(60)
    return r.json()["run_id"]


def sse_events(text: str) -> list[dict]:
    """Parse an SSE body into [{event, id, data}] (comment lines dropped)."""
    out, cur = [], {}
    for line in text.splitlines():
        if not line:
            if cur:
                out.append(cur)
                cur = {}
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if key == "data":
            cur["data"] = json.loads(value)
        else:
            cur[key] = value
    if cur:
        out.append(cur)
    return out
