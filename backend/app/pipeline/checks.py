"""Deterministic claim checks on what the explainer and the drafter return. A reply that fails any check is never used as is.

The checks cannot prove a text is true; they make it hard for a text to say anything the digest does not: every fact id must
exist and every triggered fact must be cited; numbers, identifiers and quoted strings must come from the digest; the decision
stated must be the real one; no contradicting or promising phrases; length limits. Anything that fails is repaired once and then
replaced by the deterministic template.
"""
import re
from typing import Any

from app.config import Settings
from app.enums import Decision
from app.pipeline.digest import Fact, TrailDigest, numbers_in
from app.pipeline.requests import request_line
from app.pipeline.templates import FIELD_LABELS

_FACT_ID = re.compile(r"\bF\d+\b")
_TOKEN = re.compile(r"\b(?=[A-Za-z0-9\-/_.]*[A-Za-z])(?=[A-Za-z0-9\-/_.]*\d)[A-Za-z0-9][A-Za-z0-9\-/_.]*[A-Za-z0-9]\b|\br_[a-z_]+\b")
_QUOTED = re.compile(r"'([^']{2,})'|\"([^\"]{2,})\"|‘([^’]{2,})’|“([^”]{2,})”")
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
_CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|https?://|www\.|\+?\d[\d ()-]{8,}\d")

# statements that would contradict the real decision (checked case-insensitively). Anchored on "invoice"/"it" so that a fact about
# something else ("the vendor is approved") is not mistaken for a claim about the decision.
_NOT_APPROVED = ["invoice is approved", "invoice was approved", "invoice has been approved", "invoice should be approved", "it is approved",
                 "it was approved", "it has been approved", "it should be approved", "ready for payment", "can be paid", "should be paid",
                 "recommend approv", "approve this", "safe to pay"]
_NOT_REJECTED = ["invoice is rejected", "invoice was rejected", "invoice has been rejected", "invoice should be rejected", "it is rejected",
                 "it was rejected", "it should be rejected", "recommend reject", "reject this"]
CONTRADICTIONS = {
    Decision.APPROVE: ["cannot be paid", "not ready for payment", "should not be paid", "needs a person", "must be reviewed", *_NOT_REJECTED],
    Decision.REVIEW: [*_NOT_APPROVED, *_NOT_REJECTED],
    Decision.REQUEST_INFO: [*_NOT_APPROVED, *_NOT_REJECTED],
    Decision.REJECT: [*_NOT_APPROVED, "needs a person's review"],
}
DECISION_WORD = {Decision.APPROVE: r"approv", Decision.REVIEW: r"review", Decision.REQUEST_INFO: r"request|more information|ask|missing|unclear",
                 Decision.REJECT: r"reject|cannot be processed|unable to process"}
FORBIDDEN_IN_EMAIL = [r"\bapprov\w*", r"will be paid", r"payment will", r"we will pay", r"within \d+ (?:days|business days|hours)", r"guarantee",
                      r"\br_[a-z_]+\b", r"\bseverity\b", r"\bengine\b", r"\bfloor\b", r"\bthreshold\b", r"\bscore\b", r"\bblocked\b", r"\bAI\b",
                      r"\bmodel\b", r"\bautomat\w*", r"\brule\b", r"new vendor", r"unknown vendor", r"\bpenalt\w*", r"\bwill be rejected\b"]
COVERAGE_WORDS = {
    "po_reference": r"purchase order|\bPO\b|P\.O\.", "arithmetic": r"add up|total|amount|calculat|correct", "currency": r"currency",
    "document_type": r"invoice", "duplicate": r"duplicate|already|received|previous", "unreadable_file": r"password|blank|open|read|file|resend|send it again"}


def _sentences(*texts: str) -> int:
    return sum(len(_SENTENCE_END.findall(t.strip() if t.strip().endswith((".", "!", "?")) else t.strip() + ".")) for t in texts if t.strip())


def _corpus(facts: list[Fact] | tuple[Fact, ...], extra: str = "") -> str:
    return (" ".join(f.text for f in facts) + " " + extra).lower()


def _unsupported(text: str, allowed_numbers: set[str], corpus: str, label: str) -> list[str]:
    problems: list[str] = []
    stripped = _FACT_ID.sub(" ", text)
    bad_numbers = sorted(n for n in numbers_in(stripped) if n not in allowed_numbers)
    if bad_numbers:
        problems.append(f"{label} contains number(s) not in the facts: {', '.join(bad_numbers[:4])}")
    bad_tokens = sorted({t for t in _TOKEN.findall(stripped) if t.lower() not in corpus})
    if bad_tokens:
        problems.append(f"{label} contains identifier(s) not in the facts: {', '.join(bad_tokens[:4])}")
    for m in _QUOTED.finditer(stripped):
        quoted = next(g for g in m.groups() if g)
        if quoted.lower() not in corpus:
            problems.append(f"{label} quotes text that is not in the facts")
            break
    return problems


