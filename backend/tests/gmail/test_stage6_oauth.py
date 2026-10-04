"""Gmail import, stage 6: the real OAuth web flow (state + PKCE + binding cookie), the encrypted refresh token, and
GoogleGmailClient. All HTTP goes to a fake Google through httpx.MockTransport; nothing touches the network."""
import inspect
import logging
import sqlite3
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.gmail.client import GMAIL_CLIENT_METHODS
from app.gmail.google_client import GMAIL_BASE, GoogleGmailClient
from app.gmail.oauth import AUTH_URL, MAX_PENDING, REVOKE_URL, TOKEN_URL, code_challenge
from app.gmail.scopes import GMAIL_READONLY_SCOPE
from tests.gmail.google_fake import REFRESH_TOKEN, FakeGoogle, connect, google_api
from tests.gmail.regression import HashRuns

RETURN = "http://localhost:5173/invoices"


def location(r) -> str:
    assert r.status_code == 303, r.text
    return r.headers["location"]


def rows(app, sql, *args):
    with closing(sqlite3.connect(app.state.api.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args)]


# ------------------------------------------------------------------------------------------ start

def test_start_asks_for_exactly_the_readonly_scope_with_pkce_and_sets_the_binding_cookie(tmp_path):
    google = FakeGoogle()
    with google_api(tmp_path, google) as (c, app):
        r = c.post("/api/gmail/oauth/start")
        flows = app.state.api.gmail.oauth
    assert r.status_code == 200
    url = urlsplit(r.json()["authorization_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == AUTH_URL
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert set(q) == {"client_id", "redirect_uri", "response_type", "scope", "access_type", "prompt", "state", "code_challenge",
                      "code_challenge_method"}
    assert q["scope"] == GMAIL_READONLY_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"
    assert (q["response_type"], q["access_type"], q["prompt"], q["code_challenge_method"]) == ("code", "offline", "consent", "S256")
    assert q["redirect_uri"] == "http://localhost:8000/api/gmail/oauth/callback"
    pending = flows._pending[q["state"]]
    assert 43 <= len(pending.verifier) <= 128 and q["code_challenge"] == code_challenge(pending.verifier) != pending.verifier
    cookie = r.headers["set-cookie"].lower()
    for part in ("gmail_oauth_binding=", "httponly", "samesite=lax", "path=/api/gmail/oauth", "max-age=600"):
        assert part in cookie
    assert google.requests == []                                                 # starting contacts nobody


def test_pending_states_are_capped(tmp_path):
    with google_api(tmp_path, FakeGoogle()) as (c, app):
        for _ in range(MAX_PENDING + 3):
            c.post("/api/gmail/oauth/start")
        assert len(app.state.api.gmail.oauth._pending) == MAX_PENDING


# ------------------------------------------------------------------------------------------ the callback

def test_a_successful_connect_stores_only_the_encrypted_refresh_token(tmp_path):
    google = FakeGoogle()
    with google_api(tmp_path, google) as (c, app):
        start = c.post("/api/gmail/oauth/start").json()["authorization_url"]
        q = {k: v[0] for k, v in parse_qs(urlsplit(start).query).items()}
        r = c.get("/api/gmail/oauth/callback", params={"code": "auth-code-CANARY", "state": q["state"]}, follow_redirects=False)
        status = c.get("/api/gmail/status").json()
        stored = rows(app, "SELECT * FROM oauth_credentials")
        db_bytes = app.state.api.db_path.read_bytes()
    assert location(r) == f"{RETURN}?gmail=connected"
    assert 'gmail_oauth_binding=""' in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower()
    exchange = google.token_posts("authorization_code")[0]
    assert code_challenge(exchange["code_verifier"]) == q["code_challenge"] and exchange["code"] == "auth-code-CANARY"
    assert exchange["redirect_uri"] == "http://localhost:8000/api/gmail/oauth/callback"
    assert status["connected"] and status["account_email"] == "inbox@example.com" and not status["reconnect"]
    assert len(stored) == 1 and stored[0]["scopes"] == GMAIL_READONLY_SCOPE and stored[0]["provider"] == "google"
    assert b"CANARY" not in db_bytes and REFRESH_TOKEN.encode() not in bytes(stored[0]["refresh_token_enc"])


def _callback(c, **params):
    return c.get("/api/gmail/oauth/callback", params=params, follow_redirects=False)


@pytest.mark.parametrize("case, expected", [
    ("no_state", "state_invalid"),
    ("unknown_state", "state_invalid"),
    ("replayed_state", "state_invalid"),
    ("expired_state", "state_expired"),
    ("no_cookie", "binding_mismatch"),
    ("wrong_cookie", "binding_mismatch"),
    ("google_error", "denied"),
    ("no_code", "exchange_failed"),
])
def test_a_bad_callback_never_asks_google_for_a_token(tmp_path, case, expected):
    google = FakeGoogle()
    with google_api(tmp_path, google) as (c, app):
        url = c.post("/api/gmail/oauth/start").json()["authorization_url"]
        state = parse_qs(urlsplit(url).query)["state"][0]
        flows = app.state.api.gmail.oauth
        if case == "no_state":
            r = _callback(c, code="x")
        elif case == "unknown_state":
            r = _callback(c, code="x", state="forged")
        elif case == "replayed_state":
            assert location(_callback(c, code="x", state=state)) == f"{RETURN}?gmail=connected"
            google.forms.clear()
            c.post("/api/gmail/oauth/start")                                     # a fresh cookie, but the old state is spent
            r = _callback(c, code="x", state=state)
        elif case == "expired_state":
            real = flows._clock
            flows._clock = lambda: real() + app.state.api.settings.gmail_oauth_state_ttl_s + 1
            r = _callback(c, code="x", state=state)
        elif case == "no_cookie":
            c.cookies.clear()
            r = _callback(c, code="x", state=state)
        elif case == "wrong_cookie":
            c.cookies.clear()
            c.cookies.set("gmail_oauth_binding", "attacker-binding", domain="localhost", path="/api/gmail/oauth")
            r = _callback(c, code="x", state=state)
        elif case == "google_error":
            r = _callback(c, error="access_denied", state=state)
        else:
            r = _callback(c, state=state)
        stored = rows(app, "SELECT * FROM oauth_credentials") if case != "replayed_state" else []
    assert location(r) == f"{RETURN}?gmail=error&code={expected}"
    assert google.token_posts("authorization_code") == [] and stored == []
    assert "x" not in urlsplit(location(r)).query.replace("exchange", "").replace("expired", "")   # nothing from the request is echoed


@pytest.mark.parametrize("setup, expected", [
    ("broader_scope", "scope_mismatch"),
    ("no_refresh", "no_refresh_token"),
    ("exchange_400", "exchange_failed"),
])
def test_a_wrong_grant_is_refused_revoked_and_not_stored(tmp_path, setup, expected):
    google = FakeGoogle(scope=f"{GMAIL_READONLY_SCOPE} https://www.googleapis.com/auth/gmail.modify" if setup == "broader_scope"
                        else GMAIL_READONLY_SCOPE, give_refresh=setup != "no_refresh")
    if setup == "exchange_400":
        google.exchange_status = 400
    with google_api(tmp_path, google) as (c, app):
        r = connect(c)
        stored = rows(app, "SELECT * FROM oauth_credentials")
        status = c.get("/api/gmail/status").json()
    assert location(r) == f"{RETURN}?gmail=error&code={expected}" and stored == [] and not status["connected"]
    if setup != "exchange_400":
        assert google.revoked and google.revoked[0].startswith("ya29.")         # the access token that was granted is given back


def test_the_callback_is_the_only_route_outside_the_access_token(tmp_path):
    with google_api(tmp_path, FakeGoogle(), access_token="tok-6") as (c, app):
        assert c.post("/api/gmail/oauth/start").status_code == 401
        assert c.post("/api/gmail/disconnect", json={"confirm": True}).status_code == 401
        assert c.get("/api/gmail/status").status_code == 401
        r = c.get("/api/gmail/oauth/callback", params={"code": "x", "state": "forged"}, follow_redirects=False)
        assert location(r) == f"{RETURN}?gmail=error&code=state_invalid"
        assert c.get("/api/gmail/oauth/callbackX").status_code == 401             # exact path only


def test_connecting_needs_the_real_backend(tmp_path):
    google = FakeGoogle()
    with google_api(tmp_path, google, gmail_backend="fake") as (c, app):
        assert c.post("/api/gmail/oauth/start").json()["error"] == "not_set_up"
        r = c.get("/api/gmail/oauth/callback", params={"code": "x", "state": "y"}, follow_redirects=False)
    assert location(r) == f"{RETURN}?gmail=error&code=not_set_up" and google.requests == []


# ------------------------------------------------------------------------------------------ the real client

def test_the_real_client_has_exactly_the_interface_and_only_reads(tmp_path):
    public = {n for n, _ in inspect.getmembers(GoogleGmailClient, inspect.isfunction) if not n.startswith("_")}
    assert public == set(GMAIL_CLIENT_METHODS)
    google = FakeGoogle()
    with google_api(tmp_path, google, run_fn=HashRuns()) as (c, app):
        assert location(connect(c)).endswith("connected")
        s = c.post("/api/gmail/search", json={"query": "after:2026/08/01"}).json()
        r = c.post("/api/gmail/import", json={"search_id": s["search_id"], "confirm": True,
                                              "items": [{"message_id": "fake-ss-two", "part_id": "2"}]})
        assert app.state.api.worker.wait_idle(60)
    hosts = {(req.method, f"{req.url.scheme}://{req.url.host}") for req in google.requests}
    assert hosts <= {("POST", "https://oauth2.googleapis.com"), ("GET", "https://gmail.googleapis.com")}
    assert all(req.method == "GET" and str(req.url).startswith(GMAIL_BASE) for req in google.gmail_requests())
    assert all(req.headers["Authorization"].startswith("Bearer ya29.") for req in google.gmail_requests())
    assert {str(req.url) for req in google.requests if req.method == "POST"} <= {TOKEN_URL, REVOKE_URL}
    assert len(s["messages"]) == 9 and r.json()["items"][0]["status"] == "queued"


def test_a_google_import_is_identical_to_the_fake_one_and_records_the_real_account(tmp_path):
    google = FakeGoogle()
    with google_api(tmp_path, google, run_fn=HashRuns()) as (c, app):
        connect(c)
        s = c.post("/api/gmail/search", json={"query": "10963"}).json()
        run_id = c.post("/api/gmail/import", json={"search_id": s["search_id"], "confirm": True,
                                                   "items": [{"message_id": "fake-ss-10963", "part_id": "1"}]}).json()["items"][0]["run_id"]
        assert app.state.api.worker.wait_idle(60)
        view = c.get(f"/api/runs/{run_id}").json()
        stored = rows(app, "SELECT account_email FROM gmail_imports")
    assert view["decision"] == "review" and view["match"]["matched_po"] == "PO-SS-001" and view["source"]["kind"] == "gmail"
    assert stored == [{"account_email": "inbox@example.com"}]
    assert not any("/profile" in str(r.url) for r in google.gmail_requests()[1:])   # the account is not re-read for every call


def test_a_small_attachment_sent_inline_in_the_message_is_read_from_there(tmp_path):
    google = FakeGoogle()
    google.inline_parts.add(("fake-ss-10963", "1"))
    with google_api(tmp_path, google, run_fn=HashRuns()) as (c, app):
        connect(c)
        s = c.post("/api/gmail/search", json={"query": "10963"}).json()
        out = c.post("/api/gmail/import", json={"search_id": s["search_id"], "confirm": True,
                                                "items": [{"message_id": "fake-ss-10963", "part_id": "1"}]}).json()["items"][0]
        assert app.state.api.worker.wait_idle(60)
    assert out["status"] == "queued" and not any("/attachments/" in str(r.url) for r in google.gmail_requests())


def test_an_expired_access_token_is_renewed_and_a_rejected_one_is_retried_once(tmp_path):
    google = FakeGoogle()
    with google_api(tmp_path, google) as (c, app):
        connect(c)
        svc = app.state.api.gmail
        real = svc._clock
        svc._google = None                                                       # a fresh client: no access token in memory yet
        assert c.post("/api/gmail/search", json={"query": "invoice"}).status_code == 200
        renewed = len(google.token_posts("refresh_token"))
        google.reject_next = True
        assert c.post("/api/gmail/search", json={"query": "invoice"}).status_code == 200
    assert renewed == 1 and len(google.token_posts("refresh_token")) == 2
    assert all(f["refresh_token"] == REFRESH_TOKEN for f in google.token_posts("refresh_token"))


def test_a_refresh_token_google_no_longer_accepts_means_connect_again(tmp_path):
    google = FakeGoogle()
    with google_api(tmp_path, google) as (c, app):
        connect(c)
        app.state.api.gmail._google = None
        google.refresh_error = "invalid_grant"                                   # e.g. the 7-day expiry of a Testing-mode app
        r = c.post("/api/gmail/search", json={"query": "invoice"})
        status = c.get("/api/gmail/status").json()
        stored = rows(app, "SELECT * FROM oauth_credentials")
    assert r.status_code == 409 and r.json()["error"] == "reconnect" and "CANARY" not in r.text
    assert stored == [] and not status["connected"]


@pytest.mark.parametrize("http_status, code, status_code", [(429, "rate_limited", 429), (500, "unavailable", 502),
                                                            (403, "unavailable", 502), (404, "not_found", 404)])
def test_gmail_errors_become_plain_codes_without_google_text(tmp_path, http_status, code, status_code):
    google = FakeGoogle()
    with google_api(tmp_path, google) as (c, app):
        connect(c)
        google.gmail_status = http_status
        r = c.post("/api/gmail/search", json={"query": "invoice"})
    assert r.status_code == status_code and r.json()["error"] == code and "CANARY" not in r.text and "Google:" not in r.text


def test_a_stored_token_from_another_key_asks_to_reconnect(tmp_path):
    google = FakeGoogle()
    with google_api(tmp_path, google) as (c, app):
        connect(c)
        svc = app.state.api.gmail
        svc.settings = svc.settings.model_copy(update={"oauth_encryption_key": SecretStr(Fernet.generate_key().decode())})
        svc._google = None
        status = c.get("/api/gmail/status").json()
        r = c.post("/api/gmail/search", json={"query": "invoice"})
    assert not status["connected"] and status["reconnect"] and r.status_code == 409 and r.json()["error"] == "reconnect"


# ------------------------------------------------------------------------------------------ disconnect

@pytest.mark.parametrize("revoke_status, revoked", [(200, True), (500, False)])
def test_disconnect_revokes_and_always_deletes(tmp_path, revoke_status, revoked):
    google = FakeGoogle()
    google.revoke_status = revoke_status
    with google_api(tmp_path, google) as (c, app):
        connect(c)
        refused = c.post("/api/gmail/disconnect", json={})
        r = c.post("/api/gmail/disconnect", json={"confirm": True})
        stored = rows(app, "SELECT * FROM oauth_credentials")
        status = c.get("/api/gmail/status").json()
    assert refused.status_code == 400 and refused.json()["error"] == "confirm_required"
    assert r.json() == {"disconnected": True, "revoked": revoked} and stored == [] and not status["connected"]
    assert google.revoked[-1] == REFRESH_TOKEN


def test_connecting_another_account_replaces_the_first(tmp_path):
    google = FakeGoogle()
    with google_api(tmp_path, google) as (c, app):
        connect(c)
        google.account = "second@example.com"
        connect(c)
        stored = rows(app, "SELECT account_email FROM oauth_credentials")
    assert stored == [{"account_email": "second@example.com"}]


# ------------------------------------------------------------------------------------------ no token anywhere it should not be

def test_no_token_secret_or_code_reaches_a_log_or_a_response(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    google = FakeGoogle()
    bodies = []
    with google_api(tmp_path, google) as (c, app):
        bodies.append(connect(c).headers["location"])
        bodies.append(c.get("/api/gmail/status").text)
        bodies.append(c.post("/api/gmail/search", json={"query": "invoice"}).text)
        google.gmail_status = 500
        bodies.append(c.post("/api/gmail/search", json={"query": "invoice"}).text)
        app.state.api.gmail._google = None
        google.refresh_error = "invalid_grant"
        bodies.append(c.post("/api/gmail/search", json={"query": "invoice"}).text)
        bodies.append(repr(app.state.api.gmail._google) + repr(app.state.api.gmail.oauth))
    # The TestClient logs its OWN requests (the browser's side, including the callback URL Google sends the browser to); those are
    # not the app's logs. The app's records, and every response, must hold no token, code or secret.
    app_logs = [r.getMessage() for r in caplog.records if "http://localhost:8000/" not in r.getMessage()]
    text = "\n".join(app_logs + bodies)
    assert "CANARY" not in text and "ya29." not in text and "1//" not in text and "test-client-secret" not in text


def test_the_access_log_never_shows_the_callback_code_or_state(tmp_path):
    from app.gmail.oauth import RedactCallbackQuery
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5000", "GET", "/api/gmail/oauth/callback?code=4/abc-CANARY&state=xyz", "1.1", 303), None)
    RedactCallbackQuery().filter(record)
    assert record.getMessage() == '127.0.0.1:5000 - "GET /api/gmail/oauth/callback?<redacted> HTTP/1.1" 303'
    plain = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "GET /api/gmail/oauth/callback?code=CANARY&state=s done",
                              None, None)
    RedactCallbackQuery().filter(plain)
    assert "CANARY" not in plain.getMessage() and plain.getMessage().endswith("?<redacted> done")
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", ("/api/runs?limit=5",), None)
    RedactCallbackQuery().filter(other)
    assert other.getMessage() == "/api/runs?limit=5"                          # other URLs are left alone
    with google_api(tmp_path, FakeGoogle()) as (c, app):
        installed = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, RedactCallbackQuery)]
    assert len(installed) == 1                                                 # installed once when the app starts


