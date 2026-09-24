"""Tiny, cheap live checks (text only, no images).

    python -m app.llm.probe                         does the API accept thinking=disabled together with effort=low?
    python -m app.llm.probe --schema                does the API accept the REAL extraction schema (configured mode)?
    python -m app.llm.probe --schema --structured prompt_json    test one specific mode
    python -m app.llm.probe --schema --all          test BOTH structured-output modes and recommend one

Structured-output modes: "json_schema" sends the strict wire schema to the API (grammar-constrained; the API can refuse
it as too large or complex); "prompt_json" sends no schema, describes the JSON in the prompt and validates the reply here.
Each schema probe costs about a cent or less. If the API refuses thinking=disabled + effort, the client falls back to
effort only and the report says so.
Exit codes: 0 ok (with --all: at least one mode works), 1 LLM error / schema rejected, 2 not configured (no API key).
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
MODES = ("json_schema", "prompt_json")


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
    mode: str
    accepted: bool
    message: str
    response: LLMResponse | None = None
    converts: bool | None = None          # did the reply convert to an all-null ExtractedInvoice?

    @property
    def ok(self) -> bool:
        return self.accepted and self.converts is not False


def run_schema_probe(client: LLMClient, settings: Settings, mode: str | None = None) -> SchemaProbeReport:
    """Send the REAL wire schema (json_schema) or the REAL prompt-described shape (prompt_json) with a tiny text-only
    prompt. A schema the API cannot compile fails here with HTTP 400; its exact message is returned."""
    from app.extraction.extractor import parse_reply
    from app.extraction.postprocess import postprocess
    from app.extraction.prompts import PROMPT_JSON_SUFFIX
    from app.extraction.wire import from_wire, wire_schema

    mode = mode or settings.llm_structured_output
    schema = wire_schema()
    if mode == "json_schema":
        system, request_schema = SCHEMA_PROBE_SYSTEM, schema
    else:
        system = SCHEMA_PROBE_SYSTEM + PROMPT_JSON_SUFFIX.format(schema=json.dumps(schema, separators=(",", ":")))
        request_schema = None
    request = LLMRequest(system=system, parts=(text_part("Fill the form for an empty document."),),
                         model=settings.model_name, max_output_tokens=1500, schema=request_schema,
                         run_id="probe-schema", purpose="probe-schema")
    try:
        response = client.complete(request)
    except LLMBadRequestError as exc:
        return SchemaProbeReport(mode, False, f"SCHEMA REJECTED by the API: {exc.message}")
    try:
        contract, notes = from_wire(parse_reply(response.text, expect_key="fields"))
        invoice = postprocess(contract, settings, notes).invoice
    except (ValueError, TypeError) as exc:
        return SchemaProbeReport(mode, True, f"request ACCEPTED, but the reply did not convert: {str(exc)[:200]}", response, False)
    all_null = invoice.total.value is None and invoice.vendor_name.value is None and not invoice.line_items
    return SchemaProbeReport(mode, True, "ACCEPTED by the API" if mode == "json_schema" else "works", response, all_null)


def recommendation(reports: list[SchemaProbeReport]) -> str:
    by_mode = {r.mode: r for r in reports}
    js, pj = by_mode.get("json_schema"), by_mode.get("prompt_json")
    if js and js.ok:
        return "RECOMMENDATION: json_schema works. Keep the default (LLM_STRUCTURED_OUTPUT=json_schema)."
    if pj and pj.ok:
        return ("RECOMMENDATION: json_schema does NOT work but prompt_json does. Set LLM_STRUCTURED_OUTPUT=prompt_json "
                "in .env (or ask for the default to be flipped).")
    return "RECOMMENDATION: neither mode passed. Paste this output back; the exact API messages above say what to trim."


def _print_response(r: LLMResponse) -> None:
    print(f"  model:      {r.model}")
    print(f"  sent:       thinking={r.thinking_mode}, effort={r.effort}" + (f"  (fell back: {r.param_fallback})" if r.param_fallback else ""))
    print(f"  tokens:     in={r.usage.input_tokens} out={r.usage.output_tokens}")
    print(f"  cost:       ${r.cost_usd:.6f}" if r.cost_usd is not None else "  cost:       n/a")
    print(f"  latency:    {r.latency_ms} ms   request_id: {r.request_id}")


def _print_schema_report(r: SchemaProbeReport) -> None:
    print(f"[{r.mode}] result:     {r.message}")
    if r.response is not None:
        _print_response(r.response)
        if r.converts is not None:
            print("  conversion: " + ("the reply converts to an all-null ExtractedInvoice" if r.converts
                                      else "the reply did NOT convert to an all-null invoice"))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m app.llm.probe", description=__doc__.splitlines()[0])
    parser.add_argument("--schema", action="store_true", help="probe the real extraction schema (text only, no images)")
    parser.add_argument("--structured", choices=MODES, help="with --schema: probe this mode instead of the configured one")
    parser.add_argument("--all", action="store_true", help="with --schema: probe both modes and recommend one")
    args = parser.parse_args(argv)
    if (args.structured or args.all) and not args.schema:
        args.schema = True
    settings = get_settings()
    if args.schema:
        modes = list(MODES) if args.all else [args.structured or settings.llm_structured_output]
        reports: list[SchemaProbeReport] = []
        try:
            client = build_llm_client(settings)
            for mode in modes:
                reports.append(run_schema_probe(client, settings, mode))
        except LLMConfigError as exc:
            print(f"NOT CONFIGURED: {exc.message}")
            return 2
        except LLMError as exc:
            print(f"PROBE FAILED [{exc.code}]: {exc.message}")
            return 1
        for r in reports:
            _print_schema_report(r)
        if args.all:
            print(recommendation(reports))
            return 0 if any(r.ok for r in reports) else 1
        return 0 if reports[0].ok else 1
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
