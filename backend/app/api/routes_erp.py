"""Simulated ERP purchase-order feed (ERP_PLAN section 2; SPEC section 11 item 97). Behind ACCESS_TOKEN like every /api route.

GET /api/erp/preview is read-only. POST /api/erp/import saves only the ticked POs, through the importer and `save_po`.
With ERP_FEED_ENABLED=false both answer 404. No model, no cost.
"""
from contextlib import closing

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.main import ApiState
from app.api.worker import open_db
from app.erp.importer import ImportRefused, import_pos
from app.erp.preview import build_preview
from app.erp.source import FeedError, feed_source

router = APIRouter()


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feed_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    po_numbers: list[str] = Field(default_factory=list, max_length=1000)
    confirm: bool = False


def _state(request: Request) -> ApiState:
    return request.app.state.api


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


def _disabled(st: ApiState) -> JSONResponse | None:
    return None if st.settings.erp_feed_enabled else _error(404, "disabled", "The simulated ERP feed is switched off.")


@router.get("/erp/preview")
def erp_preview(request: Request):
    st = _state(request)
    if (off := _disabled(st)) is not None:
        return off
    try:
        feed = feed_source(st.settings).fetch()
        with closing(open_db(st.db_path, st.settings)) as conn:
            return build_preview(conn, st.settings, feed)
    except FeedError as e:
        return _error(422, e.code, e.message)


@router.post("/erp/import")
def erp_import(request: Request, body: ImportRequest):
    st = _state(request)
    if (off := _disabled(st)) is not None:
        return off
    if body.confirm is not True:
        return _error(400, "not_confirmed", "Confirm the import (confirm: true); nothing was saved.")
    try:
        feed = feed_source(st.settings).fetch()
        with closing(open_db(st.db_path, st.settings)) as conn:
            return import_pos(conn, st.settings, feed, body.feed_sha256, body.po_numbers)
    except FeedError as e:
        return _error(422, e.code, e.message)
    except ImportRefused as e:
        return _error(e.status, e.code, e.message, **e.extra)
