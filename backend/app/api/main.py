"""The FastAPI application factory. `serve.py` builds the client (by mode) and calls `create_app`; tests call it directly."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.access import AccessTokenMiddleware, health
from app.api.clients import Mode
from app.api.worker import RunWorker
from app.config import Settings
from app.gmail.client import GmailClient
from app.gmail.service import GmailService
from app.llm.budget import CostTracker
from app.llm.types import LLMClient


@dataclass
class ApiState:
    settings: Settings
    mode: Mode
    db_path: Path
    worker: RunWorker
    gmail: GmailService
    tracker: CostTracker | None = None
    replay_dir: Path | None = None


def create_app(settings: Settings, *, mode: Mode, client: LLMClient, db_path: Path | None = None, tracker: CostTracker | None = None,
               replay_dir: Path | None = None, worker: RunWorker | None = None, gmail_client: GmailClient | None = None) -> FastAPI:
    from app.api.routes import router          # imported here so the routes can import ApiState without a cycle
    from app.api.routes_po import router as po_router
    from app.api.routes_gmail import router as gmail_router
    from app.api.routes_review import router as review_router

    db_path = Path(db_path or settings.db_path)
    state = ApiState(settings=settings, mode=mode, db_path=db_path, tracker=tracker, replay_dir=replay_dir,
                     worker=worker or RunWorker(db_path, client, settings),
                     gmail=GmailService(settings, db_path, client=gmail_client, tracker=tracker))

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
    # Order: the token check runs INSIDE CORS, so a 401 still carries the CORS headers and preflights never need the token.
    app.add_middleware(AccessTokenMiddleware, token=settings.access_token_value())
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.api_cors_origins), allow_origin_regex=settings.api_cors_origin_regex,
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Last-Event-ID", "Authorization"],
                       allow_credentials=False)

    @app.get("/health", include_in_schema=False)
    def health_check():
        status, body = health(state.db_path, state.mode)
        return JSONResponse(body, status_code=status)

    app.include_router(router, prefix="/api")
    app.include_router(po_router, prefix="/api")
    app.include_router(review_router, prefix="/api")
    app.include_router(gmail_router, prefix="/api")
    return app