# ------------------------------------------------------------------------------------------ deployed behind the Vercel proxy

VERCEL = "https://financial-invoice-automation-system-mauve.vercel.app"


def test_locally_the_binding_cookie_is_not_secure_so_http_localhost_keeps_working(tmp_path):
    with google_api(tmp_path, FakeGoogle()) as (c, app):
        cookie = c.post("/api/gmail/oauth/start").headers["set-cookie"].lower()
    assert "secure" not in cookie and "samesite=lax" in cookie and "httponly" in cookie


def test_deployed_the_cookie_is_secure_and_the_whole_flow_works_on_the_frontend_origin(tmp_path):
    """GMAIL_REDIRECT_URI on the Vercel domain (Vercel proxies /api/gmail/* here): the binding cookie becomes Secure, keeps
    HttpOnly / SameSite=Lax / its path, and a browser on that https origin completes the flow. Without the cookie the callback still
    refuses (binding_mismatch) and makes no token request."""
    from fastapi.testclient import TestClient
    google = FakeGoogle()
    with google_api(tmp_path, google, gmail_redirect_uri=f"{VERCEL}/api/gmail/oauth/callback",
                    gmail_ui_return_url=f"{VERCEL}/invoices") as (_, app):
        browser = TestClient(app, base_url=VERCEL)                     # same origin as the redirect URI, like the proxied site
        start = browser.post("/api/gmail/oauth/start")
        cookie = start.headers["set-cookie"].lower()
        for part in ("gmail_oauth_binding=", "httponly", "samesite=lax", "path=/api/gmail/oauth", "secure"):
            assert part in cookie
        q = {k: v[0] for k, v in parse_qs(urlsplit(start.json()["authorization_url"]).query).items()}
        assert q["redirect_uri"] == f"{VERCEL}/api/gmail/oauth/callback" and q["scope"] == GMAIL_READONLY_SCOPE
        r = browser.get("/api/gmail/oauth/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
        assert location(r) == f"{VERCEL}/invoices?gmail=connected"
        assert "secure" in r.headers["set-cookie"].lower()                # the deletion matches the cookie's attributes
        assert google.token_posts("authorization_code")[0]["redirect_uri"] == f"{VERCEL}/api/gmail/oauth/callback"

        stranger = TestClient(app, base_url=VERCEL)                    # no binding cookie (what the cross-site setup produced)
        q2 = {k: v[0] for k, v in parse_qs(urlsplit(browser.post("/api/gmail/oauth/start").json()["authorization_url"]).query).items()}
        before = len(google.token_posts("authorization_code"))
        r2 = stranger.get("/api/gmail/oauth/callback", params={"code": "c", "state": q2["state"]}, follow_redirects=False)
        assert location(r2) == f"{VERCEL}/invoices?gmail=error&code=binding_mismatch"
        assert len(google.token_posts("authorization_code")) == before


def test_the_access_token_still_gates_every_gmail_route_except_the_callback(tmp_path):
    token = "a" * 32
    with google_api(tmp_path, FakeGoogle(), access_token=token, gmail_redirect_uri=f"{VERCEL}/api/gmail/oauth/callback") as (c, _):
        assert c.post("/api/gmail/oauth/start").status_code == 401
        assert c.get("/api/gmail/status").status_code == 401
        assert c.get("/api/gmail/oauth/callback", params={"state": "x"}, follow_redirects=False).status_code == 303
