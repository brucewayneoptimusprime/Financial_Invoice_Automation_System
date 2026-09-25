"""Drafts for request_info and reject: a vendor email (never sent) or, when the vendor must not be written to, an internal note.

`draft_for` asks the model for the email and accepts it only if it passes the checks (checks.py); otherwise it returns the
deterministic template. Only facts with a vendor-facing category are ever used in a vendor email; vendor status, internal ids, rule
ids, severities and thresholds never appear in one. An internal notification is always a template.
"""
from dataclasses import dataclass, replace
from decimal import Decimal

from app.config import Settings, get_settings
from app.enums import Decision
from app.pipeline.checks import check_draft
from app.pipeline.digest import TrailDigest
from app.pipeline.explain import skip_reason
from app.pipeline.prompts import draft_schema, draft_system
from app.pipeline.requests import request_line
from app.pipeline.roles import call_role
from app.pipeline.templates import SIGNATURE


@dataclass(frozen=True)
class Draft:
    kind: str                                   # "vendor_email" | "notification"
    to: str | None
    subject: str
    body: str
    source: str                                 # "template" | "llm"
    requested: tuple[str, ...] = ()             # one line per thing asked of the vendor / listed as a reason
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    fallback_reason: str | None = None
    attempts: int = 0


def draft_kind(digest: TrailDigest, settings: Settings) -> str:
    """A vendor email, unless a triggered outcome forbids writing to the vendor (a blocked vendor) or nothing is vendor-facing."""
    for f in digest.triggered:
        if f.rule_id and f.outcome_key in settings.no_vendor_email_outcomes.get(f.rule_id, ()):
            return "notification"
    return "vendor_email" if digest.vendor_facing else "notification"


def _greeting(digest: TrailDigest) -> tuple[str, str, str]:
    inv = next(iter(digest.kind("invoice")), None)
    data = inv.data if inv else {}
    name = data.get("vendor_name")
    number, date = data.get("number"), data.get("date")
    ref = "your invoice" + (f" {number}" if number else "") + (f" dated {date}" if date else "")
    return (f"Dear {name}," if name else "Dear Sir or Madam,"), ref, (number or "")


def template_draft(digest: TrailDigest, settings: Settings, fallback_reason: str | None = None, attempts: int = 0) -> Draft:
    kind = draft_kind(digest, settings)
    greeting, ref, number = _greeting(digest)
    if kind == "notification":
        requested = tuple(f.text for f in digest.triggered)
        subject = f"Invoice {number or '(number unknown)'}: {digest.decision.value.replace('_', ' ')} - internal note"
        body = ("Internal note (no email was drafted to the vendor).\n\n"
                f"The invoice was {'rejected' if digest.decision is Decision.REJECT else 'held'} automatically. Reasons:\n"
                + "\n".join(f"- {r}" for r in requested) + "\n\nNothing was sent.")
        return Draft("notification", None, subject, body, "template", requested, fallback_reason=fallback_reason, attempts=attempts)
    requested = tuple(request_line(f) for f in digest.vendor_facing)
    bullets = "\n".join(f"- {r}" for r in requested)
    if digest.decision is Decision.REJECT:
        subject = f"Invoice {number}: unable to process" if number else "Your invoice: unable to process"
        body = (f"{greeting}\n\nThank you for sending {ref}. We are unable to process it, for the following reason(s):\n\n{bullets}\n\n"
                f"If you believe this is a mistake, please reply with the details.\n\n{SIGNATURE}")
    else:
        subject = f"Invoice {number}: information needed" if number else "Your invoice: information needed"
        body = (f"{greeting}\n\nThank you for sending {ref}. Before we can process it we need the following:\n\n{bullets}\n\n"
                f"Please reply with the information or a corrected invoice.\n\n{SIGNATURE}")
    return Draft("vendor_email", None, subject, body, "template", requested, fallback_reason=fallback_reason, attempts=attempts)


def draft_for(digest: TrailDigest, settings: Settings | None = None, ctx=None, client=None, run_id: str | None = None) -> Draft:
    """The model's vendor email if it passes the checks, else the deterministic template. The model sees only the vendor-safe
    request lines and the invoice as extracted, never internal facts."""
    settings = settings or get_settings()
    if draft_kind(digest, settings) == "notification":
        return template_draft(digest, settings, fallback_reason="internal note: no model is used")
    reason = skip_reason(ctx, client, settings, settings.draft_with_llm)
    if reason is not None:
        return template_draft(digest, settings, fallback_reason=reason)
    inv = next(iter(digest.kind("invoice")), None)
    payload = {"decision": digest.decision.value, "invoice": {k: v for k, v in (inv.data if inv else {}).items() if v},
               "requests": [{"id": f.id, "category": f.category, "text": request_line(f)} for f in digest.vendor_facing]}
    res = call_role(client, system=draft_system(settings), payload=payload, schema=draft_schema(),
                    model=settings.drafter_model or settings.model_name, max_output_tokens=settings.drafter_max_output_tokens,
                    run_id=run_id, purpose="draft", check=lambda reply: check_draft(reply, digest, settings))
    usage = dict(model=res.model, tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, attempts=res.attempts)
    if res.reply is None:
        return replace(template_draft(digest, settings, fallback_reason=res.fallback_reason), **usage)
    return Draft("vendor_email", None, res.reply["subject"].strip(), res.reply["body"].strip(), "llm",
                 tuple(request_line(f) for f in digest.vendor_facing), **usage)
