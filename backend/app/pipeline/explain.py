"""The explanation: plain language, written FROM the digest (the audit trail), never from a model's own memory or judgment.

Stage 2 provides the deterministic template explanation, which is also the fallback when a model is unavailable or its reply
fails the claim check (Stage 3). The decision is fixed before this runs: nothing here can change it.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from app.pipeline.digest import TrailDigest
from app.pipeline.templates import DECISION_MEANING, NEXT_STEP


@dataclass(frozen=True)
class Explanation:
    text: str                                   # the full explanation, as shown to a person
    one_line: str                               # one line for lists and status pages
    reasons: tuple[tuple[str, tuple[str, ...]], ...]      # (sentence, cited fact ids)
    next_step: str
    source: str                                 # "template" | "llm"
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    fallback_reason: str | None = None          # why the template was used instead of a model reply
    attempts: int = 0

    @property
    def cited(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(i for _, ids in self.reasons for i in ids))


def _one_line(digest: TrailDigest) -> str:
    triggered = digest.triggered
    if not triggered:
        return f"{digest.decision.value}: all checks passed"
    shorts = list(dict.fromkeys(f.short for f in triggered))
    return f"{digest.decision.value}: " + "; ".join(shorts[:3]) + (f" (+{len(shorts) - 3} more)" if len(shorts) > 3 else "")


def template_explanation(digest: TrailDigest, fallback_reason: str | None = None, attempts: int = 0) -> Explanation:
    decision = digest.decision
    reasons: list[tuple[str, tuple[str, ...]]] = []
    for f in digest.triggered:
        reasons.append((f.text, (f.id,)))
    if not reasons:
        summary = digest.kind("summary")
        if summary:
            reasons.append((summary[0].text, (summary[0].id,)))
    header = f"Decision: {decision.value.upper().replace('_', ' ')}. {DECISION_MEANING[decision]}"
    lines = [header]
    if digest.triggered:
        lines.append("Why:")
        lines.extend(f"- {sentence} [{', '.join(ids)}]" for sentence, ids in reasons)
    elif reasons:
        lines.append(f"Why: {reasons[0][0]} [{', '.join(reasons[0][1])}]")
    lines.append(f"Next step: {NEXT_STEP[decision]}")
    return Explanation(text="\n".join(lines), one_line=_one_line(digest), reasons=tuple(reasons), next_step=NEXT_STEP[decision],
                       source="template", fallback_reason=fallback_reason, attempts=attempts)


def explain(digest: TrailDigest, **_ignored) -> Explanation:
    """Stage 2: the template. (Stage 3 adds the model call in front of this, with this as the fallback.)"""
    return template_explanation(digest)
