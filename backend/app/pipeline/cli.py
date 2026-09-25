"""Run one invoice file through the whole pipeline and show every step.

    python -m app.pipeline.cli <file> (--replay DIR | --live [--record DIR]) [--db PATH] [--reset-demo] [--max-cost USD] [--json]

LIVE CALLS NEED --live. Without `--replay DIR` this would call the paid API, so it REFUSES unless you pass `--live` (`--record` also
needs it). The refusal happens before any client exists: no key is read and nothing is sent.

Prints, in order: ingest, extraction, PO match, validation (every rule), decision, explanation, actions and drafted emails, what was
written to the database (with the PO balance before and after), and tokens and cost per stage. Nothing is ever sent to a vendor.

Exit codes: 0 done (whatever the decision), 1 the run failed (rolled back, marked failed), 2 usage or the file was not accepted
(nothing written), 3 API key not configured (live), 4 the API rejected the extraction schema, 5 live call refused (no --live).
"""
import argparse
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path

from app.config import get_settings
from app.db.connection import connect
from app.db.reset import reset_database
from app.extraction.eval import LiveNotAllowed, build_client
from app.extraction.preflight import SCHEMA_EXIT_CODE, is_schema_rejection, schema_rejection_message
from app.ingest.validate import IngestRejected
from app.llm.budget import CostTracker
from app.llm.errors import LLMConfigError
from app.pipeline.runner import PipelineResult, run_pipeline

EXIT_DONE, EXIT_FAILED, EXIT_USAGE, EXIT_NOT_CONFIGURED, EXIT_SCHEMA, EXIT_LIVE_REFUSED = 0, 1, 2, 3, SCHEMA_EXIT_CODE, 5
ESTIMATED_COST = Decimal("0.03")          # extraction about $0.023 measured, plus about $0.006 per model role


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def live_refusal_message() -> str:
    return ("REFUSED: no live API call was made (nothing was sent, nothing was spent).\n"
            "This run would call the paid API for this invoice, and --live was not given.\n"
            f"  To allow it:      python -m app.pipeline.cli <file> --live [--record DIR] [--max-cost USD]   (about ${ESTIMATED_COST} per invoice)\n"
            "  To run for free:  python -m app.pipeline.cli <file> --replay DIR   (responses recorded earlier)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.pipeline.cli", description="Run one invoice through the whole pipeline.")
    p.add_argument("file", type=Path, help="PDF, PNG or JPEG invoice")
    p.add_argument("--db", type=Path, help="SQLite database (default from config)")
    p.add_argument("--reset-demo", action="store_true", help="first reset the database to the demo dataset (data/seed_demo.json)")
    p.add_argument("--replay", type=Path, help="serve responses recorded in DIR (no API call, no key needed)")
    p.add_argument("--live", action="store_true", help="ALLOW real, paid API calls (required unless --replay is given)")
    p.add_argument("--record", type=Path, help="with --live: also record each response into DIR")
    p.add_argument("--max-cost", type=Decimal, help="cost ceiling in USD for this invocation (default: the session ceiling in config)")
    p.add_argument("--mode", choices=["auto", "vision", "text_and_vision", "text"], help="override EXTRACTION_MODE")
    p.add_argument("--json", action="store_true", help="also print the full extracted invoice as JSON")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _money(value) -> str:
    return "-" if value is None else f"{value:,.2f}"


