"""Measure extraction accuracy on real invoices against a hand-verified answer key.

    python -m app.extraction.eval [--dir data/invoices] [--manifest data/manifest.md] [--mode auto]
                                  [--replay DIR | --live [--record DIR]] [--max-cost USD] [--draft-manifest]

LIVE CALLS NEED --live. Without `--replay DIR` this program would call the paid API, so it REFUSES unless you pass
`--live` (and `--record DIR` also needs `--live`). The refusal happens before any client exists: no key is read, nothing
is sent, nothing is spent. `--replay DIR` serves responses recorded earlier and never touches the network.

Only manifest entries with `"verified": true` are scored (see manifest.py). `--draft-manifest` appends draft entries
(`"verified": false`) for files that have none; it never edits an existing entry and never marks anything verified.

Exit codes: 0 ok, 1 some file could not be extracted, 2 bad usage, 3 API key not configured (live), 4 the API rejected
the extraction schema, 5 refused to make a live call because --live was not given.
"""
import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from app.config import ROOT_DIR, Settings, get_settings
from app.extraction.manifest import Manifest, append_drafts, draft_expected, load_manifest
from app.extraction.preflight import SCHEMA_EXIT_CODE, is_schema_rejection, schema_rejection_message
from app.extraction.scoring import FileScore, Totals, score_file
from app.extraction.stage import run_extract_stage
from app.ingest.stage import run_ingest_stage
from app.ingest.store import new_run_id
from app.ingest.validate import IngestRejected
from app.llm.budget import CostTracker
from app.llm.client import AnthropicClient, MeteredClient
from app.llm.errors import LLMConfigError
from app.llm.replay import RecordingClient, ReplayClient
from app.models import RunContext
from app.models.extraction import ExtractedInvoice
from app.models.extraction_meta import ExtractionMeta

EXIT_OK, EXIT_FAILURES, EXIT_USAGE, EXIT_NOT_CONFIGURED, EXIT_SCHEMA, EXIT_LIVE_REFUSED = 0, 1, 2, 3, SCHEMA_EXIT_CODE, 5
DEFAULT_DIR = ROOT_DIR / "data" / "invoices"
DEFAULT_MANIFEST = ROOT_DIR / "data" / "manifest.md"
INVOICE_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg")
ESTIMATED_COST_PER_INVOICE = Decimal("0.023")        # measured live on two real one-page invoices (SPEC section 11)


class LiveNotAllowed(RuntimeError):
    """A live API call was requested without the explicit --live flag."""


@dataclass
class FileResult:
    file: str
    run_id: str | None = None
    rejected: str | None = None                  # ingest rejection message
    meta: ExtractionMeta | None = None
    invoice: ExtractedInvoice | None = None
    score: FileScore | None = None

    @property
    def failed(self) -> bool:
        return self.rejected is not None or (self.meta is not None and self.meta.degraded)

    @property
    def failure(self) -> str:
        if self.rejected:
            return f"rejected: {self.rejected}"
        m = self.meta
        return f"[{m.failure_kind}/{m.failure_code}] {m.failure_reason}" if m and m.degraded else ""


@dataclass
class EvalReport:
    directory: Path
    manifest_path: Path
    mode: str                                    # "replay" | "live"
    results: list[FileResult] = field(default_factory=list)
    not_processed: list[str] = field(default_factory=list)
    stopped_reason: str | None = None
    totals: Totals = field(default_factory=Totals)
    unverified: list[str] = field(default_factory=list)      # manifest entries present but not verified
    unlabelled: list[str] = field(default_factory=list)      # files with no manifest entry
    missing_files: list[str] = field(default_factory=list)   # manifest entries whose file is not in the folder
    warnings: list[str] = field(default_factory=list)
    drafts_written: list[str] = field(default_factory=list)
    drafts_skipped: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0


# ---------------------------------------------------------------------------------------------- the client

def build_client(settings: Settings, tracker: CostTracker, *, replay: Path | None = None, allow_live: bool = False,
                 record: Path | None = None) -> MeteredClient:
    """The ONLY place this program builds an LLM client. A live client is built only when `allow_live` is true."""
    if replay is not None:
        return MeteredClient(ReplayClient(replay), tracker, settings.llm_prices)
    if not allow_live:
        raise LiveNotAllowed("a live API call was requested without --live")
    real = AnthropicClient(settings)                                   # LLMConfigError when there is no key
    inner = RecordingClient(real, record) if record is not None else real
    return MeteredClient(inner, tracker, settings.llm_prices)


