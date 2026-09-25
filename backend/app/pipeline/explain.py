"""The explanation: plain language, written FROM the digest (the audit trail), never from a model's own memory or judgment.

`explain` asks the model for an explanation of the digest and accepts it only if it passes the claim checks (checks.py); otherwise,
or when no model is used, it returns the deterministic template. The decision is fixed before this runs: nothing here can change it.
"""
from dataclasses import dataclass, field, replace
from decimal import Decimal

from app.config import Settings, get_settings
from app.pipeline.checks import check_explanation
from app.pipeline.digest import TrailDigest
from app.pipeline.prompts import explain_schema, explain_system
from app.pipeline.roles import call_role
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


def skip_reason(ctx, client, settings: Settings, enabled: bool) -> str | None:
    """Why a model is NOT used for this run (None = use it). Reader instructions in the document keep a model away from it."""
    if client is None:
        return "no model client"
    if not enabled:
        return "disabled in configuration"
    meta = getattr(ctx, "extraction_meta", None)
    if meta is not None and meta.injection_suspected:
        return "the document appears to contain instructions addressed to an AI reader; no model is used on this run"
    return None


def explain(digest: TrailDigest, ctx=None, client=None, settings: Settings | None = None, run_id: str | None = None) -> Explanation:
    """The model's explanation if it passes the claim checks, else the deterministic template (with the reason recorded)."""
    settings = settings or get_settings()
    reason = skip_reason(ctx, client, settings, settings.explain_with_llm)
    if reason is not None:
        return template_explanation(digest, fallback_reason=reason)
    payload = {**digest.prompt_dict(), "next_step_hint": NEXT_STEP[digest.decision]}
    res = call_role(client, system=explain_system(settings), payload=payload, schema=explain_schema(),
                    model=settings.explainer_model or settings.model_name, max_output_tokens=settings.explainer_max_output_tokens,
                    run_id=run_id, purpose="explain", check=lambda reply: check_explanation(reply, digest, settings))
    usage = dict(model=res.model, tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, attempts=res.attempts)
    if res.reply is None:
        return replace(template_explanation(digest, fallback_reason=res.fallback_reason), **usage)
    reasons = tuple((r["text"].strip(), tuple(r["facts"])) for r in res.reply["reasons"])
    next_step = res.reply["next_step"].strip()
    lines = [res.reply["summary"].strip()]
    if reasons:
        lines.append("Why:")
        lines.extend(f"- {text} [{', '.join(ids)}]" for text, ids in reasons)
    lines.append(f"Next step: {next_step}")
    return Explanation(text="\n".join(lines), one_line=_one_line(digest), reasons=reasons, next_step=next_step, source="llm", **usage)
