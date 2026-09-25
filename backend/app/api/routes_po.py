"""PO endpoints (PLAN "PO integration" section 4). Save is the ONLY route that creates a purchase order."""
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.api.main import ApiState
from app.api.uploads import MULTIPART_SLACK_BYTES, STATUS_FOR_CODE, disk_name, display_name, remove_upload
from app.api.views import page_numbers, page_path
from app.api.worker import open_db
from app.ingest.store import check_run_id
from app.po import drafts as po_drafts
from app.po import views as po_views
from app.po.drafter import POModelDraft, draft_po
from app.po.models import SaveRequest, ValidateRequest
from app.po.prompts import document_parts, typed_text_parts
from app.po.readers import PODocRejected, read_po_document
from app.po.service import build_draft_view
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


# ---------------------------------------------------------------------------------------------- model drafts
# They use the server's ONE client (the --live / --replay / --offline rule), write NOTHING to the database, and return a draft
# for the confirmation form. Only POST /api/pos (above) saves.

OFFLINE_MESSAGE = "Offline mode: no model is available to draft a purchase order. Use the form instead."


def _discard(folder: Path) -> None:
    remove_upload(folder)
    try:
        folder.parent.rmdir()                                          # the shared _incoming folder, when empty
    except OSError:
        pass


def _offline(st: ApiState) -> JSONResponse | None:
    return _error(503, "offline", OFFLINE_MESSAGE) if st.mode == "offline" else None


class TextDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)


@router.post("/pos/drafts/text")
def draft_from_text(request: Request, body: TextDraftRequest):
    st = _state(request)
    if (refused := _offline(st)) is not None:
        return refused
    text = body.text.strip()
    if not text:
        return _error(422, "empty", "Type the purchase order first.")
    if len(text) > st.settings.po_text_max_chars:
        return _error(422, "too_long", f"The text is longer than {st.settings.po_text_max_chars} characters.")
    draft_id = po_drafts.new_draft_id()
    model_draft = draft_po(typed_text_parts(text), {1: text}, True, client=st.worker.client, settings=st.settings, draft_id=draft_id)
    with closing(open_db(st.db_path, st.settings)) as conn:
        return build_draft_view(model_draft, draft_id=draft_id, source="text", provenance={"text": text}, conn=conn,
                                settings=st.settings, pages=[])


@router.post("/pos/drafts/document")
async def draft_from_document(request: Request):
    st = _state(request)
    if (refused := _offline(st)) is not None:
        return refused
    limit = st.settings.max_file_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit + MULTIPART_SLACK_BYTES:
        return _error(413, "too_large", f"The upload is larger than the {limit / 1_048_576:.1f} MB limit.")
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        return _error(422, "no_file", "Send the purchase order as multipart/form-data in a field named 'file'.")
    form = await request.form(max_files=1, max_fields=4)
    draft_id = po_drafts.new_draft_id()
    folder = Path(st.settings.po_drafts_dir) / "_incoming" / draft_id    # ingest creates the draft folder itself
    try:
        upload = form.get("file")
        if upload is None or isinstance(upload, str) or not hasattr(upload, "file"):
            return _error(422, "no_file", "Send the purchase order as multipart/form-data in a field named 'file'.")
        name = display_name(upload.filename)
        folder.mkdir(parents=True, exist_ok=False)
        dest = folder / disk_name(upload.filename)
        written = 0
        with dest.open("wb") as out:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    out.close()
                    _discard(folder)
                    return _error(413, "too_large", f"{name} is larger than the {limit / 1_048_576:.1f} MB limit.")
                out.write(chunk)
    finally:
        await form.close()
    try:
        doc = await run_in_threadpool(read_po_document, dest, name, draft_id, st.settings)
    except PODocRejected as exc:
        _discard(folder)
        remove_upload(Path(st.settings.po_drafts_dir) / draft_id)
        return _error(STATUS_FOR_CODE.get(exc.code, 415 if exc.code in ("legacy_office", "macro_enabled") else 400), exc.code, exc.message)
    _discard(folder)                                                   # ingest keeps its own copy; text documents keep document.txt
    provenance = {"file_name": doc.file_name, "media_type": doc.media_type, "sha256": doc.sha256, "size_bytes": doc.size_bytes,
                  "document_kind": doc.kind, "path": doc.path}
    if doc.failure_code is not None:                                   # unreadable (e.g. password-protected PDF): no model call
        model_draft = POModelDraft(status="failed", failure_code=doc.failure_code, failure_message=doc.failure_message)
    else:
        parts = document_parts(doc.pages, doc.total_pages)
        model_draft = await run_in_threadpool(lambda: draft_po(parts, doc.page_texts, doc.text_usable, client=st.worker.client,
                                                               settings=st.settings, draft_id=draft_id))
        model_draft.notes = [*model_draft.notes, *doc.notes]
    pages = page_numbers(Path(st.settings.po_drafts_dir), draft_id)
    with closing(open_db(st.db_path, st.settings)) as conn:
        return build_draft_view(model_draft, draft_id=draft_id, source="document", provenance=provenance, conn=conn,
                                settings=st.settings, pages=pages)


@router.get("/pos/drafts/{draft_id}/pages/{n}")
def draft_page(request: Request, draft_id: str, n: int):
    st = _state(request)
    try:
        check_run_id(draft_id)
    except ValueError:
        return _error(404, "not_found", "No such page.")
    path = page_path(Path(st.settings.po_drafts_dir), draft_id, n) if n >= 1 else None
    if path is None:
        return _error(404, "not_found", "No such page.")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})
