"""The Query translator (SPEC section 11 item 88): one sentence -> one Gmail query, checked by code.

The model sees ONLY the sentence, today's date and the timezone (no email content, no vendor list). Its reply must pass:
  1. the existing allowlist validator (`query.validate_query`), with the configured limits;
  2. the company-name rule, enforced here: every from: / to: value must be an email address or a domain (contains @ or .);
     a bare name is a problem, so "Meridian" can only appear as a plain keyword.
A failing reply is repaired ONCE (our problems listed), then given up: the caller shows the model's query (if any) and the problems
under the editable manual box. An empty query is the model saying the sentence cannot be a Gmail search (its notes say why).
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.gmail.errors import GmailError
from app.gmail.models import clean_text
from app.gmail.prompts import QUERY_SYSTEM, query_schema
from app.gmail.query import validate_query
from app.llm.types import LLMClient
from app.pipeline.roles import call_role

NOTES_CAP = 300


@dataclass
class TranslateResult:
    query: str | None                       # the accepted, validated query (None = no usable query)
    proposed: str | None = None             # the model's last query when it was refused (shown to the user to edit)
    notes: str = ""
    problems: list[str] = field(default_factory=list)
    fallback_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    attempts: int = 0


def check_translation(reply: Any, settings: Settings) -> list[str]:
    """[] when the reply is usable (an empty query is usable: it means "cannot be expressed")."""
    if not isinstance(reply, dict) or not isinstance(reply.get("query"), str) or not isinstance(reply.get("notes"), str):
        return ["the reply must be a JSON object with the string fields query and notes"]
    text = reply["query"].strip()
    if not text:
        return []
    try:
        q = validate_query(text, max_chars=settings.gmail_query_max_chars, max_terms=settings.gmail_query_max_terms)
    except GmailError as exc:
        return list(exc.problems) or [exc.message]
    problems = []
    for t in q.terms:
        if t.kind == "op" and t.op in ("from", "to") and "@" not in t.value and "." not in t.value:
            problems.append(f"'{t.render()}': company or person names must be plain keywords (write {t.value} without {t.op}:); "
                            f"use {t.op}: only for an email address or a domain")
    return problems


def translate(sentence: str, *, client: LLMClient, settings: Settings, run_key: str, today: date) -> TranslateResult:
    last: dict[str, Any] = {}

    def check(reply: Any) -> list[str]:
        last["reply"] = reply
        return check_translation(reply, settings)

    res = call_role(client, system=QUERY_SYSTEM, payload={"sentence": sentence, "today": today.isoformat(), "timezone": "UTC"},
                    schema=query_schema(), model=settings.model_name, max_output_tokens=settings.gmail_translate_max_output_tokens,
                    run_id=run_key, purpose="gmail_query", check=check)
    usage = dict(tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, attempts=res.attempts)
    if res.reply is not None:
        notes = clean_text(res.reply["notes"], NOTES_CAP) or ""
        query = res.reply["query"].strip()
        if not query:
            return TranslateResult(query=None, notes=notes, fallback_reason="the sentence cannot be expressed as a Gmail search", **usage)
        return TranslateResult(query=query, notes=notes, **usage)
    reply = last.get("reply")
    proposed = reply.get("query").strip() if isinstance(reply, dict) and isinstance(reply.get("query"), str) else None
    notes = clean_text(reply.get("notes"), NOTES_CAP) if isinstance(reply, dict) and isinstance(reply.get("notes"), str) else ""
    return TranslateResult(query=None, proposed=proposed or None, notes=notes or "", problems=list(res.problems),
                           fallback_reason=res.fallback_reason, **usage)
