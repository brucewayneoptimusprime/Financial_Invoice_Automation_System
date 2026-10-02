"""FakeGmailClient: a mailbox made of fixtures, for the whole test suite and the labelled `GMAIL_BACKEND=fake` demo mode.

It never touches the network. Each fixture message is turned into the Gmail API's message shape, so the import code cannot tell
it from the real client. Fixture format (JSON):

    {"_notice": "FAKE INBOX ...", "account": "...", "now": "2026-10-01T12:00:00Z",
     "messages": [{"id", "from", "subject", "date" (ISO), "snippet", "body_text"?,
                   "attachments": [{"part_id", "filename", "mime_type", "file"? (relative to the repository root) |
                                    "data_base64"?, "size"? (overrides the byte count, e.g. a declared 25 MB file), "inline"?}]}]}

The search understands the subset of Gmail's language the allowlist permits: words and phrases (subject, sender, snippet, body,
filenames), from:/to:/subject:/filename:, has:attachment, after:/before:/newer_than:/older_than: (relative to "now"), '-' and OR.
larger:/smaller: are accepted and ignored. Results are newest first.
"""
import base64
import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR
from app.gmail.client import RawMessage
from app.gmail.errors import GmailError
from app.gmail.query import Term, validate_query

_UNITS = {"d": 1, "m": 30, "y": 365}


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


class FakeGmailClient:
    def __init__(self, inbox: Path | str | dict, *, root: Path = ROOT_DIR):
        data = json.loads(Path(inbox).read_text(encoding="utf-8")) if not isinstance(inbox, dict) else inbox
        if "FAKE" not in str(data.get("_notice", "")):
            raise ValueError("a fake inbox must carry a _notice saying it is FAKE test data")
        self.root = Path(root)
        self.account = str(data.get("account") or "fake-inbox@example.test")
        self.now = datetime.fromisoformat(str(data.get("now", "2026-10-01T12:00:00Z")).replace("Z", "+00:00"))
        self._messages = {m["id"]: m for m in data.get("messages", [])}
        self.queries: list[str] = []
        self.downloads: list[tuple[str, str]] = []
        self.calls: list[str] = []

    # ------------------------------------------------------------------ the GmailClient interface
    def profile(self) -> str:
        self.calls.append("profile")
        return self.account

    def search(self, query: str, max_results: int) -> tuple[list[str], int]:
        self.calls.append("search")
        self.queries.append(query)
        q = validate_query(query, max_chars=10_000, max_terms=1_000)             # the fake trusts nothing either
        hits = [m for m in self._messages.values() if self._matches(m, list(q.terms))]
        hits.sort(key=lambda m: _ms(m["date"]), reverse=True)
        return [m["id"] for m in hits[:max_results]], len(hits)

    def message(self, message_id: str) -> RawMessage:
        self.calls.append("message")
        m = self._messages.get(message_id)
        if m is None:
            raise GmailError("not_found", "That email is no longer in the mailbox.")
        return copy.deepcopy(self._gmail_shape(m))

    def attachment(self, message_id: str, part_id: str, max_bytes: int) -> bytes:
        self.calls.append("attachment")
        m = self._messages.get(message_id)
        a = next((a for a in (m or {}).get("attachments", []) if str(a.get("part_id")) == part_id), None)
        if a is None:
            raise GmailError("not_found", "That attachment is no longer in the mailbox.")
        if int(a.get("size", 0) or 0) > max_bytes:
            raise GmailError("too_large", "The attachment is larger than the upload limit.")
        data = self._bytes(a)
        if len(data) > max_bytes:
            raise GmailError("too_large", "The attachment is larger than the upload limit.")
        self.downloads.append((message_id, part_id))
        return data

    # ------------------------------------------------------------------ fixture -> Gmail shape
    def _bytes(self, a: dict[str, Any]) -> bytes:
        if "data_base64" in a:
            return base64.b64decode(a["data_base64"])
        if "file" in a:
            return (self.root / a["file"]).read_bytes()
        return b""

    def _size(self, a: dict[str, Any]) -> int:
        if "size" in a:
            return int(a["size"])
        if "file" in a:
            return (self.root / a["file"]).stat().st_size
        return len(self._bytes(a))

    def _gmail_shape(self, m: dict[str, Any]) -> RawMessage:
        headers = [{"name": "From", "value": m.get("from", "")}, {"name": "To", "value": m.get("to", self.account)},
                   {"name": "Subject", "value": m.get("subject", "")}, {"name": "Date", "value": m.get("date", "")}]
        body_text = m.get("body_text", "")
        parts = [{"partId": "0", "mimeType": "text/plain", "filename": "", "headers": [], "body": {"size": len(body_text)}}]
        for a in m.get("attachments", []):
            disposition = "inline" if a.get("inline") else f'attachment; filename="{a.get("filename", "")}"'
            parts.append({"partId": str(a["part_id"]), "mimeType": a.get("mime_type", "application/octet-stream"),
                          "filename": a.get("filename", ""), "headers": [{"name": "Content-Disposition", "value": disposition}],
                          "body": {"size": self._size(a), "attachmentId": f"fake-{m['id']}-{a['part_id']}"}})
        return {"id": m["id"], "threadId": m.get("thread_id", m["id"]), "internalDate": str(_ms(m["date"])),
                "snippet": m.get("snippet", ""),
                "payload": {"partId": "", "mimeType": "multipart/mixed", "filename": "", "headers": headers, "body": {"size": 0},
                            "parts": parts}}

    # ------------------------------------------------------------------ the search subset
    def _matches(self, m: dict[str, Any], terms: list[Term]) -> bool:
        clauses: list[list[Term]] = []
        joining = False
        for t in terms:
            if t.kind == "or":
                joining = True
                continue
            if joining and clauses:
                clauses[-1].append(t)
            else:
                clauses.append([t])
            joining = False
        return all(any(self._term(m, t) for t in clause) for clause in clauses)

    def _term(self, m: dict[str, Any], t: Term) -> bool:
        return self._positive(m, t) != t.negated

    def _positive(self, m: dict[str, Any], t: Term) -> bool:
        names = " ".join(a.get("filename", "") for a in m.get("attachments", [])).lower()
        sent = datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        value = t.value.lower()
        if t.kind in ("word", "phrase"):
            hay = " ".join([m.get("subject", ""), m.get("from", ""), m.get("snippet", ""), m.get("body_text", ""), names]).lower()
            return value in hay
        field = {"from": m.get("from", ""), "to": m.get("to", self.account), "subject": m.get("subject", ""), "filename": names}
        if t.op in field:
            return value in field[t.op].lower()
        if t.op == "has":
            return bool(m.get("attachments"))
        if t.op in ("after", "before"):
            day = datetime.combine(date.fromisoformat(t.value.replace("/", "-")), datetime.min.time(), timezone.utc)
            return sent >= day if t.op == "after" else sent < day
        if t.op in ("newer_than", "older_than"):
            limit = self.now - timedelta(days=int(t.value[:-1]) * _UNITS[t.value[-1]])
            return sent >= limit if t.op == "newer_than" else sent < limit
        return True                                                                 # larger: / smaller: ignored by the fake
