"""Extract one invoice from the command line.

    python -m app.extraction.cli <file> [--mode auto|vision|text_and_vision|text] [--max-cost USD]
                                        [--replay DIR | --record DIR] [--ingest-only] [--events] [-v]

Runs ingest then extract and prints: the extracted JSON, the path used (text_and_vision / vision_only), tokens,
cost, whether the API accepted thinking=disabled with effort=low, and the grounding status available at this
stage. Exit codes: 0 ok, 1 degraded extraction, 2 file rejected / bad usage, 3 API key not configured, 4 the API rejected
the extraction schema (a configuration problem: the message names LLM_STRUCTURED_OUTPUT).
"""
import argparse
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path

from app.config import get_settings
from app.extraction.preflight import SCHEMA_EXIT_CODE, is_schema_rejection, schema_rejection_message
from app.extraction.stage import run_extract_stage
from app.ingest.stage import run_ingest_stage
from app.ingest.store import new_run_id
from app.ingest.validate import IngestRejected
from app.llm.budget import CostTracker, get_session_tracker
from app.llm.client import AnthropicClient, MeteredClient
from app.llm.errors import LLMConfigError
from app.llm.replay import RecordingClient, ReplayClient
from app.models import RunContext


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _thinking_line(meta) -> str:
    if meta.thinking_mode is None:
        return "not exercised (no live API response in this run: replayed, or no call completed)"
    if meta.param_fallback:
        return (f"thinking=disabled + effort={meta.effort}: REJECTED by the API; fell back to effort={meta.effort} only "
                f"(thinking omitted). Set LLM_THINKING=omit to skip the failed first attempt.")
    if meta.thinking_mode == "disabled":
        return f"thinking=disabled + effort={meta.effort}: ACCEPTED by the API"
    return f"thinking omitted + effort={meta.effort} (as configured)"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.extraction.cli", description="Ingest and extract one invoice.")
    p.add_argument("file", type=Path, help="PDF, PNG or JPEG invoice")
    p.add_argument("--mode", choices=["auto", "vision", "text_and_vision", "text"], help="override EXTRACTION_MODE")
    p.add_argument("--run-id", help="run folder name under data/runs (default: a new random id)")
    p.add_argument("--max-cost", type=Decimal, help="cost ceiling in USD for this invocation (run and session)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--replay", type=Path, help="serve responses recorded in DIR instead of calling the API")
    g.add_argument("--record", type=Path, help="call the API and record each response into DIR")
    p.add_argument("--ingest-only", action="store_true", help="stop after ingest (no LLM call, no key needed)")
    p.add_argument("--events", action="store_true", help="also print the audit events")
    p.add_argument("-v", "--verbose", action="store_true", help="log LLM calls")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")      # Windows consoles default to cp1252
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if args.mode:
        settings = settings.model_copy(update={"extraction_mode": args.mode})

    client = None
    if not args.ingest_only:
        tracker = CostTracker(args.max_cost, args.max_cost) if args.max_cost is not None else get_session_tracker()
        if args.replay:
            inner = ReplayClient(args.replay)
        else:
            try:
                real = AnthropicClient(settings)
            except LLMConfigError as exc:
                print(f"NOT CONFIGURED: {exc.message}\n(Use --ingest-only to test ingest without a key, or --replay DIR.)")
                return 3
            inner = RecordingClient(real, args.record) if args.record else real
        client = MeteredClient(inner, tracker, settings.llm_prices)

    ctx = RunContext(run_id=args.run_id or new_run_id(), source_file=args.file.name)
    try:
        ingest_stage = run_ingest_stage(ctx, args.file, settings)
    except IngestRejected as exc:
        print(f"REJECTED [{exc.code}]: {exc.message}")
        return 2
    except ValueError as exc:
        print(f"BAD USAGE: {exc}")
        return 2
    info = ctx.ingest

    _section("ingest")
    print(f"file:        {info.original_name}  ({info.media_type}, {info.size_bytes} bytes)")
    print(f"sha256:      {info.sha256}")
    print(f"run folder:  {info.run_dir}")
    print(f"pages:       {info.pages_processed} processed of {info.pages_total}" + ("  (TRUNCATED at the page cap)" if info.truncated else ""))
    layer = info.text_layer
    print(f"text layer:  {layer.reason} (usable={layer.usable}, chars per page={layer.chars_by_page}"
          + (f", wordlike={layer.wordlike_ratio}" if layer.wordlike_ratio is not None else "") + ")")
    if info.failure_kind:
        print(f"FAILED:      [{info.failure_kind}/{info.failure_code}] {info.failure_reason}")
    if args.ingest_only:
        if args.events:
            _print_events(ingest_stage)
        return 1 if info.failure_kind else 0

    stage = run_extract_stage(ctx, client, settings)
    meta = ctx.extraction_meta
    if is_schema_rejection(meta):                       # a config problem, not a bad invoice: say so instead of a degraded run
        print()
        print(schema_rejection_message(meta))
        return SCHEMA_EXIT_CODE

    _section("extracted invoice (JSON)")
    print(json.dumps(ctx.extracted.model_dump(mode="json"), indent=2, ensure_ascii=False))

    _section("summary")
    print(f"path used:   {meta.path}   (mode requested: {meta.mode_requested}; text layer usable: {meta.text_layer_usable})")
    print(f"model:       {meta.model}   prompt: {meta.prompt_version}   structured output: {meta.structured_output}")
    print(f"thinking:    {_thinking_line(meta)}")
    print(f"attempts:    {meta.attempts}   (schema repair used: {'yes' if meta.schema_repair_used else 'no'})")
    print(f"tokens:      in={meta.tokens_in} out={meta.tokens_out}")
    print(f"cost:        ${meta.cost_usd:.6f} this run" + (f"   (session total ${client.tracker.session_spent:.6f})" if client else ""))
    print(f"latency:     {meta.latency_ms} ms")
    print("grounding:   not run yet (arrives in Stage 4); per-field model confidence is in the JSON above")
    if meta.degraded:
        print(f"RESULT:      DEGRADED [{meta.failure_kind}/{meta.failure_code}] {meta.failure_reason}")
    else:
        print("RESULT:      OK")
    if args.events:
        _print_events(ingest_stage)
        _print_events(stage)
    return 1 if meta.degraded else 0


def _print_events(stage) -> None:
    _section(f"{stage.stage} events")
    for e in stage.events:
        print(f"[{e.outcome.value:<4}] {e.event_type}: {e.message}")


if __name__ == "__main__":
    sys.exit(main())