def print_result(result: PipelineResult, show_json: bool) -> None:
    ctx, stages = result.ctx, result.stages
    ingest = ctx.ingest
    _section("1. ingest")
    print(f"file:        {ingest.original_name}  ({ingest.media_type}, {ingest.size_bytes} bytes)")
    print(f"sha256:      {ingest.sha256}")
    print(f"pages:       {ingest.pages_processed} processed of {ingest.pages_total}" + ("  (TRUNCATED at the page cap)" if ingest.truncated else ""))
    print(f"text layer:  {ingest.text_layer.reason} (usable={ingest.text_layer.usable})")
    if ingest.failure_kind:
        print(f"FAILED:      [{ingest.failure_kind}/{ingest.failure_code}] {ingest.failure_reason}")

    meta, ex = ctx.extraction_meta, ctx.extracted
    if meta is not None and ex is not None:
        _section("2. extraction")
        print(f"path: {meta.path}   model: {meta.model}   prompt: {meta.prompt_version}   attempts: {meta.attempts}   "
              f"tokens in/out: {meta.tokens_in}/{meta.tokens_out}   cost: ${meta.cost_usd:.6f}")
        if meta.degraded:
            print(f"DEGRADED [{meta.failure_kind}/{meta.failure_code}]: {meta.failure_reason}")
        print(f"grounding: {meta.grounding or 'nothing checked'}" + (f"   READER INSTRUCTIONS SUSPECTED: {meta.injection_evidence[:2]}" if meta.injection_suspected else ""))
        print(f"  {'field':<15}{'value':<44}{'conf (model)':<14}grounding")
        for name in ("vendor_name", "vendor_tax_id", "document_type", "invoice_number", "invoice_date", "currency", "po_reference", "subtotal", "tax", "total"):
            f = getattr(ex, name)
            value = "-" if f.value is None else (f.value.isoformat() if hasattr(f.value, "isoformat") else str(f.value))
            conf = "-" if f.value is None else f"{f.confidence:.2f} ({f.model_confidence:.2f})"
            extra = " [tax included in total]" if name == "tax" and getattr(f, "included_in_total", None) else ""
            print(f"  {name:<15}{value[:42] + extra:<44}{conf:<14}{f.grounding.value if f.grounding else '-'}")
        for i, line in enumerate(ex.line_items, start=1):
            print(f"  line {i}: {(line.description or '-')[:60]}  qty {line.quantity}  unit {_money(line.unit_price)}  amount {_money(line.amount)}")
        for a in ex.adjustments:
            print(f"  adjustment: {a.kind} {_money(a.amount)}")
        if show_json:
            print(json.dumps(ex.model_dump(mode="json"), indent=2, ensure_ascii=False))

    _section("3. match")
    vm = ctx.matched_vendor
    if vm is not None:
        vendor = ctx.facts.vendor_by_id(vm.vendor_id) if (ctx.facts and vm.vendor_id is not None) else None
        print(f"vendor: {'%s (%s, score %.2f%s)' % (vendor.name, vm.method, vm.score, ', AMBIGUOUS' if vm.ambiguous else '') if vendor else 'no known vendor matched'}")
    print(f"PO match: {ctx.match_status.value if ctx.match_status else 'not run'}")
    for c in ctx.candidates[:3]:
        print(f"  {c.po_number}: score {c.score:.3f}  {c.breakdown}  {', '.join(c.reasons)}")

    _section("4. validation")
    for r in ctx.rule_results:
        mark = {"pass": "ok  ", "info": "info", "flag": "FLAG", "fail": "FAIL"}[r.outcome.value]
        print(f"  [{mark}] {r.rule_id:<24} sev {r.severity}  {r.message[:120]}")

    _section("5. decision")
    print(f"{result.decision.value.upper() if result.decision else 'NONE (run failed)'}"
          + (f"   (approve withheld: {result.downgraded_reason})" if result.downgraded_reason else ""))

    if result.explanation is not None:
        e = result.explanation
        _section("6. explanation")
        print(e.text)
        print(f"[source: {e.source}" + (f", model {e.model}" if e.model else "") + (f"; template used because: {e.fallback_reason}" if e.fallback_reason else "") + "]")

    _section("7. actions and drafts (nothing is sent)")
    printed = False
    if result.po_balance is not None:
        print(f"ledger: committed {_money(ctx.extracted.total.value)} on {result.po_number}; derived PO balance {_money(result.po_balance[0])} -> {_money(result.po_balance[1])}")
        printed = True
    for w in result.writes:
        if w.table == "review_queue":
            print(f"review queue: {w.summary}")
            printed = True
    if result.draft is not None:
        d = result.draft
        print(f"draft ({d.kind}, status draft, source {d.source}" + (f", model {d.model}" if d.model else "") + f"): to: {d.to or '(none: no vendor contact on file)'}")
        print(f"Subject: {d.subject}\n\n{d.body}")
        if d.fallback_reason:
            print(f"[template used because: {d.fallback_reason}]")
        printed = True
    if not printed:
        print("(no ledger commit, review item or draft for this decision)" if result.decision is not None else "(none)")

    _section("8. written to the database")
    if result.writes:
        for w in result.writes:
            print(f"  {w.table:<15}{'' if w.row_id is None else 'id ' + str(w.row_id):<8}{w.summary}")
    print(f"  runs           id {result.run_id}  status {result.status}" + (f"  final_decision {result.decision.value}" if result.decision else ""))

    _section("9. tokens and cost")
    for stage, cost in result.stage_costs.items():
        print(f"  {stage:<10}${cost:.6f}")
    print(f"  total     in={result.tokens_in} out={result.tokens_out}  ${result.cost_usd:.6f}")
    if result.error:
        print(f"\nRUN FAILED (rolled back): {result.error}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.replay is not None and (args.live or args.record is not None):
        print("BAD USAGE: --replay serves recorded responses and cannot be combined with --live or --record.")
        return EXIT_USAGE
    if args.replay is None and not args.live:                          # --record alone would also call the API
        print(live_refusal_message())
        return EXIT_LIVE_REFUSED
    if args.replay is not None and not args.replay.is_dir():
        print(f"BAD USAGE: --replay {args.replay} is not a folder of recorded responses.")
        return EXIT_USAGE
    if args.max_cost is not None and args.max_cost <= 0:
        print("BAD USAGE: --max-cost must be greater than zero.")
        return EXIT_USAGE

    settings = get_settings()
    if args.mode:
        settings = settings.model_copy(update={"extraction_mode": args.mode})
    db_path = args.db or settings.db_path
    if args.reset_demo:
        try:
            reset_database(db_path, settings.demo_seed_path)
        except ValueError as exc:
            print(f"BAD USAGE: {exc}")
            return EXIT_USAGE
        print(f"Database reset to the demo dataset: {db_path}")
    elif not Path(db_path).is_file():
        print(f"The database {db_path} does not exist. Create it with:  python -m app.db.reset --demo   (or pass --reset-demo)")
        return EXIT_USAGE

    ceiling = args.max_cost if args.max_cost is not None else settings.cost_ceiling_per_session_usd
    tracker = CostTracker(min(settings.cost_ceiling_per_run_usd, ceiling), ceiling)
    try:
        client = build_client(settings, tracker, replay=args.replay, allow_live=args.live, record=args.record)
    except LLMConfigError as exc:
        print(f"NOT CONFIGURED: {exc.message}\n(Use --replay DIR to run offline.)")
        return EXIT_NOT_CONFIGURED
    except LiveNotAllowed:                                              # unreachable: guarded above, kept as a second lock
        print(live_refusal_message())
        return EXIT_LIVE_REFUSED
    print(("LIVE MODE: this run calls the paid API (about $%s)." % ESTIMATED_COST) if args.replay is None
          else f"REPLAY MODE: serving recorded responses from {args.replay}; no API calls (the explainer and drafter fall back to templates on a replay miss).")

    conn = connect(db_path)
    try:
        try:
            result = run_pipeline(args.file, conn, client=client, settings=settings)
        except IngestRejected as exc:
            print(f"REJECTED [{exc.code}]: {exc.message}\n(no run was created; nothing was written)")
            return EXIT_USAGE
        except ValueError as exc:
            print(f"BAD USAGE: {exc}")
            return EXIT_USAGE
        print_result(result, args.json)
        if result.status == "failed":
            return EXIT_FAILED
        if is_schema_rejection(result.ctx.extraction_meta):
            print()
            print(schema_rejection_message(result.ctx.extraction_meta))
            return EXIT_SCHEMA
        return EXIT_DONE
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
