"""The Relevance labeller (SPEC section 11 item 89): advisory labels for the attachments a search already returned.

ONE model call per search reads ONLY metadata of the importable (eligible) attachments, in Gmail order, under refs a1..aN that code
assigns (the model never sees Gmail ids): sender, subject, snippet, filename (each cleaned and wrapped in <email_data>...</email_data>,
declared to be data), MIME type and size. It never sees an attachment's contents. Code checks the reply: refs we sent, each once,
a label from the enum, a non-empty reason. More than 20% missing / unknown / duplicate / malformed fails the reply (one repair
retry, then no labels); within a passing reply, bad entries are dropped and a missing ref simply has no label.

Never sent to the model: attachments of emails carrying the deterministic injection flag (they get `unsure` by rule) and attachments
beyond `gmail_labels_max_items` (no label). No eligible attachment at all means no call and no cost. Labels change nothing else:
not the candidate set, not the order, not eligibility, not what can be ticked or imported.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.gmail.models import clean_text
from app.gmail.prompts import LABELS, LABELS_SYSTEM, labels_schema
from app.llm.types import LLMClient
from app.pipeline.roles import call_role

REASON_CAP = 120
BAD_SHARE = 0.20
FLAGGED_REASON = "The email contains text addressed to an AI; check it yourself."
_TAG = re.compile(r"<\s*/?\s*email_data\s*>", re.IGNORECASE)


@dataclass
class LabelResult:
    labels: dict[tuple[str, str], tuple[str, str, str]] = field(default_factory=dict)   # key -> (label, reason, source model|rule)
    sent: int = 0
    skipped: str | None = None                  # no_eligible_attachments: no call was made
    fallback: str | None = None                 # unavailable | invalid_output: no labels
    fallback_detail: str | None = None          # for the log only
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    attempts: int = 0


def wrap(text: str | None) -> str:
    """Email text as delimited data; a delimiter inside the text is neutralised so it cannot close the block."""
    return f"<email_data>{_TAG.sub('[email_data]', text or '')}</email_data>"


def build_items(session, settings: Settings) -> tuple[list[dict], dict[str, tuple[str, str]], list[tuple[str, str]]]:
    """(prompt items, ref -> (message_id, part_id), flagged keys). Gmail order; at most gmail_labels_max_items items."""
    items, refs, flagged = [], {}, []
    for key, cand in session.candidates.items():
        meta = session.messages.get(cand.message_id, {})
        if meta.get("flagged"):
            flagged.append(key)
            continue
        if len(items) >= settings.gmail_labels_max_items:
            continue
        ref = f"a{len(items) + 1}"
        refs[ref] = key
        items.append({"ref": ref, "sender": wrap(cand.sender), "subject": wrap(meta.get("subject")), "snippet": wrap(meta.get("snippet")),
                      "filename": wrap(cand.filename), "mime_type": cand.mime_type, "size_kb": max(1, round(cand.size / 1024))})
    return items, refs, flagged


def _valid(entry: Any, refs: dict) -> bool:
    return (isinstance(entry, dict) and set(entry) == {"ref", "label", "reason"} and entry["ref"] in refs and entry["label"] in LABELS
            and isinstance(entry["reason"], str) and bool(entry["reason"].strip()))


def check_labels(reply: Any, refs: dict) -> list[str]:
    if not isinstance(reply, dict) or not isinstance(reply.get("labels"), list):
        return ["the reply must be a JSON object with a labels array"]
    seen, bad = set(), 0
    for entry in reply["labels"]:
        if not _valid(entry, refs) or entry["ref"] in seen:
            bad += 1
            continue
        seen.add(entry["ref"])
    missing = len(refs) - len(seen)
    if bad + missing > BAD_SHARE * len(refs):
        return [f"{bad} invalid or duplicate entries and {missing} missing refs out of {len(refs)}: label every ref "
                f"({', '.join(refs)}) exactly once with likely_invoice, unlikely or unsure and a short reason"]
    return []


def label(session, *, client: LLMClient, settings: Settings) -> LabelResult:
    if not session.candidates:
        return LabelResult(skipped="no_eligible_attachments")
    items, refs, flagged = build_items(session, settings)
    rule_labels = {key: ("unsure", FLAGGED_REASON, "rule") for key in flagged}
    if not items:
        return LabelResult(labels=rule_labels)                            # everything was flagged: nothing to ask, no cost
    res = call_role(client, system=LABELS_SYSTEM,
                    payload={"intent": session.intent, "intent_kind": session.intent_kind, "attachments": items},
                    schema=labels_schema(), model=settings.model_name, max_output_tokens=settings.gmail_labels_max_output_tokens,
                    run_id=f"gmail-search-{session.search_id}", purpose="gmail_labels", check=lambda r: check_labels(r, refs))
    out = LabelResult(sent=len(items), tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, attempts=res.attempts)
    if res.reply is None:
        out.fallback = "invalid_output" if res.problems else "unavailable"
        out.fallback_detail = res.fallback_reason
        return out
    seen: set[str] = set()
    labels = dict(rule_labels)
    for entry in res.reply["labels"]:
        if _valid(entry, refs) and entry["ref"] not in seen:
            seen.add(entry["ref"])
            labels[refs[entry["ref"]]] = (entry["label"], clean_text(entry["reason"], REASON_CAP) or "", "model")
    out.labels = labels
    return out
