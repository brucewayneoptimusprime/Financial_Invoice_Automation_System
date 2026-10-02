"""OAuth 2.0 web flow for Gmail import (SPEC section 11 item 87): authorization-code flow with state, PKCE (S256) and a binding cookie.

- `start` creates a single-use state (10-minute TTL, at most MAX_PENDING pending), a PKCE verifier and a separate random binding value
  whose hash is kept server-side; the binding goes to the browser as an HttpOnly cookie. The authorization URL asks for exactly the
  scopes in `scopes.py` (gmail.readonly), offline access and a consent prompt (so Google returns a refresh token).
- `finish` consumes the state FIRST, then checks expiry, the binding cookie (constant time) and Google's `error`; any failure raises
  OAuthFailed with a code and makes NO token request. Only then is the code exchanged (with the verifier). A granted scope set that is
  not exactly gmail.readonly is refused and the token revoked; so is a reply without a refresh token.

Every HTTP call goes through the injected httpx.Client (MockTransport in tests). No token, code, verifier, secret or Google response
body is ever logged or put in an exception message.
"""
import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.gmail.errors import GmailError
from app.gmail.scopes import GMAIL_SCOPES

logger = logging.getLogger("app.gmail.oauth")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
BINDING_COOKIE = "gmail_oauth_binding"
COOKIE_PATH = "/api/gmail/oauth"
MAX_PENDING = 5
RESULT_CODES = ("state_invalid", "state_expired", "binding_mismatch", "denied", "exchange_failed", "scope_mismatch", "no_refresh_token")


class OAuthFailed(Exception):
    def __init__(self, code: str):
        assert code in RESULT_CODES
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scopes: tuple[str, ...]

    def __repr__(self) -> str:                                            # never print a token, even in a traceback
        return f"Tokens(scopes={self.scopes}, expires_in={self.expires_in})"


