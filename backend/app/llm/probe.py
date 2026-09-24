"""Tiny, cheap live checks.

    python -m app.llm.probe            does the API accept thinking=disabled together with effort=low?
    python -m app.llm.probe --schema   does the API accept the REAL extraction wire schema? (text-only prompt, no
                                       images, so a schema-compile error shows up for about a cent, or nothing)

If the API refuses thinking=disabled + effort, the client falls back to effort only and the report says so.
Exit codes: 0 ok, 1 LLM error / schema rejected, 2 not configured (no API key).
"""
import argparse
import json
import sys
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.llm.client import LLMClient, LLMRequest, LLMResponse, build_llm_client, text_part
from app.llm.errors import LLMBadRequestError, LLMConfigError, LLMError

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


SCHEMA_PROBE_SYSTEM = ("You fill a form. The document is empty: nothing is present on it. Set found to false and use the "
                       "placeholders for every field, empty arrays for lists, and unknown for every enumerated value.")


@dataclass(frozen=True)
class SchemaProbeReport:
    accepted: bool
    message: str
    response: LLMResponse | None = None
    converts: bool | None = None          # did the reply convert to an all-null ExtractedInvoice?


def run_schema_probe(client: LLMClient, settings: Settings) -> SchemaProbeReport:
    """Send the REAL wire schema with a tiny text-only prompt. A schema the API cannot compile fails here (HTTP 400)."""
    from app.extraction.postprocess import postprocess
    from app.extraction.wire import from_wire, wire_schema

    request = LLMRequest(system=SCHEMA_PROBE_SYSTEM, parts=(text_part("Fill the form for an empty document."),),
                         model=settings.model_name, max_output_tokens=1500, schema=wire_schema(),
                         run_id="probe-schema", purpose="probe-schema")
    try:
        response = client.complete(request)
    except LLMBadRequestError as exc:
        return SchemaProbeReport(False, f"SCHEMA REJECTED by the API: {exc.message}")
    try:
        contract, notes = from_wire(json.loads(response.text))
        invoice = postprocess(contract, settings, notes).invoice
    except (ValueError, TypeError) as exc:
        return SchemaProbeReport(True, f"schema ACCEPTED, but the reply did not convert: {str(exc)[:200]}", response, False)
    all_null = invoice.total.value is None and invoice.vendor_name.value is None and not invoice.line_items
    return SchemaProbeReport(True, "schema ACCEPTED by the API", response, all_null)


def _print_response(r: LLMResponse) -> None:
    print(f"model:      {r.model}")
    print(f"sent:       thinking={r.thinking_mode}, effort={r.effort}" + (f"  (fell back: {r.param_fallback})" if r.param_fallback else ""))
    print(f"tokens:     in={r.usage.input_tokens} out={r.usage.output_tokens}")
    print(f"cost:       ${r.cost_usd:.6f}" if r.cost_usd is not None else "cost:       n/a")
    print(f"latency:    {r.latency_ms} ms   request_id: {r.request_id}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m app.llm.probe", description=__doc__.splitlines()[0])
    parser.add_argument("--schema", action="store_true", help="probe the real extraction wire schema (text-only)")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.schema:
        try:
            client = build_llm_client(settings)
            sreport = run_schema_probe(client, settings)
        except LLMConfigError as exc:
            print(f"NOT CONFIGURED: {exc.message}")
            return 2
        except LLMError as exc:
            print(f"PROBE FAILED [{exc.code}]: {exc.message}")
            return 1
        print(f"result:     {sreport.message}")
        if sreport.response is not None:
            _print_response(sreport.response)
            if sreport.converts is not None:
                print("conversion: " + ("the reply converts to an all-null ExtractedInvoice" if sreport.converts
                                        else "the reply converted, but not to an all-null invoice"))
        return 0 if sreport.accepted and sreport.converts is not False else 1
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
