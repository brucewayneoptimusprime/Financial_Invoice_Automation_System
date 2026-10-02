"""What the Gmail endpoints return. Every text that came from an email is cleaned (control characters removed, whitespace collapsed,
capped) and is DATA: the UI shows it as plain text, and no model ever receives it."""
import html
import re

from pydantic import BaseModel, ConfigDict

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")
_WS = re.compile(r"\s+")


def clean_text(text: str | None, cap: int) -> str | None:
    """Control and bidi-override characters removed, whitespace collapsed, at most `cap` characters. None stays None."""
    if text is None:
        return None
    flat = _WS.sub(" ", _CONTROL.sub("", html.unescape(str(text)))).strip()
    return flat[:cap] if flat else None


class AttachmentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_id: str
    filename: str
    mime_type: str
    size_bytes: int
    inline: bool
    eligible: bool
    reason_code: str | None = None
    reason: str | None = None
    imported_run_id: str | None = None          # already imported from this message (same part and size): its run


class MessageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    sender: str | None
    subject: str | None
    date: str | None                            # UTC ISO-8601 from Gmail's internalDate
    snippet: str | None
    attachments: list[AttachmentInfo]
    more_attachments: int = 0                   # named parts beyond gmail_max_attachments_per_message, not listed
    reader_instructions: bool = False           # sender / subject / snippet / a filename matched an injection pattern
    reader_instruction_fields: list[str] = []


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_id: str
    backend: str
    account_email: str
    query_sent: str
    added_terms: list[str]
    result_estimate: int
    truncated: bool
    messages: list[MessageSummary]
    translation: dict | None = None             # {sentence, query, notes} when the search started from a sentence
    cost: dict = {}                              # {translate_usd, tokens_in, tokens_out}