@dataclass(frozen=True)
class _Pending:
    verifier: str
    binding_hash: str
    created: float


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def code_challenge(verifier: str) -> str:
    """PKCE S256: base64url(SHA-256(verifier)) without padding (RFC 7636)."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


class OAuthFlows:
    def __init__(self, settings: Settings, http: httpx.Client, clock: Callable[[], float] = time.monotonic):
        self.settings, self.http, self._clock = settings, http, clock
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def _client(self) -> tuple[str, str]:
        cid, secret = self.settings.google_client_id_value(), self.settings.google_client_secret_value()
        if cid is None or secret is None:
            raise GmailError("not_set_up", "Gmail import is not set up (missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET).")
        return cid, secret

    # ------------------------------------------------------------------ start
    def start(self) -> tuple[str, str]:
        """(authorization URL, binding value for the cookie)."""
        client_id, _ = self._client()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)                               # 64 characters of [A-Za-z0-9_-]: RFC 7636 allows 43-128
        binding = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_locked()
            self._pending[state] = _Pending(verifier, _sha256_hex(binding), self._clock())
            while len(self._pending) > MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))
        params = {"client_id": client_id, "redirect_uri": self.settings.gmail_redirect_uri, "response_type": "code",
                  "scope": " ".join(GMAIL_SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
                  "code_challenge": code_challenge(verifier), "code_challenge_method": "S256"}
        return f"{AUTH_URL}?{urlencode(params)}", binding

    def _prune_locked(self) -> None:
        horizon = self._clock() - self.settings.gmail_oauth_state_ttl_s
        for s in [s for s, p in self._pending.items() if p.created < horizon]:
            del self._pending[s]

    # ------------------------------------------------------------------ finish
    def finish(self, *, code: str | None, state: str | None, error: str | None, binding: str | None) -> Tokens:
        with self._lock:
            pending = self._pending.pop(state, None) if state else None     # consumed first: a state works exactly once
        if pending is None:
            raise OAuthFailed("state_invalid")
        if self._clock() - pending.created > self.settings.gmail_oauth_state_ttl_s:
            raise OAuthFailed("state_expired")
        if not binding or not hmac.compare_digest(_sha256_hex(binding), pending.binding_hash):
            raise OAuthFailed("binding_mismatch")
        if error or not code:
            raise OAuthFailed("denied" if error else "exchange_failed")
        client_id, secret = self._client()
        try:
            r = self.http.post(TOKEN_URL, data={"code": code, "client_id": client_id, "client_secret": secret,
                                                "redirect_uri": self.settings.gmail_redirect_uri, "grant_type": "authorization_code",
                                                "code_verifier": pending.verifier})
        except httpx.HTTPError as exc:
            logger.warning("gmail token exchange failed: %s", type(exc).__name__)
            raise OAuthFailed("exchange_failed") from None
        if r.status_code != 200:
            logger.warning("gmail token exchange answered %s", r.status_code)
            raise OAuthFailed("exchange_failed")
        try:
            body = r.json()
            access = str(body["access_token"])
        except (ValueError, KeyError, TypeError):
            raise OAuthFailed("exchange_failed") from None
        granted = tuple(sorted(str(body.get("scope", "")).split()))
        if set(granted) != set(GMAIL_SCOPES):
            self.revoke(access)                                             # never keep a broader (or different) grant
            raise OAuthFailed("scope_mismatch")
        refresh = body.get("refresh_token")
        if not refresh:
            self.revoke(access)
            raise OAuthFailed("no_refresh_token")
        return Tokens(access_token=access, refresh_token=str(refresh), expires_in=int(body.get("expires_in") or 3600), scopes=granted)

    # ------------------------------------------------------------------ refresh / revoke
    def refresh(self, refresh_token: str) -> tuple[str, int]:
        """A fresh access token. Raises GmailError('reconnect') when Google no longer accepts the refresh token (revoked, expired:
        Testing-mode apps get 7-day refresh tokens), GmailError('unavailable') for anything else."""
        client_id, secret = self._client()
        try:
            r = self.http.post(TOKEN_URL, data={"client_id": client_id, "client_secret": secret, "refresh_token": refresh_token,
                                                "grant_type": "refresh_token"})
        except httpx.HTTPError as exc:
            logger.warning("gmail token refresh failed: %s", type(exc).__name__)
            raise GmailError("unavailable", "Google could not be reached to renew the Gmail connection.") from None
        if r.status_code == 200:
            try:
                body = r.json()
                return str(body["access_token"]), int(body.get("expires_in") or 3600)
            except (ValueError, KeyError, TypeError):
                raise GmailError("unavailable", "Google answered the token renewal unexpectedly.") from None
        try:
            err = r.json().get("error")
        except ValueError:
            err = None
        if r.status_code in (400, 401) and err in ("invalid_grant", "unauthorized_client", "invalid_client"):
            raise GmailError("reconnect", "The Gmail connection has expired or was revoked. Connect Gmail again.")
        logger.warning("gmail token refresh answered %s", r.status_code)
        raise GmailError("unavailable", "Google could not renew the Gmail connection just now.")

    def revoke(self, token: str) -> bool:
        try:
            r = self.http.post(REVOKE_URL, data={"token": token})
        except httpx.HTTPError as exc:
            logger.warning("gmail token revoke failed: %s", type(exc).__name__)
            return False
        return r.status_code == 200


# ------------------------------------------------------------------------------------------ access-log redaction

CALLBACK_PATH = "/api/gmail/oauth/callback"


class RedactCallbackQuery(logging.Filter):
    """The OAuth callback URL carries the authorization code and the state in its query string. Server access logs (uvicorn) would
    print them; this filter replaces the query with <redacted> before any handler sees the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str) and CALLBACK_PATH in record.msg:
            record.msg = _redact(record.msg)
        return True


def _redact(text: str) -> str:
    if CALLBACK_PATH not in text:
        return text
    head, _, tail = text.partition(CALLBACK_PATH)
    if not tail.startswith("?"):
        return text
    rest = tail[1:]
    end = min((i for i in (rest.find(" "), rest.find('"')) if i >= 0), default=len(rest))
    return f"{head}{CALLBACK_PATH}?<redacted>{rest[end:]}"


def install_log_redaction(names: tuple[str, ...] = ("uvicorn.access", "uvicorn.error")) -> None:
    """Idempotent. Called when the app starts (after uvicorn has configured its loggers)."""
    for name in names:
        logger_ = logging.getLogger(name)
        if not any(isinstance(f, RedactCallbackQuery) for f in logger_.filters):
            logger_.addFilter(RedactCallbackQuery())
