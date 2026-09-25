"""The server's one LLM client, chosen by the mode `serve` was started with (the same live rule as the CLIs, SPEC item 68).

live     -> the real API, through `build_client(..., allow_live=True)` (optionally recording)
replay   -> recorded responses only (`build_client(..., replay=DIR)`); a miss degrades like an outage, never reaches the network
offline  -> no model at all: every call fails as "not configured", so extraction degrades to review and texts are templates

`build_client` in app.extraction.eval stays the ONLY place a real client is constructed.
"""
from pathlib import Path
from typing import Literal

from app.config import Settings
from app.extraction.eval import build_client
from app.llm.budget import CostTracker
from app.llm.client import MeteredClient
from app.llm.errors import LLMConfigError
from app.llm.types import LLMRequest, LLMResponse

Mode = Literal["live", "replay", "offline"]
OFFLINE_MESSAGE = "Offline mode: the server was started with --offline, so no model is called."


class OfflineClient:
    """Refuses every request without touching the network."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMConfigError(OFFLINE_MESSAGE)


def build_api_client(mode: Mode, settings: Settings, tracker: CostTracker, *, replay: Path | None = None,
                     record: Path | None = None) -> MeteredClient:
    if mode == "offline":
        return MeteredClient(OfflineClient(), tracker, settings.llm_prices)
    if mode == "replay":
        if replay is None:
            raise ValueError("replay mode needs a folder of recorded responses")
        return build_client(settings, tracker, replay=replay)
    if mode == "live":
        return build_client(settings, tracker, allow_live=True, record=record)
    raise ValueError(f"unknown mode {mode!r}")
