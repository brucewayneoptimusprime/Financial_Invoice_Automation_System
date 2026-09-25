"""The HTTP endpoints (PLAN M4 section 3). Each request opens and closes its own SQLite connection."""
import uuid
from contextlib import closing

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api import sse, views
from app.api.main import ApiState
from app.api.uploads import MULTIPART_SLACK_BYTES, STATUS_FOR_CODE, save_upload
from app.api.worker import Job, open_db
from app.ingest.store import check_run_id
from app.ingest.validate import IngestRejected

router = APIRouter()


def _state(request: Request) -> ApiState:
    return request.app.state.api


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def _valid_id(run_id: str) -> bool:
    try:
        check_run_id(run_id)
        return True
    except ValueError:
        return False


@router.get("/health")
def health(request: Request) -> dict:
    st = _state(request)
    return {"status": "ok", "mode": st.mode, "model": st.settings.model_name,
            "session_spent_usd": None if st.tracker is None else str(st.tracker.session_spent),
            "session_ceiling_usd": str(st.settings.cost_ceiling_per_session_usd),
            "run_ceiling_usd": str(st.settings.cost_ceiling_per_run_usd),
            "db_path": str(st.db_path), "replay_dir": None if st.replay_dir is None else str(st.replay_dir),
            "queue_length": st.worker.queue_length, "max_file_bytes": st.settings.max_file_bytes}


@router.post("/runs", status_code=202)
async def create_run(request: Request):
    st = _state(request)
    limit = st.settings.max_file_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit + MULTIPART_SLACK_BYTES:
        return _error(413, "too_large", f"The upload is larger than the {limit / 1_048_576:.1f} MB limit.")   # nothing read
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        return _error(422, "no_file", "Send the invoice as multipart/form-data in a field named 'file'.")
    form = await request.form(max_files=1, max_fields=4)
    try:
        upload = form.get("file")
        if upload is None or isinstance(upload, str) or not hasattr(upload, "file"):
            return _error(422, "no_file", "Send the invoice as multipart/form-data in a field named 'file'.")
        run_id = uuid.uuid4().hex
        try:
            saved = await run_in_threadpool(save_upload, upload.file, upload.filename, st.settings.api_upload_dir / run_id, st.settings)
        except IngestRejected as exc:
            return _error(STATUS_FOR_CODE.get(exc.code, 400), exc.code, exc.message)
    finally:
        await form.close()
    st.worker.submit(Job(run_id=run_id, path=saved.path, source_name=saved.display_name, folder=saved.folder))
    return {"run_id": run_id, "status": "queued", "source_file": saved.display_name, "media_type": saved.media_type,
            "size_bytes": saved.size_bytes, "view": f"/api/runs/{run_id}", "events": f"/api/runs/{run_id}/events"}


@router.get("/runs")
def list_runs(request: Request, limit: int = Query(20, ge=1)) -> dict:
    st = _state(request)
    limit = min(limit, st.settings.api_recent_runs_max)
    with closing(open_db(st.db_path, st.settings)) as conn:
        runs = views.recent_runs(conn, limit)
    known = {r["id"] for r in runs}
    queued = [p for p in st.worker.pending() if p["id"] not in known]
    return {"runs": queued + runs}


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str):
    st = _state(request)
    if not _valid_id(run_id):
        return _error(404, "not_found", "No such run.")
    with closing(open_db(st.db_path, st.settings)) as conn:
        view = views.run_view(conn, run_id, st.settings.runs_dir)
    if view is not None:
        return view
    state = st.worker.state(run_id)
    if state is None:
        return _error(404, "not_found", "No such run.")
    return {"run": {"id": run_id, "status": state}, "rejection": st.worker.rejection(run_id)}


@router.get("/runs/{run_id}/events")
def stream_events(request: Request, run_id: str, after: int | None = Query(None, ge=-1)):
    st = _state(request)
    if not _valid_id(run_id):
        return _error(404, "not_found", "No such run.")
    with closing(open_db(st.db_path, st.settings)) as conn:
        known = views.run_row(conn, run_id) is not None
    if not known and st.worker.state(run_id) is None:
        return _error(404, "not_found", "No such run.")
    header = request.headers.get("last-event-id")
    start = sse.parse_last_event_id(header) if header is not None else (-1 if after is None else after)
    body = sse.run_events(run_id, open_conn=lambda: open_db(st.db_path, st.settings), worker=st.worker, after=start,
                          poll_s=st.settings.sse_poll_ms / 1000, heartbeat_s=st.settings.sse_heartbeat_s,
                          is_disconnected=request.is_disconnected)
    return StreamingResponse(body, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@router.get("/runs/{run_id}/pages/{n}")
def get_page(request: Request, run_id: str, n: int):
    st = _state(request)
    if not _valid_id(run_id) or n < 1:
        return _error(404, "not_found", "No such page.")
    with closing(open_db(st.db_path, st.settings)) as conn:
        if views.run_row(conn, run_id) is None:
            return _error(404, "not_found", "No such page.")
    path = views.page_path(st.settings.runs_dir, run_id, n)
    if path is None:
        return _error(404, "not_found", "No such page.")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})
