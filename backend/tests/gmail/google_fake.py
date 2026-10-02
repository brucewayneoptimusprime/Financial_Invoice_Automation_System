"""A fake Google behind httpx.MockTransport: the OAuth token and revoke endpoints and the Gmail API (users/me), backed by the labelled
fake inbox. Every request is recorded. Nothing here touches the network.

Tokens contain "CANARY" so tests can prove no token ever reaches a log, a response or the database in clear text.
"""
import base64
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.gmail.oauth import REVOKE_URL, TOKEN_URL
from app.gmail.scopes import GMAIL_READONLY_SCOPE
from tests.api.helpers import build_app
from tests.gmail.helpers import fake_client, gmail_settings

GMAIL_PREFIX = "https://gmail.googleapis.com/gmail/v1/users/me"
REFRESH_TOKEN = "1//fake-refresh-CANARY"


class FakeGoogle:
    def __init__(self, *, account="inbox@example.com", scope=GMAIL_READONLY_SCOPE, give_refresh=True):
        self.account, self.scope, self.give_refresh = account, scope, give_refresh
        self.inbox = fake_client()
        self.requests: list[httpx.Request] = []
        self.forms: list[dict] = []
        self.valid_access: set[str] = set()
        self.issued = 0
        self.revoked: list[str] = []
        self.exchange_status = 200
        self.refresh_error: str | None = None
        self.revoke_status = 200
        self.gmail_status: int | None = None          # force every Gmail request to answer this
        self.reject_next = False                      # the next Gmail request gets a 401 (an access token Google dropped)
        self.inline_parts: set[tuple[str, str]] = set()
        self._attachment_ids: dict[str, tuple[str, str]] = {}

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _access(self) -> str:
        self.issued += 1
        token = f"ya29.fake-access-CANARY-{self.issued}"
        self.valid_access.add(token)
        return token

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.method == "POST" and url == TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.forms.append(form)
            if form.get("grant_type") == "authorization_code":
                if self.exchange_status != 200:
                    return httpx.Response(self.exchange_status, json={"error": "invalid_grant", "error_description": "CANARY body"})
                body = {"access_token": self._access(), "expires_in": 3599, "scope": self.scope, "token_type": "Bearer"}
                if self.give_refresh:
                    body["refresh_token"] = REFRESH_TOKEN
                return httpx.Response(200, json=body)
            if form.get("grant_type") == "refresh_token":
                if self.refresh_error:
                    return httpx.Response(400, json={"error": self.refresh_error, "error_description": "CANARY body"})
                return httpx.Response(200, json={"access_token": self._access(), "expires_in": 3599, "scope": self.scope})
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if request.method == "POST" and url == REVOKE_URL:
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(self.revoke_status)
        if url.startswith(GMAIL_PREFIX) and request.method == "GET":
            return self._gmail(request)
        return httpx.Response(599, text="unexpected request in the fake Google")

    def _gmail(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if self.reject_next:
            self.reject_next = False
            self.valid_access.discard(auth.removeprefix("Bearer "))
            return httpx.Response(401, json={"error": {"message": "CANARY body"}})
        if auth.removeprefix("Bearer ") not in self.valid_access:
            return httpx.Response(401, json={"error": {"message": "CANARY body"}})
        if self.gmail_status:
            return httpx.Response(self.gmail_status, json={"error": {"message": "CANARY body from Google"}})
        parts = urlsplit(str(request.url))
        path = parts.path.removeprefix("/gmail/v1/users/me")
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": self.account, "messagesTotal": 11})
        if path == "/messages":
            ids, estimate = self.inbox.search(query["q"], int(query.get("maxResults", 100)))
            return httpx.Response(200, json={"messages": [{"id": i, "threadId": i} for i in ids], "resultSizeEstimate": estimate})
        segments = path.strip("/").split("/")
        if len(segments) == 2:
            try:
                msg = self.inbox.message(segments[1])
            except Exception:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            for part in msg["payload"]["parts"]:
                if not part.get("filename"):
                    continue
                if (segments[1], part["partId"]) in self.inline_parts:
                    data = self.inbox.attachment(segments[1], part["partId"], 10**9)
                    part["body"] = {"size": len(data), "data": base64.urlsafe_b64encode(data).decode().rstrip("=")}
                    continue
                att_id = f"ATT{len(self._attachment_ids)}"                           # a new id on every call, like Gmail
                self._attachment_ids[att_id] = (segments[1], part["partId"])
                part["body"]["attachmentId"] = att_id
            return httpx.Response(200, json=msg)
        if len(segments) == 4 and segments[2] == "attachments":
            mid, pid = self._attachment_ids.get(segments[3], (None, None))
            if mid != segments[1]:
                return httpx.Response(404, json={"error": {"message": "stale attachment id"}})
            data = self.inbox.attachment(mid, pid, 10**9)
            return httpx.Response(200, json={"size": len(data), "data": base64.urlsafe_b64encode(data).decode().rstrip("=")})
        return httpx.Response(404, json={"error": {"message": "unknown"}})

    # helpers for assertions
    def token_posts(self, grant: str | None = None) -> list[dict]:
        return [f for f in self.forms if grant is None or f.get("grant_type") == grant]

    def gmail_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url).startswith("https://gmail.googleapis.com")]


SECRETS = {"google_client_id": "test-client-id.apps.googleusercontent.com", "google_client_secret": "test-client-secret-CANARY",
           "oauth_encryption_key": Fernet.generate_key().decode()}


@contextmanager
def google_api(tmp_path, google: FakeGoogle, *, run_fn=None, **kw):
    """(TestClient, app) with the REAL Gmail backend (gmail_backend unset, the three secrets set), all HTTP to the fake Google."""
    kw.setdefault("gmail_backend", None)
    for k, v in SECRETS.items():
        kw.setdefault(k, v)
    settings = gmail_settings(tmp_path, **kw)
    app, worker, db, settings = build_app(tmp_path, settings=settings, run_fn=run_fn)
    svc = app.state.api.gmail
    svc._http, svc._owns_http = httpx.Client(transport=google.transport), False
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c, app


def connect(c) -> httpx.Response:
    """Run the whole browser flow against the fake: start, then Google 'redirects' back with a code and the state."""
    url = c.post("/api/gmail/oauth/start").json()["authorization_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    return c.get("/api/gmail/oauth/callback", params={"code": "auth-code-CANARY", "state": state}, follow_redirects=False)
