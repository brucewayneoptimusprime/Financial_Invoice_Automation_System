"""Gmail import endpoints (SPEC section 11 items 81-85). All of them sit behind the ACCESS_TOKEN gate (the OAuth callback, a later
stage, will be the one exemption). Search writes nothing; nothing is ever imported without an explicit request listing the picks."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from app.api.main import ApiState
from app.gmail.errors import GmailError

router = APIRouter(prefix="/gmail")


def _state(request: Request) -> ApiState:
    return request.app.state.api


def _gmail_error(exc: GmailError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status)


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
