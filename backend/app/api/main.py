"""The FastAPI application factory. `serve.py` builds the client (by mode) and calls `create_app`; tests call it directly."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.clients import Mode
from app.api.worker import RunWorker
from app.config import Settings
from app.llm.budget import CostTracker
from app.llm.types import LLMClient


@dataclass
class ApiState:
    settings: Settings
    mode: Mode
    db_path: Path
    worker: RunWorker
    tracker: CostTracker | None = None
    replay_dir: Path | None = None


def create_app(settings: Settings, *, mode: Mode, client: LLMClient, db_path: Path | None = None, tracker: CostTracker | None = None,
               replay_dir: Path | None = None, worker: RunWorker | None = None) -> FastAPI:
    from app.api.routes import router          # imported here so the routes can import ApiState without a cycle
    from app.api.routes_po import router as po_router
    from app.api.routes_review import router as review_router

    db_path = Path(db_path or settings.db_path)
    state = ApiState(settings=settings, mode=mode, db_path=db_path, tracker=tracker, replay_dir=replay_dir,
                     worker=worker or RunWorker(db_path, client, settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.worker.start()
        try:
            yield
        finally:
            state.worker.stop()

    app = FastAPI(title="Invoice agent", version="0.4.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json",
                  redoc_url=None)
    app.state.api = state
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.api_cors_origins), allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type", "Last-Event-ID"], allow_credentials=False)
    app.include_router(router, prefix="/api")
    app.include_router(po_router, prefix="/api")
    app.include_router(review_router, prefix="/api")
    return app
