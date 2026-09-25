"""PO endpoints (PLAN "PO integration" section 4). Save is the ONLY route that creates a purchase order."""
from contextlib import closing

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api.main import ApiState
from app.api.worker import open_db
from app.po import drafts as po_drafts
from app.po import views as po_views
from app.po.models import SaveRequest, ValidateRequest
from app.po.store import DuplicatePONumber, save_po
from app.po.validate import lines_sum, validate_po

router = APIRouter()


def _state(request: Request) -> ApiState:
    return request.app.state.api


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


@router.get("/vendors")
def list_vendors(request: Request) -> dict:
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        return {"vendors": po_views.vendors(conn)}


@router.get("/pos")
def list_pos(request: Request, q: str | None = Query(None, max_length=100), status: str | None = Query(None, max_length=20)) -> dict:
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        return {"pos": po_views.po_list(conn, q=q or None, status=status or None)}


@router.get("/pos/{po_id}")
def get_po(request: Request, po_id: int):
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        view = po_views.po_detail(conn, po_id)
    if view is None:
        return _error(404, "not_found", "No such purchase order.")
    return view


@router.post("/pos/validate")
def validate(request: Request, body: ValidateRequest) -> dict:
    """Nothing is saved: the form calls this as the person types."""
    st = _state(request)
    with closing(open_db(st.db_path, st.settings)) as conn:
        issues, parsed = validate_po(body.po, conn, st.settings, body.new_vendor)
    return {"issues": [i.as_dict() for i in issues], "can_save": parsed is not None, "lines_sum": lines_sum(body.po)}


@router.post("/pos", status_code=201)
def create_po(request: Request, body: SaveRequest):
    """Save: the values the person confirmed on the form. Never a draft by itself."""
    st = _state(request)
    draft = None
    if body.draft_id is not None:
        draft = po_drafts.load_draft(st.settings, body.draft_id)
        if draft is None:
            return _error(422, "unknown_draft", "That draft does not exist (it may have been removed); save again without it.")
    with closing(open_db(st.db_path, st.settings)) as conn:
        issues, parsed = validate_po(body.po, conn, st.settings, body.new_vendor)
        if parsed is None:
            dup = any(i.code == "duplicate" and i.field == "po_number" for i in issues)
            return _error(409 if dup else 422, "duplicate" if dup else "invalid",
                          "A purchase order with this number already exists." if dup else "The purchase order has problems to fix.",
                          issues=[i.as_dict() for i in issues])
        provenance = {"source": "manual"}
        if draft is not None:
            provenance = {"source": draft.get("source", "manual"), "draft_id": body.draft_id,
                          "edited_fields": po_drafts.edited_fields(draft, body), **(draft.get("provenance") or {})}
        try:
            po_id, vendor_id = save_po(conn, parsed, provenance=provenance, new_vendor=body.new_vendor)
        except DuplicatePONumber:
            return _error(409, "duplicate", "A purchase order with this number already exists.")
    return {"po_id": po_id, "vendor_id": vendor_id, "warnings": [i.as_dict() for i in issues if i.level == "warning"]}