def live_refusal_message(n_files: int, record: bool) -> str:
    return (
        "REFUSED: no live API call was made (nothing was sent, nothing was spent).\n"
        f"This run would call the paid API for {n_files} invoice(s)"
        + (" and record the responses" if record else "") + ", and --live was not given.\n"
        f"  To allow it:      python -m app.extraction.eval --live [--record DIR] [--max-cost USD]   "
        f"(about ${ESTIMATED_COST_PER_INVOICE} per one-page invoice)\n"
        "  To run for free:  python -m app.extraction.eval --replay DIR   (responses recorded earlier)")


# ------------------------------------------------------------------------------------------------ the run

def discover(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in INVOICE_SUFFIXES),
                  key=lambda p: p.name.lower())


def _say(text: str = "") -> None:
    print(text, flush=True)


def run_eval(files: list[Path], manifest: Manifest, manifest_path: Path, directory: Path, client: MeteredClient,
             settings: Settings, *, mode: str, draft: bool = False, progress: bool = True) -> EvalReport | int:
    """Extract every file and score the verified ones. Returns an EvalReport, or an exit code (schema rejection)."""
    report = EvalReport(directory=directory, manifest_path=manifest_path, mode=mode)
    names = {f.name for f in files}
    report.missing_files = sorted(set(manifest.entries) - names)
    report.unlabelled = sorted(names - set(manifest.entries))
    report.unverified = sorted(name for name, e in manifest.entries.items() if not e.verified and name in names)
    report.warnings.extend(manifest.problems)
    report.warnings.extend(f"manifest section '{h}' has no expected block, but a file with that name exists"
                           for h in manifest.sections_without_block if h in names)
    drafts: dict[str, tuple[dict, list[str]]] = {}

    for index, path in enumerate(files, start=1):
        result = FileResult(file=path.name)
        ctx = RunContext(run_id=new_run_id(), source_file=path.name)
        result.run_id = ctx.run_id
        try:
            run_ingest_stage(ctx, path, settings)
        except IngestRejected as exc:
            result.rejected = f"[{exc.code}] {exc.message}"
        except ValueError as exc:
            result.rejected = str(exc)
        if result.rejected is None:
            run_extract_stage(ctx, client, settings)
            result.meta, result.invoice = ctx.extraction_meta, ctx.extracted
            if is_schema_rejection(result.meta):
                print()
                print(schema_rejection_message(result.meta))
                return SCHEMA_EXIT_CODE
            report.tokens_in += result.meta.tokens_in
            report.tokens_out += result.meta.tokens_out
            report.cost_usd += result.meta.cost_usd
            report.latency_ms += result.meta.latency_ms
            entry = manifest.entries.get(path.name)
            if not result.failed:
                if entry is not None and entry.verified:
                    result.score = score_file(path.name, entry.expected, result.invoice)
                    report.totals.add(result.score)
                if draft:
                    drafts[path.name] = (draft_expected(result.invoice),
                                         [f"draft extracted by {result.meta.model} (prompt {result.meta.prompt_version}): CHECK EVERY VALUE "
                                          f"against the document, then set \"verified\" to true"])
        report.results.append(result)
        if progress:
            _say(_progress_line(index, len(files), result))
        if result.meta is not None and result.meta.failure_code == "cost_ceiling":
            report.stopped_reason = f"cost ceiling reached: {result.meta.failure_reason}"
            report.not_processed = [f.name for f in files[index:]]
            break

    if draft and drafts:
        report.drafts_written, report.drafts_skipped = append_drafts(manifest_path, drafts)
    return report


def _progress_line(index: int, total: int, r: FileResult) -> str:
    head = f"[{index}/{total}] {r.file}"
    if r.rejected:
        return f"{head}: REJECTED {r.rejected}"
    m = r.meta
    if r.failed:
        return f"{head}: FAILED {r.failure}"
    scored = "" if r.score is None else f"  scored: {'all correct' if r.score.all_correct else 'DIFFERENCES'}"
    return f"{head}: ok  ${m.cost_usd:.4f}  {m.latency_ms / 1000:.1f}s  path={m.path}{scored}"


