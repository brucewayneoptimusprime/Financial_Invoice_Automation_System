"""Stage timing events and their small, whitelisted summaries (M4: what the live run view shows on each stage card).

`stage_started` is written (and committed) before a stage runs, `stage_completed` after it, with the duration and a summary built
here from values the stage already produced. The summary is a whitelist of codes, counts, ids and system-side names: never page
text, image bytes, invoice text, a key or free-text model output. The full detail stays in the stage's own events.
"""
from decimal import Decimal
from typing import Any

from app.enums import Outcome
from app.models.audit import AuditEvent
from app.models.run import RunContext, StageResult

PIPELINE_STAGE = "pipeline"
STAGE_STARTED = "stage_started"
STAGE_COMPLETED = "stage_completed"
STAGES = ("ingest", "extract", "match", "validate", "decide", "explain", "act")

HEADER_FIELDS = ("vendor_name", "vendor_tax_id", "vendor_address", "document_type", "invoice_number", "invoice_date", "currency",
                 "po_reference", "subtotal", "tax", "total")
_TEXT_CAP = 200


def _cap(text: str | None) -> str | None:
    return None if text is None else text[:_TEXT_CAP]


def started_event(stage: str) -> AuditEvent:
    return AuditEvent(stage=PIPELINE_STAGE, event_type=STAGE_STARTED, outcome=Outcome.INFO, message=f"Stage {stage} started.",
                      detail={"stage": stage})


def completed_event(stage: str, status: str, duration_ms: int, summary: dict[str, Any]) -> AuditEvent:
    outcome = {"ok": Outcome.PASS, "flagged": Outcome.FLAG, "failed": Outcome.FAIL}.get(status, Outcome.INFO)
    return AuditEvent(stage=PIPELINE_STAGE, event_type=STAGE_COMPLETED, outcome=outcome,
                      message=f"Stage {stage} {status} in {duration_ms} ms.",
                      detail={"stage": stage, "status": status, "duration_ms": max(int(duration_ms), 0), "summary": summary})


def summarize_ingest(ctx: RunContext) -> dict[str, Any]:
    info = ctx.ingest
    if info is None:
        return {}
    return {"media_type": info.media_type, "size_bytes": info.size_bytes, "pages_total": info.pages_total,
            "pages_processed": info.pages_processed, "truncated": info.truncated, "text_layer": info.text_layer.reason,
            "failure_kind": info.failure_kind, "failure_code": info.failure_code}


def summarize_extract(ctx: RunContext) -> dict[str, Any]:
    meta, ex = ctx.extraction_meta, ctx.extracted
    found = [] if ex is None else [name for name in HEADER_FIELDS if getattr(ex, name).value is not None]
    out: dict[str, Any] = {"fields_found": len(found), "fields_total": len(HEADER_FIELDS),
                           "line_items": 0 if ex is None else len(ex.line_items),
                           "adjustments": 0 if ex is None else len(ex.adjustments)}
    if meta is not None:
        out.update({"path": meta.path, "model": meta.model, "prompt_version": meta.prompt_version, "degraded": meta.degraded,
                    "failure_kind": meta.failure_kind, "failure_code": meta.failure_code, "attempts": meta.attempts,
                    "grounding": dict(meta.grounding), "injection_suspected": meta.injection_suspected,
                    "tokens_in": meta.tokens_in, "tokens_out": meta.tokens_out, "cost_usd": meta.cost_usd})
    return out


def summarize_match(ctx: RunContext) -> dict[str, Any]:
    vm = ctx.matched_vendor
    vendor = None
    if vm is not None and vm.vendor_id is not None and ctx.facts is not None:
        vendor = next((v.name for v in ctx.facts.vendors if v.id == vm.vendor_id), None)   # the vendor RECORD's name, not invoice text
    top = ctx.candidates[0] if ctx.candidates else None
    return {"match_status": None if ctx.match_status is None else ctx.match_status.value,
            "vendor_id": None if vm is None else vm.vendor_id, "vendor": vendor, "vendor_method": None if vm is None else vm.method,
            "vendor_score": None if vm is None else vm.score, "vendor_ambiguous": False if vm is None else vm.ambiguous,
            "matched_po": None if ctx.matched_po is None else ctx.matched_po.po_number,
            "top_candidate": None if top is None else top.po_number, "top_score": None if top is None else top.score,
            "candidate_count": len(ctx.candidates),
            "line_mode": None if ctx.line_matches is None else ctx.line_matches.mode,
            "line_counts": {} if ctx.line_matches is None else
            {s: sum(1 for ln in ctx.line_matches.lines if ln.status.value == s) for s in ("matched", "ambiguous", "no_match", "not_evaluable")}}


def summarize_validate(ctx: RunContext) -> dict[str, Any]:
    counts = {o.value: 0 for o in Outcome}
    for r in ctx.rule_results:
        counts[r.outcome.value] += 1
    return {"results": len(ctx.rule_results), "counts": counts,
            "triggered": [r.rule_id for r in ctx.rule_results if r.outcome in (Outcome.FLAG, Outcome.FAIL)]}


def summarize_decide(stage: StageResult) -> dict[str, Any]:
    return {"decision": stage.outputs.get("decision"), "final_severity": stage.outputs.get("final_severity")}


def summarize_explain(source: str, model: str | None, fallback_reason: str | None, attempts: int, cost_usd: Decimal) -> dict[str, Any]:
    return {"source": source, "model": model, "fallback_reason": _cap(fallback_reason), "attempts": attempts, "cost_usd": cost_usd}


def summarize_act(decision: str, tables: dict[str, int], downgraded: bool, draft_kind: str | None) -> dict[str, Any]:
    return {"decision": decision, "rows_written": tables, "downgraded_to_review": downgraded, "draft_kind": draft_kind}
