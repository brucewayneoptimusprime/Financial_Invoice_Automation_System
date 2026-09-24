"""LIVE tests: real API calls that cost money. Deselected by default (`-m "not live"`) and skipped when no
ANTHROPIC_API_KEY is configured. Run with:  pytest -m live -s
Each test uses its own tiny cost ceiling."""
from decimal import Decimal

import pytest

from app.config import Settings
from app.llm.budget import CostTracker
from app.llm.client import LLMRequest, build_llm_client, text_part
from app.llm.probe import PROBE_SCHEMA, run_probe

pytestmark = pytest.mark.live


def live_client():
    settings = Settings()                                             # reads the key from the environment / .env
    return settings, build_llm_client(settings, CostTracker(Decimal("0.05"), Decimal("0.05")))


def test_api_accepts_thinking_disabled_with_effort_low_or_reports_the_fallback(capsys):
    settings, client = live_client()
    report = run_probe(client, settings)
    with capsys.disabled():
        print(f"\n[live] {report.verdict}")
    assert report.response.text.strip().startswith("{")
    assert report.response.usage.output_tokens > 0 and report.response.cost_usd < Decimal("0.01")


def test_structured_output_round_trip():
    settings, client = live_client()
    response = client.complete(LLMRequest(
        system="Reply only with the requested JSON.", parts=(text_part('Return {"ok": true}.'),),
        model=settings.model_name, max_output_tokens=64, schema=PROBE_SCHEMA, run_id="live-test", purpose="live"))
    import json
    assert json.loads(response.text) == {"ok": True}
    assert response.stop_reason == "end_turn" and response.request_id