def check_explanation(reply: Any, digest: TrailDigest, settings: Settings) -> list[str]:
    """Problems with an explainer reply (empty list = acceptable)."""
    if not isinstance(reply, dict):
        return ["the reply is not a JSON object"]
    summary, reasons, next_step = reply.get("summary"), reply.get("reasons"), reply.get("next_step")
    if not (isinstance(summary, str) and summary.strip()) or not isinstance(reasons, list) or not (isinstance(next_step, str) and next_step.strip()):
        return ["summary, reasons and next_step are required (summary and next_step non-empty text, reasons a list)"]
    problems: list[str] = []
    cited: set[str] = set()
    texts = [summary, next_step]
    all_numbers = digest.allowed_numbers()
    corpus = _corpus(digest.facts)
    for i, item in enumerate(reasons, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip() or not isinstance(item.get("facts"), list) \
                or not item["facts"] or not all(isinstance(x, str) for x in item["facts"]):
            problems.append(f"reason {i} needs text and a non-empty list of fact ids")
            continue
        unknown = [x for x in item["facts"] if digest.fact(x) is None]
        if unknown:
            problems.append(f"reason {i} cites unknown fact id(s): {', '.join(unknown[:3])}")
            continue
        cited.update(item["facts"])
        texts.append(item["text"])
        problems += _unsupported(item["text"], digest.allowed_numbers(item["facts"]), _corpus([digest.fact(x) for x in item["facts"]]), f"reason {i}")
    missing = [f.id for f in digest.triggered if f.id not in cited]
    if missing:
        problems.append(f"triggered fact(s) not cited: {', '.join(missing)}")
    problems += _unsupported(summary, all_numbers, corpus, "summary") + _unsupported(next_step, all_numbers, corpus, "next_step")

    whole = " ".join(texts).lower()
    if not re.search(DECISION_WORD[digest.decision], summary.lower()):
        problems.append(f"the summary does not state the decision ({digest.decision.value})")
    bad = [p for p in CONTRADICTIONS[digest.decision] if p in whole]
    if bad:
        problems.append(f"the text contradicts the decision ({digest.decision.value}): '{bad[0]}'")
    if _sentences(*texts) > settings.explanation_max_sentences:
        problems.append(f"more than {settings.explanation_max_sentences} sentences")
    if len(whole) > 2000:
        problems.append("too long")
    return problems


def request_facts(digest: TrailDigest) -> tuple[Fact, ...]:
    """The facts a vendor email may address (vendor-facing, triggered)."""
    return digest.vendor_facing


def check_draft(reply: Any, digest: TrailDigest, settings: Settings) -> list[str]:
    """Problems with a drafter reply for a vendor email (empty list = acceptable)."""
    if not isinstance(reply, dict):
        return ["the reply is not a JSON object"]
    subject, body, covers = reply.get("subject"), reply.get("body"), reply.get("covers")
    if not (isinstance(subject, str) and subject.strip()) or not (isinstance(body, str) and body.strip()) or not isinstance(covers, list) \
            or not all(isinstance(x, str) for x in covers):
        return ["subject, body and covers are required (subject and body non-empty text, covers a list of request ids)"]
    facts = request_facts(digest)
    invoice = next(iter(digest.kind("invoice")), None)
    lines = {f.id: request_line(f) for f in facts}
    allowed_text = " ".join(lines.values()) + " " + (invoice.text if invoice else "") + " accounts payable kind regards"
    allowed_numbers = numbers_in(allowed_text)
    corpus = allowed_text.lower()
    problems: list[str] = []
    missing = [f.id for f in facts if f.id not in covers]
    if missing:
        problems.append(f"request(s) not covered: {', '.join(missing)}")
    extra = [c for c in covers if c not in lines]
    if extra:
        problems.append(f"covers unknown request id(s): {', '.join(extra[:3])}")
    low = body.lower()
    for f in facts:
        if f.category in ("missing_fields", "unclear_fields"):
            for item in f.items:
                label = FIELD_LABELS.get(item, item.replace("_", " "))
                variants = {item.replace("_", " "), label.removeprefix("the ").removeprefix("the seller's ")}
                if not any(v.lower() in low for v in variants):
                    problems.append(f"the body does not mention '{item.replace('_', ' ')}'")
        elif f.category in COVERAGE_WORDS and not re.search(COVERAGE_WORDS[f.category], body, re.IGNORECASE):
            problems.append(f"the body does not address the {f.category.replace('_', ' ')} request")
    number = invoice.data.get("number") if invoice else None
    if number and number.lower() not in low and number.lower() not in subject.lower():
        problems.append("the invoice number is not mentioned")
    problems += _unsupported(body + " " + subject, allowed_numbers, corpus, "the email")
    for pattern in FORBIDDEN_IN_EMAIL:
        if re.search(pattern, body + " " + subject, re.IGNORECASE):
            problems.append("the email contains wording that must not be sent to a vendor")
            break
    for p in CONTRADICTIONS[digest.decision]:
        if p in low:
            problems.append(f"the email contradicts the decision: '{p}'")
            break
    if _CONTACT.search(body):
        problems.append("the email contains a contact detail (address, link or phone number) that is not in the facts")
    if "accounts payable" not in low:
        problems.append("the email must be signed 'Accounts Payable'")
    if len(body.split()) > settings.draft_max_words:
        problems.append(f"more than {settings.draft_max_words} words")
    return problems
