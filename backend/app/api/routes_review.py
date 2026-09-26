"""Review-queue endpoints (review actions + line allocation). One item at a time; no bulk endpoint exists."""
from contextlib import closing

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api import views
from app.api.main import ApiState
from app.api.worker import open_db
from app.review import actions, service

router = APIRouter()


def _state(request: Request) -> ApiState:
    return request.app.state.api


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


@router.get("/review-queue")
def list_items(request: Request, status: str = Query("open", pattern="^(open|resolved)$"), limit: int = Query(50, ge=1, le=200)) -> dict:
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        return {"items": service.list_items(conn, status, limit, st.settings), "open_count": service.open_count(conn)}


def item_detail(conn, item_id: int, settings) -> dict | None:
    c = service.load_item(conn, item_id)
    if c is None:
        return None
    events = views.events_after(conn, c.item["run_id"], -1, limit=100_000)
    explanation = next((e["detail"] for e in events if e["event_type"] == "explanation"), None)
    inv = c.invoice
    return {
        "item": {k: c.item[k] for k in ("id", "run_id", "reason", "status", "resolution", "resolved_at")},
        "run": None if c.run is None else {"id": c.run["id"], "source_file": c.run["source_file"], "status": c.run["status"],
                                           "decision": c.run["final_decision"], "started_at": c.run["started_at"], "explanation": explanation},
        "invoice": None if inv is None else {"id": inv["id"], "invoice_number": inv["invoice_number"], "invoice_date": inv["invoice_date"],
                                             "currency": inv["currency"], "total": service._s(inv["total"]), "status": inv["status"]},
        "approve": service.approve_preview(conn, c, settings),
        "line_matches": views.line_match_view(conn, c.item["run_id"], events, inv),
    }


@router.get("/review-queue/{item_id}")
def get_item(request: Request, item_id: int):
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        detail = item_detail(conn, item_id, st.settings)
    if detail is None:
        return _error(404, "not_found", "No such review item.")
    return detail


def _act(request: Request, fn, *args):
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        try:
            return fn(conn, *args)
        except actions.ActionError as exc:
            return JSONResponse(exc.body(), status_code=exc.status)


@router.post("/review-queue/{item_id}/approve")
def approve_item(request: Request, item_id: int, body: actions.ApproveRequest):
    """ONE item. Nothing is guessed: a missing line choice is a 422 listing what is needed."""
    return _act(request, lambda conn: actions.approve(conn, item_id, body, _state(request).settings))


@router.post("/review-queue/{item_id}/reject")
def reject_item(request: Request, item_id: int, body: actions.RejectRequest):
    """ONE item. Never writes a ledger entry or an allocation."""
    return _act(request, lambda conn: actions.reject(conn, item_id, body))
