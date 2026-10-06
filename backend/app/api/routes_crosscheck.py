"""Cross-check documents (CROSSCHECK_PLAN; SPEC section 11 item 99). REPORT ONLY.

Both routes are under /api, so they sit behind ACCESS_TOKEN like everything else. The database is opened READ-ONLY here: this
feature cannot write to the PO, its invoices, the ledger, the consumption tables, the review queue or any decision. Uploaded
files live in a per-analysis temp folder that is removed before the response is returned, whatever happened.
"""
import shutil
import tempfile
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.main import ApiState
from app.api.uploads import MULTIPART_SLACK_BYTES, disk_name, display_name
from app.crosscheck import service
from app.crosscheck.facts import load_po_context, open_readonly

router = APIRouter()
_CHUNK = 1024 * 1024


def _state(request: Request) -> ApiState:
    return request.app.state.api


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


def _po(st: ApiState, po_id: int):
    with closing(open_readonly(st.db_path, st.settings.api_busy_timeout_ms)) as conn:
        return load_po_context(conn, po_id)


def _gate(st: ApiState, po_id: int):
    """(PO context, None) or (None, the refusal)."""
    if not st.settings.crosscheck_enabled:
        return None, _error(404, "not_found", "Cross-check is switched off.")
    po = _po(st, po_id)
    if po is None:
        return None, _error(404, "not_found", "No such purchase order.")
    return po, None


@router.get("/pos/{po_id}/crosscheck")
def crosscheck_info(request: Request, po_id: int):
    """Limits, mode and the cost figures shown before Analyze. No model call."""
    st = _state(request)
    _, refused = _gate(st, po_id)
    return refused if refused is not None else service.info(st.settings, st.mode, st.tracker)


@router.post("/pos/{po_id}/crosscheck")
async def crosscheck_analyse(request: Request, po_id: int):
    """The only route that sends anything to the model: it runs when the person clicks Analyze."""
    st = _state(request)
    po, refused = await run_in_threadpool(_gate, st, po_id)
    if refused is not None:
        return refused
    if st.mode == "offline":
        return _error(503, "offline", service.OFFLINE_MESSAGE)
    limit, most = st.settings.max_file_bytes, st.settings.crosscheck_max_documents
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > most * (limit + MULTIPART_SLACK_BYTES):
        return _error(413, "too_large", f"The upload is larger than {most} files of {limit / 1_048_576:.1f} MB.")
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        return _error(422, "no_file", "Send the documents as multipart/form-data in a field named 'files'.")
    base = st.settings.crosscheck_tmp_dir
    if base is not None:
        Path(base).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="crosscheck-", dir=base))
    try:
        form = await request.form(max_files=most + 1, max_fields=4)
        try:
            uploads = [u for u in form.getlist("files") if not isinstance(u, str) and hasattr(u, "file")]
            if not uploads:
                return _error(422, "no_file", "Choose at least one document (PDF, PNG or JPG).")
            if len(uploads) > most:
                return _error(422, "too_many", f"At most {most} documents can be analysed at once. Nothing was sent.")
            files: list[service.Incoming] = []
            for n, upload in enumerate(uploads, start=1):
                name = display_name(upload.filename)
                folder = work / "in" / str(n)
                folder.mkdir(parents=True)
                dest, written, too_large = folder / disk_name(upload.filename), 0, False
                with dest.open("wb") as out:
                    while chunk := await upload.read(_CHUNK):
                        written += len(chunk)
                        if written > limit:
                            too_large = True
                            break
                        out.write(chunk)
                if too_large:
                    dest.unlink(missing_ok=True)
                    files.append(service.Incoming(name, rejection=("too_large", f"{name} is larger than the {limit / 1_048_576:.1f} MB limit.")))
                else:
                    files.append(service.Incoming(name, path=dest))
        finally:
            await form.close()
        try:
            return await run_in_threadpool(lambda: service.analyse(po, files, work, client=st.worker.client, tracker=st.tracker,
                                                                   settings=st.settings, mode=st.mode))
        except service.BudgetRefused as exc:
            return _error(409, "budget", exc.message, projected_usd=f"{exc.projected:.2f}", remaining_usd=f"{exc.remaining:.2f}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
