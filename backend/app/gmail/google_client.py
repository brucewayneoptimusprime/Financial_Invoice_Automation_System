"""GoogleGmailClient: the real GmailClient over HTTPS (SPEC section 11 item 87).

Every request is a GET to https://gmail.googleapis.com/gmail/v1/users/me/... (a test enforces it): users.getProfile,
users.messages.list, users.messages.get (format=full; body data, when Gmail includes it, is never read) and
users.messages.attachments.get. The access token is kept only in memory and renewed from the refresh token through
`OAuthFlows.refresh`; a 401 is retried once after a renewal. Google's response bodies never reach a log, an exception message or a
caller: failures become GmailError codes with plain messages.
"""
import base64
import logging
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from app.gmail.client import RawMessage
from app.gmail.errors import GmailError
from app.gmail.oauth import OAuthFlows

logger = logging.getLogger("app.gmail.client")

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_EARLY_RENEWAL_S = 60


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _find_part(payload: dict[str, Any] | None, part_id: str, depth: int = 0) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or depth > 10:
        return None
    if str(payload.get("partId") or "") == part_id:
        return payload
    for child in payload.get("parts") or []:
        found = _find_part(child, part_id, depth + 1)
        if found is not None:
            return found
    return None


class GoogleGmailClient:
    def __init__(self, http: httpx.Client, oauth: OAuthFlows, refresh_token: str, *, access_token: str | None = None,
                 expires_in: int = 0, on_reconnect: Callable[[], None] | None = None, clock: Callable[[], float] = time.monotonic):
        self._http, self._oauth, self._clock = http, oauth, clock
        self._refresh_token = refresh_token
        self._access = access_token
        self._expires_at = clock() + expires_in if access_token else 0.0
        self._on_reconnect = on_reconnect or (lambda: None)

    def __repr__(self) -> str:                                            # never print a token
        return "GoogleGmailClient(<connected>)"

    # ------------------------------------------------------------------ tokens
    def _token(self, force: bool = False) -> str:
        if force or self._access is None or self._clock() >= self._expires_at - _EARLY_RENEWAL_S:
            try:
                self._access, expires_in = self._oauth.refresh(self._refresh_token)
            except GmailError as exc:
                if exc.code == "reconnect":
                    self._on_reconnect()
                raise
            self._expires_at = self._clock() + expires_in
        return self._access

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        url = f"{GMAIL_BASE}{path}"
        for attempt in (0, 1):
            try:
                r = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._token(force=attempt == 1)}"})
            except httpx.HTTPError as exc:
                logger.warning("gmail request failed: %s", type(exc).__name__)
                raise GmailError("unavailable", "Gmail could not be reached just now. Try again.") from None
            if r.status_code == 401 and attempt == 0:
                continue                                                  # the access token was rejected: renew once and retry
            break
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                raise GmailError("unavailable", "Gmail answered unexpectedly.") from None
        logger.warning("gmail request answered %s", r.status_code)
        if r.status_code == 401:
            self._on_reconnect()
            raise GmailError("reconnect", "Gmail no longer accepts this connection. Connect Gmail again.")
        if r.status_code == 404:
            raise GmailError("not_found", "That email or attachment is no longer in the mailbox.")
        if r.status_code == 429:
            raise GmailError("rate_limited", "Gmail is limiting requests just now. Wait a minute and try again.")
        if r.status_code == 403:
            raise GmailError("unavailable", "Gmail refused the request (check that the Gmail API is enabled for the OAuth project).")
        raise GmailError("unavailable", "Gmail answered with an error. Try again.")

    # ------------------------------------------------------------------ the GmailClient interface
    def profile(self) -> str:
        return str(self._get("/profile").get("emailAddress") or "")

    def search(self, query: str, max_results: int) -> tuple[list[str], int]:
        body = self._get("/messages", {"q": query, "maxResults": max_results})
        ids = [str(m["id"]) for m in body.get("messages") or [] if isinstance(m, dict) and m.get("id")]
        return ids[:max_results], int(body.get("resultSizeEstimate") or len(ids))

    def message(self, message_id: str) -> RawMessage:
        return self._get(f"/messages/{quote(message_id, safe='')}", {"format": "full"})

    def attachment(self, message_id: str, part_id: str, max_bytes: int) -> bytes:
        # Attachment ids are not stable between calls, so the part is looked up again by its (stable) part id.
        part = _find_part(self.message(message_id).get("payload"), part_id)
        if part is None:
            raise GmailError("not_found", "That attachment is no longer in the email.")
        body = part.get("body") or {}
        if int(body.get("size") or 0) > max_bytes:
            raise GmailError("too_large", "The attachment is larger than the upload limit.")
        if body.get("attachmentId"):
            data = self._get(f"/messages/{quote(message_id, safe='')}/attachments/{quote(str(body['attachmentId']), safe='')}").get("data")
        else:
            data = body.get("data")                                       # a small attachment can come inline in the message
        if not data:
            raise GmailError("not_found", "The attachment has no content.")
        raw = _b64url(str(data))
        if len(raw) > max_bytes:
            raise GmailError("too_large", "The attachment is larger than the upload limit.")
        return raw
