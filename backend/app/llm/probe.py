"""One tiny, cheap live call that answers: does the API accept thinking=disabled together with effort=low?

    python -m app.llm.probe            (or the `llm-probe` console script)

Costs a fraction of a cent. If the API refuses the pair, the client falls back to effort=low only (thinking
omitted) and this report says so. Exit codes: 0 ok, 1 LLM error, 2 not configured (no API key).
"""
import sys
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.llm.client import LLMClient, LLMRequest, LLMResponse, build_llm_client, text_part
from app.llm.errors import LLMConfigError, LLMError

PROBE_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                "additionalProperties": False}


@dataclass(frozen=True)
class ProbeReport:
    verdict: str
    accepted_as_configured: bool
    response: LLMResponse


def run_probe(client: LLMClient, settings: Settings) -> ProbeReport:
    request = LLMRequest(
        system="You are a JSON echo service. Reply only with the requested JSON.",
        parts=(text_part('Return the JSON object {"ok": true}.'),), model=settings.model_name, max_output_tokens=64,
        schema=PROBE_SCHEMA, run_id="probe", purpose="probe")
    response = client.complete(request)
    if response.param_fallback:
        verdict = (f"thinking=disabled + effort={settings.llm_effort}: REJECTED by the API ({response.param_fallback_reason}). "
                   f"Fell back to effort={settings.llm_effort} only (thinking omitted, so adaptive thinking runs at that effort). "
                   "Set LLM_THINKING=omit to skip the failed first attempt on every call.")
        accepted = False
    elif settings.llm_thinking == "disabled":
        verdict = f"thinking=disabled + effort={settings.llm_effort}: ACCEPTED by the API."
        accepted = True
    else:
        verdict = f"thinking omitted + effort={settings.llm_effort} (as configured): accepted."
        accepted = True
    return ProbeReport(verdict, accepted, response)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = get_settings()
    try:
        client = build_llm_client(settings)
        report = run_probe(client, settings)
    except LLMConfigError as exc:
        print(f"NOT CONFIGURED: {exc.message}")
        return 2
    except LLMError as exc:
        print(f"PROBE FAILED [{exc.code}]: {exc.message}")
        return 1
    r = report.response
    print(f"model:      {r.model}")
    print(f"result:     {report.verdict}")
    print(f"sent:       thinking={r.thinking_mode}, effort={r.effort}")
    print(f"tokens:     in={r.usage.input_tokens} out={r.usage.output_tokens}")
    print(f"cost:       ${r.cost_usd:.6f}" if r.cost_usd is not None else "cost:       n/a")
    print(f"latency:    {r.latency_ms} ms   request_id: {r.request_id}")
    print(f"output:     {r.text.strip()[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
