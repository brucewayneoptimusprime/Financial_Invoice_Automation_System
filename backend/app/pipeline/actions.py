"""The act stage's decision table, as pure code: which rows a decision writes. Persistence happens in runner.py through persist.py.

  approve       ledger commit (invoice total) on the matched PO, PO status update, invoice status approved, ready for payment
  review        review_queue item (reason from the triggered checks); NO draft
  request_info  vendor email draft (or an internal note if nothing is vendor-facing); invoice status awaiting_info
  reject        vendor email draft with the reason (an internal note for a blocked vendor); invoice status rejected
"""
from dataclasses import dataclass

from app.config import Settings
from app.enums import Decision
from app.models.run import RunContext
from app.pipeline.digest import TrailDigest


@dataclass(frozen=True)
class ActionPlan:
    decision: Decision
    commit_ledger: bool = False
    review_reason: str | None = None
    draft: bool = False                       # a vendor email / notification is to be drafted
    ready_for_payment: bool = False


def review_reason(digest: TrailDigest, settings: Settings) -> str:
    """One deterministic line from the triggered checks (rule id, outcome, the engine's message). Evidence = the run's audit events."""
    parts = []
    for f in digest.triggered:
        parts.append(f"{f.rule_id or f.kind} ({f.outcome_key}): {f.text}" if f.rule_id and f.kind == "rule" else f.text)
    text = "Review: " + " | ".join(parts) if parts else "Review: escalated for a person to confirm."
    limit = settings.review_reason_max_chars
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plan_actions(ctx: RunContext, digest: TrailDigest, settings: Settings) -> ActionPlan:
    decision = digest.decision
    if decision is Decision.APPROVE:
        # an approve always has a matched PO and a total (the engine floor guarantees it); anything else is not committed
        can_commit = ctx.matched_po is not None and ctx.extracted is not None and ctx.extracted.total.value is not None
        return ActionPlan(decision, commit_ledger=can_commit, ready_for_payment=True)
    if decision is Decision.REVIEW:
        return ActionPlan(decision, review_reason=review_reason(digest, settings))
    return ActionPlan(decision, draft=True)
