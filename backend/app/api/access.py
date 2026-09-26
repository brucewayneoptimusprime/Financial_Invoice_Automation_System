"""Deployment plumbing for the API: the optional access token and the public health check (DEPLOY.md).

Neither changes any business behaviour. With `ACCESS_TOKEN` unset (local development) the middleware lets everything through.
"""
import hmac
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Receive, Scope, Send

from app.db.init_db import SCHEMA_VERSION

PROTECTED_PREFIX = "/api"


class AccessTokenMiddleware:
    """When a token is configured, every /api request needs `Authorization: Bearer <token>` or `?access_token=<token>` (the
    EventSource stream and <img> page URLs cannot send headers). Compared in constant time. /health and CORS preflights pass."""

    def __init__(self, app: ASGIApp, token: str | None):
        self.app, self.token = app, token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (self.token is None or scope["type"] != "http" or scope.get("method") == "OPTIONS"
                or not scope.get("path", "").startswith(PROTECTED_PREFIX)):
            await self.app(scope, receive, send)
            return
        supplied = None
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                text = value.decode("latin-1")
                if text[:7].lower() == "bearer ":
                    supplied = text[7:].strip()
        if supplied is None:
            q = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            supplied = (q.get("access_token") or [None])[0]
        if supplied is not None and hmac.compare_digest(supplied.encode(), self.token.encode()):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": "unauthorized", "message": "An access token is required."}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
        await send({"type": "http.response.body", "body": body})


def health(db_path: Path, mode: str) -> tuple[int, dict]:
    """For the host's health check: the database opens read-only, answers, and has the expected schema. No paths, keys or money."""
    if not Path(db_path).is_file():
        return 503, {"status": "unavailable", "reason": "database missing", "mode": mode}
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        try:
            conn.execute("SELECT 1").fetchone()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 503, {"status": "unavailable", "reason": "database unreadable", "mode": mode}
    if version != SCHEMA_VERSION:
        return 503, {"status": "unavailable", "reason": f"schema version {version}, expected {SCHEMA_VERSION}", "mode": mode}
    return 200, {"status": "ok", "db": "ok", "schema_version": version, "mode": mode}
