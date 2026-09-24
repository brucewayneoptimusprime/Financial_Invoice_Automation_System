"""Record real responses to fixture files and replay them offline (tests, `eval --replay`).

A fixture holds a SUMMARY of the request (hashes and sizes, never image bytes) and the response. Headers,
keys and request content are never written. The lookup key is a hash of everything that determines the
answer: model, system prompt, schema, and the text/image parts.
"""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from app.llm.types import LLMClient, LLMRequest, LLMResponse, LLMUsage
from app.llm.errors import ReplayMiss


def request_key(request: LLMRequest) -> str:
    parts = [{"t": p.text} if p.kind == "text" else {"i": hashlib.sha256(p.data).hexdigest(), "m": p.media_type}
             for p in request.parts]
    payload = json.dumps({"model": request.model, "system": request.system, "schema": request.schema, "parts": parts},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _summary(request: LLMRequest) -> dict:
    return {
        "model": request.model, "purpose": request.purpose,
        "system_sha256": hashlib.sha256(request.system.encode("utf-8")).hexdigest(),
        "parts": [{"kind": "text", "chars": len(p.text)} if p.kind == "text"
                  else {"kind": "image", "media_type": p.media_type, "bytes": len(p.data),
                        "sha256": hashlib.sha256(p.data).hexdigest()} for p in request.parts],
    }


class RecordingClient:
    """Calls the real client and writes each request/response pair into `directory`."""

    def __init__(self, inner: LLMClient, directory: Path):
        self.inner, self.directory = inner, Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def complete(self, request: LLMRequest) -> LLMResponse:
        response = self.inner.complete(request)
        key = request_key(request)
        record = {
            "key": key, "request": _summary(request),
            "response": {"text": response.text, "stop_reason": response.stop_reason, "model": response.model,
                         "request_id": response.request_id,
                         "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens,
                                   "cache_read_tokens": response.usage.cache_read_tokens,
                                   "cache_write_tokens": response.usage.cache_write_tokens}},
        }
        (self.directory / f"{key}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False),
                                                     encoding="utf-8", newline="\n")
        return response


class ReplayClient:
    """Serves recorded responses; a request with no fixture raises ReplayMiss (never falls through to the network)."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def complete(self, request: LLMRequest) -> LLMResponse:
        key = request_key(request)
        path = self.directory / f"{key}.json"
        if not path.is_file():
            raise ReplayMiss(f"No recorded response for this request (key {key[:12]}...) in {self.directory}.")
        data = json.loads(path.read_text(encoding="utf-8"))["response"]
        return replace(
            LLMResponse(text=data["text"], usage=LLMUsage(**data["usage"]), stop_reason=data["stop_reason"],
                        model=data["model"], request_id=data.get("request_id"), latency_ms=0))
