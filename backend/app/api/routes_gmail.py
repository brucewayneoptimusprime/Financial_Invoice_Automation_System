"""Gmail import endpoints (SPEC section 11 items 81-87). All of them sit behind the ACCESS_TOKEN gate except the OAuth callback (a
browser redirect from Google cannot carry the token; it is protected by single-use state, PKCE and the binding cookie instead).
Search writes nothing; nothing is ever imported without an explicit request listing the picks."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from app.api.main import ApiState
from app.gmail.errors import GmailError
from app.gmail.oauth import BINDING_COOKIE, COOKIE_PATH, OAuthFailed

router = APIRouter(prefix="/gmail")


def _state(request: Request) -> ApiState:
    return request.app.state.api


def _gmail_error(exc: GmailError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status)


class ImportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    part_id: str


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_id: str
    items: list[ImportItem]
    confirm: bool = False


class DisconnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = ""


@router.get("/status")
def status(request: Request) -> dict:
    return _state(request).gmail.status()


@router.post("/search")
async def search(request: Request, body: SearchRequest):
    try:
        result = await run_in_threadpool(_state(request).gmail.search, body.query)
    except GmailError as exc:
        return _gmail_error(exc)
    return result.model_dump()


@router.post("/import")
async def import_attachments(request: Request, body: ImportRequest):
    """Only the picked attachments of one recent search; each one becomes an ordinary run in the upload queue."""
    st = _state(request)
    try:
        outcomes = await run_in_threadpool(st.gmail.import_items, body.search_id, [(i.message_id, i.part_id) for i in body.items],
                                           confirm=body.confirm, submit=st.worker.submit)
    except GmailError as exc:
        return _gmail_error(exc)
    return {"items": outcomes, "queued": sum(o["status"] == "queued" for o in outcomes)}


@router.post("/oauth/start")
def oauth_start(request: Request):
    """Google's authorization URL for the read-only scope; the browser goes there. Sets the HttpOnly binding cookie."""
    st = _state(request)
    try:
        url, binding = st.gmail.start_connect()
    except GmailError as exc:
        return _gmail_error(exc)
    response = JSONResponse({"authorization_url": url})
    response.set_cookie(BINDING_COOKIE, binding, max_age=st.settings.gmail_oauth_state_ttl_s, path=COOKIE_PATH, httponly=True,
                        samesite="lax")
    return response


def _return_url(base: str, query: str) -> str:
    return f"{base}{'&' if '?' in base else '?'}{query}"


@router.get("/oauth/callback")
def oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """Google sends the browser here. Always answers with a redirect to the fixed UI address, carrying only a result code."""
    st = _state(request)
    try:
        st.gmail.finish_connect(code=code, state=state, error=error, binding=request.cookies.get(BINDING_COOKIE))
        result = "gmail=connected"
    except OAuthFailed as exc:
        result = f"gmail=error&code={exc.code}"
    except GmailError as exc:
        result = f"gmail=error&code={'not_set_up' if exc.code == 'not_set_up' else 'exchange_failed'}"
    response = RedirectResponse(_return_url(st.settings.gmail_ui_return_url, result), status_code=303)
    response.delete_cookie(BINDING_COOKIE, path=COOKIE_PATH)
    return response


@router.post("/disconnect")
def disconnect(request: Request, body: DisconnectRequest):
    if body.confirm is not True:
        return _gmail_error(GmailError("confirm_required", "Confirm the disconnect."))
    return _state(request).gmail.disconnect()
