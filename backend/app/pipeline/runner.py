"""One file through the whole pipeline: ingest -> extract -> match -> validate -> decide -> explain -> act, persisted to SQLite.

The M1/M2 stages are reused unchanged. This module only sequences them, persists their audit events after each stage, asks for an
explanation and (when a vendor must be asked) a draft, and performs the decision's actions in ONE transaction.

Order of safety:
  * an ingest rejection (bad type, empty, oversize) creates no run;
  * the `runs` row exists before any model call, so a crash leaves a visible run;
  * the decision is fixed before the explainer/drafter run and nothing they return can change it;
  * the act stage is atomic (invoice, lines, ledger, review item, draft, explanation event, run completion), and an approve is
    re-verified inside the transaction: if the records it relied on changed, it is downgraded to `review` (escalate-only);
  * any programming or database error rolls the act stage back and marks the run `failed` (no decision, no partial rows).
"""
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Callable

from app.config import Settings, get_settings
from app.engine.engine import run_decide_stage, run_validate_stage
from app.engine.loader import load_facts, load_rules
from app.engine.matching import run_match_stage
from app.db.consumption import record_consumption
from app.enums import Decision, MatchedBy, Outcome
from app.extraction.stage import run_extract_stage
from app.ingest.stage import run_ingest_stage
from app.ingest.store import check_run_id, new_run_id
from app.llm.client import build_llm_client
from app.llm.errors import LLMConfigError
from app.llm.types import LLMClient
from app.models.audit import AuditEvent
from app.models.run import RunContext, StageResult
from app.money import from_minor, to_minor
from app.pipeline import persist
from app.pipeline.actions import plan_actions
from app.pipeline.digest import TrailDigest, build_digest
from app.pipeline.draft import Draft, draft_for
from app.pipeline.explain import Explanation, explain, template_explanation
from app.pipeline.persist import AuditWriter, transaction
from app.pipeline.summary import (completed_event, started_event, summarize_act, summarize_decide, summarize_explain,
                                  summarize_extract, summarize_ingest, summarize_match, summarize_validate)

logger = logging.getLogger("app.pipeline")
PIPELINE_STAGE = "pipeline"
EXPLAIN_STAGE = "explain"
ACT_STAGE = "act"


@dataclass
class WriteRecord:
    table: str
    row_id: int | None
    summary: str


@dataclass
class PipelineResult:
    run_id: str
    status: str                                            # completed | failed
    ctx: RunContext | None = None
    decision: Decision | None = None
    digest: TrailDigest | None = None
    explanation: Explanation | None = None
    draft: Draft | None = None
    writes: list[WriteRecord] = field(default_factory=list)
    stages: dict[str, StageResult] = field(default_factory=dict)
    po_number: str | None = None
    po_balance: tuple[Decimal, Decimal] | None = None      # (before, after) the ledger commit
    downgraded_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    stage_costs: dict[str, Decimal] = field(default_factory=dict)
    error: str | None = None


ExplainFn = Callable[..., Explanation]
DraftFn = Callable[..., Draft]


def _event(stage: str, event_type: str, outcome: Outcome, message: str, detail: dict) -> AuditEvent:
    return AuditEvent(stage=stage, event_type=event_type, outcome=outcome, message=message, detail=detail)


def _explanation_event(e: Explanation) -> AuditEvent:
    return _event(EXPLAIN_STAGE, "explanation", Outcome.INFO, e.one_line,
                  {"text": e.text, "one_line": e.one_line, "reasons": [{"text": t, "facts": list(ids)} for t, ids in e.reasons],
                   "next_step": e.next_step, "cited_facts": list(e.cited), "source": e.source, "model": e.model,
                   "fallback_reason": e.fallback_reason, "attempts": e.attempts, "tokens_in": e.tokens_in, "tokens_out": e.tokens_out,
                   "cost_usd": e.cost_usd})


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)