# ---------------------------------------------------------------------------------------------- the report

def _mean(values: list[float]) -> str:
    return "n/a" if not values else f"{sum(values) / len(values):.2f}"


def format_report(r: EvalReport) -> str:
    out: list[str] = []
    ok = [x for x in r.results if not x.failed]
    failed = [x for x in r.results if x.failed]
    out += ["", "=" * 78, f"EVAL REPORT ({'LIVE API calls' if r.mode == 'live' else 'replayed responses, no API calls'})", "=" * 78,
            f"folder:    {r.directory}", f"manifest:  {r.manifest_path}"]
    scored = r.totals.files
    out.append(f"files:     {len(r.results)} processed, {len(ok)} extracted, {len(failed)} failed | "
               f"{scored} scored (verified), {len(r.unverified)} unverified (NOT scored), {len(r.unlabelled)} unlabelled (NOT scored)")
    if r.stopped_reason:
        out.append(f"STOPPED EARLY: {r.stopped_reason}; not processed: {', '.join(r.not_processed) or 'none'}")

    out += ["", "FIELD ACCURACY (verified files only)"]
    if scored == 0:
        out.append("  nothing scored: no processed file has a manifest entry with \"verified\": true.")
        if r.unverified:
            out.append(f"  unverified entries waiting for a human check: {', '.join(r.unverified)}")
    else:
        out.append(f"  {'field':<16}{'scored':>7}{'correct':>9}{'missed':>8}{'halluc.':>9}{'wrong':>7}{'accuracy':>10}")
        total_scored = total_correct = 0
        for name, s in r.totals.fields.items():
            acc = "n/a" if s.accuracy is None else f"{s.accuracy * 100:.1f}%"
            out.append(f"  {name:<16}{s.scored:>7}{s.correct:>9}{s.missed:>8}{s.hallucinated:>9}{s.wrong:>7}{acc:>10}")
            total_scored += s.scored
            total_correct += s.correct
        overall = "n/a" if not total_scored else f"{total_correct / total_scored * 100:.1f}%"
        out.append(f"  {'ALL':<16}{total_scored:>7}{total_correct:>9}{'':>8}{'':>9}{'':>7}{overall:>10}")
        out.append(f"  files with every scored field correct: {r.totals.files_all_correct} of {scored}")
        out += ["", "CONFIDENCE (effective, after grounding | the model's raw score)",
                f"  correct answers: {_mean(r.totals.conf_correct)} | {_mean(r.totals.model_conf_correct)}   (n={len(r.totals.conf_correct)})",
                f"  wrong answers:   {_mean(r.totals.conf_wrong)} | {_mean(r.totals.model_conf_wrong)}   (n={len(r.totals.conf_wrong)})"]
        differences = [(x.file, j) for x in r.results if x.score for j in x.score.judgements if j.verdict != "correct"]
        if differences:
            out += ["", "DIFFERENCES"]
            for file, j in differences:
                conf = "" if j.confidence is None else f"  (confidence {j.confidence:.2f})"
                out.append(f"  {file}: {j.field} {j.verdict.upper()}: expected {j.expected!r}, extracted {j.actual!r}{conf}")

    grounding: dict[str, int] = {}
    paths: dict[str, int] = {}
    for x in ok:
        for status, n in x.meta.grounding.items():
            grounding[status] = grounding.get(status, 0) + n
        paths[x.meta.path] = paths.get(x.meta.path, 0) + 1
    if ok:
        out += ["", "GROUNDING (all extracted files): " + (", ".join(f"{n} {k}" for k, n in sorted(grounding.items(), key=lambda kv: (-kv[1], kv[0]))) or "nothing checked"),
                "PATHS: " + ", ".join(f"{n} {k}" for k, n in sorted(paths.items()))]
        suspected = [x.file for x in ok if x.meta.injection_suspected]
        if suspected:
            out.append(f"READER INSTRUCTIONS SUSPECTED in: {', '.join(suspected)}")
    if failed:
        out += ["", "FAILURES (not scored)"] + [f"  {x.file}: {x.failure}" for x in failed]
    if r.unlabelled:
        out += ["", f"UNLABELLED (extracted, not scored): {', '.join(r.unlabelled)}"]
    if r.missing_files:
        out += ["", f"WARNING: manifest entries with no file in the folder: {', '.join(r.missing_files)}"]
    for w in r.warnings:
        out.append(f"WARNING: {w}")
    if r.drafts_written:
        out += ["", f"DRAFTS written to {r.manifest_path.name} (all \"verified\": false; check each value against the document, then set it to true): "
                    + ", ".join(r.drafts_written)]
    if r.drafts_skipped:
        out.append(f"drafts skipped (the manifest already has an entry; left untouched): {', '.join(r.drafts_skipped)}")
    out += ["", f"TOKENS: in={r.tokens_in} out={r.tokens_out}   COST: ${r.cost_usd:.6f}   API TIME: {r.latency_ms / 1000:.1f}s",
            "=" * 78]
    return "\n".join(out)


