"""Rules settings endpoints (SETTINGS_PLAN; SPEC section 11 items 91-94). Behind ACCESS_TOKEN; GET and POST only (the CORS methods).
No model, no cost. Changes apply to invoices processed from now on; each changed value is logged in settings_events."""
from contextlib import closing
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.api.main import ApiState
from app.api.worker import open_db
from app.rulesettings import service
from app.rulesettings.catalog import SettingsError

router = APIRouter(prefix="/settings")


def _state(request: Request) -> ApiState:
    return request.app.state.api


def _invalid(exc: SettingsError) -> JSONResponse:
    first = next(iter(exc.problems.items()))
    return JSONResponse({"error": "invalid", "message": f"{first[0]} {first[1]}.", "problems": exc.problems}, status_code=422)


class GlobalChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = {}
    rules: dict[str, Any] = {}
    restore: list[str] = []


class POChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = {}           # a value, or null = reset to the default
    rules: dict[str, Any] = {}            # true / false, or null = reset to the default


@router.get("")
def get_global(request: Request) -> dict:
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        return service.global_view(conn, st.settings)


@router.post("/global")
def post_global(request: Request, body: GlobalChange):
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        try:
            return service.update_global(conn, values=body.values, rules=body.rules, restore=body.restore, settings=st.settings)
        except SettingsError as exc:
            return _invalid(exc)


@router.get("/pos")
def list_pos(request: Request, q: str | None = Query(None, max_length=100), custom: bool | None = None) -> dict:
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        return {"pos": service.po_list(conn, q or None, custom, st.settings)}


@router.get("/pos/{po_id}")
def get_po(request: Request, po_id: int):
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        try:
            return service.po_view(conn, po_id, st.settings)
        except service.NotFound:
            return JSONResponse({"error": "not_found", "message": "No such purchase order."}, status_code=404)


@router.post("/pos/{po_id}")
def post_po(request: Request, po_id: int, body: POChange):
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        try:
            return service.update_po(conn, po_id, values=body.values, rules=body.rules, settings=st.settings)
        except service.NotFound:
            return JSONResponse({"error": "not_found", "message": "No such purchase order."}, status_code=404)
        except SettingsError as exc:
            return _invalid(exc)


@router.get("/history")
def get_history(request: Request, scope: str | None = Query(None, pattern="^(global|po)$"), po_id: int | None = None,
                limit: int = Query(50, ge=1, le=500)) -> dict:
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        return {"events": service.history(conn, scope, po_id, limit)}