def run_pipeline(path: Path, conn: sqlite3.Connection, *, client: LLMClient | None = None, settings: Settings | None = None,
                 explain_fn: ExplainFn = explain, draft_fn: DraftFn = draft_for,
                 on_stage: Callable[[str, StageResult | None], None] | None = None, run_id: str | None = None,
                 source_name: str | None = None, provenance: dict | None = None) -> PipelineResult:
    """Run one file. Raises IngestRejected / ValueError only for a file that is not accepted at all (no run is created).

    `run_id` lets a caller (the API) choose the id before the run starts; `source_name` is the name to record when `path` is a
    temporary upload copy. `provenance` (Gmail import) is recorded as one `source_gmail` event next to `run_started`; it is never
    read by any stage, the digest, the explainer or the drafter. Every stage is bracketed by `stage_started` (committed before the stage runs) and `stage_completed`
    (duration and a whitelisted summary) events; ingest's pair is written after it, since no run exists until ingest accepts the file.
    """
    settings = settings or get_settings()
    ctx = RunContext(run_id=new_run_id() if run_id is None else check_run_id(run_id), source_file=source_name or Path(path).name)
    result = PipelineResult(run_id=ctx.run_id, status="running", ctx=ctx)
    notify = on_stage or (lambda name, stage: None)

    if client is None:                                                    # one metered client for extraction, explainer and drafter
        try:
            client = build_llm_client(settings)
        except LLMConfigError:
            client = None                                                 # extraction degrades with a clear message; the template is used

    ingest_started = perf_counter()
    ingest = run_ingest_stage(ctx, Path(path), settings)                 # may raise: nothing has been written to the database
    ingest_ms = _elapsed_ms(ingest_started)
    result.stages["ingest"] = ingest
    with transaction(conn):
        persist.start_run(conn, ctx.run_id, ctx.source_file)
    writer = AuditWriter(conn, ctx.run_id)

    def begin(name: str) -> float:
        with transaction(conn):                                           # committed first, so a live view can show "running"
            writer.write([started_event(name)])
        return perf_counter()

    def persist_stage(name: str, stage: StageResult, duration_ms: int, summary: dict, *before: AuditEvent) -> None:
        result.stages[name] = stage
        with transaction(conn):
            writer.write([*before, *stage.events, completed_event(name, stage.status.value, duration_ms, summary)])
        notify(name, stage)

    try:
        source_events = [_event(PIPELINE_STAGE, "run_started", Outcome.INFO, f"Run started for {ctx.source_file}.",
                                {"source_file": ctx.source_file, "file_hash": ctx.file_hash,
                                 "source": "upload" if provenance is None else "gmail"})]
        if provenance is not None:
            source_events.append(_event(PIPELINE_STAGE, "source_gmail", Outcome.INFO,
                                        f"Imported from Gmail: {provenance.get('filename')} (email from {provenance.get('sender')}, "
                                        f"{provenance.get('message_date')}).", dict(provenance)))
        persist_stage("ingest", ingest, ingest_ms, summarize_ingest(ctx), *source_events, started_event("ingest"))
        t = begin("extract")
        extract = run_extract_stage(ctx, client, settings)
        persist_stage("extract", extract, _elapsed_ms(t), summarize_extract(ctx))
        meta = ctx.extraction_meta
        result.stage_costs["extract"] = meta.cost_usd
        t = begin("match")
        ctx.facts = load_facts(conn, ctx.run_id)
        match = run_match_stage(ctx)
        persist_stage("match", match, _elapsed_ms(t), summarize_match(ctx))
        t = begin("validate")
        rules = load_rules(conn)
        validate = run_validate_stage(ctx, rules)
        persist_stage("validate", validate, _elapsed_ms(t), summarize_validate(ctx))
        t = begin("decide")
        decide = run_decide_stage(ctx)
        persist_stage("decide", decide, _elapsed_ms(t), summarize_decide(decide))
        decision = ctx.decision
        t = begin("explain")
        digest = build_digest(ctx, settings, {r.id: r.name for r in rules})
        explanation = explain_fn(digest, ctx=ctx, client=client, settings=settings, run_id=ctx.run_id)
        with transaction(conn):                                           # the explanation text itself is written by the act stage
            writer.write([completed_event("explain", "ok", _elapsed_ms(t), summarize_explain(
                explanation.source, explanation.model, explanation.fallback_reason, explanation.attempts, explanation.cost_usd))])
        t = begin("act")
        draft = (draft_fn(digest, settings, ctx=ctx, client=client, run_id=ctx.run_id)
                 if decision in (Decision.REQUEST_INFO, Decision.REJECT) else None)
        _act(conn, writer, ctx, settings, result, digest, explanation, draft, act_started=t)
    except Exception as exc:                                              # noqa: BLE001 - the run must end up recorded as failed
        logger.exception("pipeline failed run=%s", ctx.run_id)
        _record_failure(conn, ctx.run_id, exc, result)
    return result


