"""The GmailClient interface: everything the import needs from a mailbox, and nothing more.

There is deliberately no send, draft, modify, trash or label method: the scope is gmail.readonly and the interface cannot express
anything else. A message is returned in the shape of the Gmail API's users.messages.get (format=full) JSON, so the fake and the real
client feed exactly the same parsing code (`attachments.walk_parts`, `service`). Body data, if present, is never read.

Implementations: `fake.FakeGmailClient` (fixtures; the whole test suite) and `google_client.GoogleGmailClient` (HTTPS, GET only).
"""
from typing import Any, Protocol

RawMessage = dict[str, Any]

GMAIL_CLIENT_METHODS = frozenset({"profile", "search", "message", "attachment"})


class GmailClient(Protocol):
    def profile(self) -> str:
        """The connected account's email address."""

    def search(self, query: str, max_results: int) -> tuple[list[str], int]:
        """Message ids for a Gmail search query (newest first, at most `max_results`) and Gmail's estimate of the total."""

    def message(self, message_id: str) -> RawMessage:
        """One message: id, threadId, internalDate (ms), snippet, payload (headers and the MIME part tree)."""

    def attachment(self, message_id: str, part_id: str, max_bytes: int) -> bytes:
        """The bytes of one attachment part. Raises GmailError('too_large') past `max_bytes` without returning a partial file."""