# --------------------------------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.extraction.eval", description="Score extraction against data/manifest.md.")
    p.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="folder of invoices (default: data/invoices)")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="answer key (default: data/manifest.md)")
    p.add_argument("--mode", choices=["auto", "vision", "text_and_vision", "text"], help="override EXTRACTION_MODE")
    p.add_argument("--replay", type=Path, help="serve responses recorded in DIR (no API call, no key needed)")
    p.add_argument("--live", action="store_true", help="ALLOW real, paid API calls (required unless --replay is given)")
    p.add_argument("--record", type=Path, help="with --live: also record each response into DIR")
    p.add_argument("--max-cost", type=Decimal, help="cost ceiling in USD for this whole run (default: the session ceiling in config)")
    p.add_argument("--draft-manifest", action="store_true",
                   help="append draft entries (verified=false) for files with no manifest entry; never edits existing entries")
    p.add_argument("-v", "--verbose", action="store_true", help="log LLM calls")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.replay is not None and (args.live or args.record is not None):
        print("BAD USAGE: --replay serves recorded responses and cannot be combined with --live or --record.")
        return EXIT_USAGE
    if args.record is not None and not args.live:
        print(live_refusal_message(len(discover(args.dir)), record=True))          # --record means calling the API
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
    files = discover(args.dir)
    manifest = load_manifest(args.manifest)

    if not files:
        _say(f"0 invoices found in {args.dir}")
        _say("Add PDF, PNG or JPEG invoices to that folder, then run this again "
             "(add --draft-manifest to start an answer key; check it by hand before setting \"verified\": true).")
        for problem in manifest.problems:
            _say(f"WARNING: {problem}")
        return EXIT_OK

    live = args.replay is None
    if live and not args.live:
        print(live_refusal_message(len(files), record=False))
        return EXIT_LIVE_REFUSED

    ceiling = args.max_cost if args.max_cost is not None else settings.cost_ceiling_per_session_usd
    tracker = CostTracker(min(settings.cost_ceiling_per_run_usd, ceiling), ceiling)
    try:
        client = build_client(settings, tracker, replay=args.replay, allow_live=args.live, record=args.record)
    except LLMConfigError as exc:
        print(f"NOT CONFIGURED: {exc.message}\n(Use --replay DIR to run offline.)")
        return EXIT_NOT_CONFIGURED

    if live:
        _say(f"LIVE MODE: up to {len(files)} paid API call(s) (about ${ESTIMATED_COST_PER_INVOICE} each, about "
             f"${ESTIMATED_COST_PER_INVOICE * len(files):.2f} in total); cost ceiling ${ceiling}.")
    else:
        _say(f"REPLAY MODE: serving recorded responses from {args.replay}; no API calls.")
    started = time.monotonic()
    result = run_eval(files, manifest, args.manifest, args.dir, client, settings, mode="live" if live else "replay",
                      draft=args.draft_manifest)
    if isinstance(result, int):
        return result
    _say(format_report(result))
    _say(f"wall time {time.monotonic() - started:.1f}s; session spend ${tracker.session_spent:.6f}")
    return EXIT_FAILURES if any(x.failed for x in result.results) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