def _totals(result: PipelineResult, explanation: Explanation | None, draft: Draft | None) -> tuple[int, int, Decimal]:
    meta = result.ctx.extraction_meta if result.ctx else None
    t_in, t_out, cost = (meta.tokens_in, meta.tokens_out, meta.cost_usd) if meta else (0, 0, Decimal(0))
    for name, part in (("explain", explanation), ("draft", draft)):
        if part is not None:
            t_in, t_out, cost = t_in + part.tokens_in, t_out + part.tokens_out, cost + part.cost_usd
            result.stage_costs[name] = part.cost_usd
    return t_in, t_out, cost


def _act(conn: sqlite3.Connection, writer: AuditWriter, ctx: RunContext, settings: Settings, result: PipelineResult, digest: TrailDigest,
         explanation: Explanation, draft: Draft | None, act_started: float | None = None) -> None:
    decision = digest.decision
    writes: list[WriteRecord] = []
    with transaction(conn, immediate=True):
        # 1. An approve relies on a snapshot taken earlier. If anything it relied on changed, do not commit: escalate to review.
        withheld = None
        commit_minor = None
        if decision is Decision.APPROVE:
            po_fact = ctx.facts.po_by_id(ctx.matched_po.po_id) if (ctx.matched_po and ctx.facts) else None
            try:
                commit_minor = None if ctx.extracted is None or ctx.extracted.total.value is None else to_minor(ctx.extracted.total.value)
            except (ValueError, ArithmeticError):
                withheld = "the invoice total is not a whole number of cents, so it cannot be committed to the ledger"
            if withheld is None:
                withheld = persist.snapshot_is_current(
                    conn, ctx.run_id, len(ctx.facts.prior_invoices) if ctx.facts else 0, None if po_fact is None else po_fact.id,
                    None if po_fact is None else to_minor(po_fact.net_committed))
            if withheld is None and commit_minor is None:
                withheld = "there is no invoice total to commit"
        if withheld is not None:
            old = decision
            decision = Decision.REVIEW
            severity = settings.decision_severity[decision.value]
            ctx.decision = decision
            digest = digest.with_override(f"Approval withheld: {withheld}.", decision, severity)
            explanation = template_explanation(digest, fallback_reason="approval withheld in the act stage")
            writer.write([_event(ACT_STAGE, "decision_escalated", Outcome.FLAG, f"Approve escalated to review: {withheld}.",
                                 {"from": old.value, "to": decision.value, "reason": withheld})])
            result.downgraded_reason = withheld
            draft = None

        # 2. The explanation (written before the actions, so the trail reads in order).
        writer.write([_explanation_event(explanation)])

        # 3. The invoice, always.
        saved = persist.save_invoice(conn, ctx, decision)
        writes.append(WriteRecord("invoices", saved.invoice_id, f"decision {decision.value}, status {persist.STATUS_FOR_DECISION[decision].value}"))
        if saved.lines:
            writes.append(WriteRecord("invoice_lines", None, f"{saved.lines} line(s)"))
        matched_lines = persist.save_line_matches(conn, ctx.run_id, saved.invoice_id, ctx.line_matches)
        if matched_lines:
            writes.append(WriteRecord("invoice_line_matches", None, f"{matched_lines} line(s), mode {ctx.line_matches.mode}"))
        writer.write([_event(ACT_STAGE, "invoice_saved", Outcome.INFO, f"Invoice saved (id {saved.invoice_id}) with status "
                             f"{persist.STATUS_FOR_DECISION[decision].value}.", {"invoice_id": saved.invoice_id, "lines": saved.lines,
                                                                                 "notes": saved.notes})])
        plan = plan_actions(ctx, digest, settings)

        # 4. The decision's action.
        if plan.commit_ledger:
            po_id, po_number = ctx.matched_po.po_id, ctx.matched_po.po_number
            before = persist.po_balance_minor(conn, po_id)
            entry_id = persist.commit_ledger(conn, po_id, saved.invoice_id, commit_minor)
            # the allocation of that commit: against the PO total, no specific line (per-line allocation is the follow-up)
            record_consumption(conn, ledger_entry_id=entry_id, po_id=po_id, invoice_id=saved.invoice_id, run_id=ctx.run_id,
                               amount_minor=commit_minor, matched_by=MatchedBy.AUTO)
            after = persist.po_balance_minor(conn, po_id)
            status = persist.set_po_status_after_commit(conn, po_id)
            result.po_number, result.po_balance = po_number, (from_minor(before), from_minor(after))
            writes.append(WriteRecord("ledger_entries", entry_id, f"commit {from_minor(commit_minor):,.2f} on {po_number}"))
            writes.append(WriteRecord("po_consumption", None, "against the PO total (no specific line)"))
            writes.append(WriteRecord("purchase_orders", po_id, f"{po_number} status {status}; derived balance {from_minor(before):,.2f} -> {from_minor(after):,.2f}"))
            writer.write([_event(ACT_STAGE, "ledger_committed", Outcome.PASS, f"Committed {from_minor(commit_minor):,.2f} to {po_number}; "
                                 f"balance {from_minor(before):,.2f} -> {from_minor(after):,.2f}.",
                                 {"po_number": po_number, "po_id": po_id, "amount": from_minor(commit_minor), "balance_before": from_minor(before),
                                  "balance_after": from_minor(after), "over_balance": after < 0, "invoice_id": saved.invoice_id, "ledger_entry_id": entry_id}),
                          _event(ACT_STAGE, "po_status_updated", Outcome.INFO, f"{po_number} is now {status}.", {"po_number": po_number, "status": status})])
        if plan.ready_for_payment:
            writer.write([_event(ACT_STAGE, "ready_for_payment", Outcome.PASS, "The invoice is approved and ready for payment.",
                                 {"invoice_id": saved.invoice_id})])
        if plan.review_reason:
            rid = persist.enqueue_review(conn, ctx.run_id, plan.review_reason)
            writes.append(WriteRecord("review_queue", rid, "open: " + plan.review_reason[:120]))
            writer.write([_event(ACT_STAGE, "review_queued", Outcome.FLAG, "Placed in the review queue.", {"review_id": rid, "reason": plan.review_reason})])
        if plan.draft and draft is not None:
            did = persist.save_draft(conn, ctx.run_id, draft.kind, draft.to, draft.subject, draft.body)
            writes.append(WriteRecord("drafts", did, f"{draft.kind} (status draft, never sent): {draft.subject}"))
            writer.write([_event(ACT_STAGE, "draft_saved", Outcome.INFO, f"Draft {draft.kind} saved (status draft); nothing is sent.",
                                 {"draft_id": did, "kind": draft.kind, "subject": draft.subject, "source": draft.source, "model": draft.model,
                                  "fallback_reason": draft.fallback_reason, "requested": list(draft.requested), "tokens_in": draft.tokens_in,
                                  "tokens_out": draft.tokens_out, "cost_usd": draft.cost_usd})])

        # 5. Close the stage and the run in the same transaction.
        tables = Counter(w.table for w in writes)
        if saved.lines:
            tables["invoice_lines"] = saved.lines
        if matched_lines:
            tables["invoice_line_matches"] = matched_lines
        saved_draft = draft if (plan.draft and draft is not None) else None
        writer.write([completed_event("act", "flagged" if withheld is not None else "ok",
                                      0 if act_started is None else _elapsed_ms(act_started),
                                      summarize_act(decision.value, dict(tables), withheld is not None,
                                                    None if saved_draft is None else saved_draft.kind))])
        t_in, t_out, cost = _totals(result, explanation, draft)
        writer.write([_event(PIPELINE_STAGE, "run_completed", Outcome.INFO, f"Run completed: {decision.value}.",
                             {"decision": decision.value, "tokens_in": t_in, "tokens_out": t_out, "cost_usd": cost})])
        persist.finish_run(conn, ctx.run_id, status="completed", decision=decision, tokens_in=t_in, tokens_out=t_out, cost_usd=cost,
                           model=ctx.extraction_meta.model if ctx.extraction_meta else None)
    result.status, result.decision, result.digest, result.explanation, result.draft = "completed", decision, digest, explanation, draft
    result.writes = writes
    result.tokens_in, result.tokens_out, result.cost_usd = t_in, t_out, cost


def _record_failure(conn: sqlite3.Connection, run_id: str, exc: Exception, result: PipelineResult) -> None:
    result.status, result.error = "failed", f"{type(exc).__name__}: {exc}"
    try:
        if conn.in_transaction:
            conn.rollback()
        with transaction(conn):
            AuditWriter(conn, run_id).emit(PIPELINE_STAGE, "pipeline_error", Outcome.FAIL, f"The run failed: {type(exc).__name__}.",
                                           {"error_type": type(exc).__name__, "error": str(exc)[:500]})
            meta = result.ctx.extraction_meta if result.ctx else None
            persist.finish_run(conn, run_id, status="failed", decision=None, tokens_in=0 if meta is None else meta.tokens_in,
                               tokens_out=0 if meta is None else meta.tokens_out, cost_usd=0 if meta is None else meta.cost_usd,
                               model=None if meta is None else meta.model)
    except Exception:                                                     # noqa: BLE001 - nothing more can be done
        logger.exception("could not record the failure of run=%s", run_id)
    result.decision = None
