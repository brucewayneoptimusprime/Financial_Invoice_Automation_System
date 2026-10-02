"""Gmail import, stage 2: the query allowlist, attachment eligibility, the fake client, and search (status + preview list) through
the API with the fake backend. No model, no OAuth, no Google call."""
import inspect
import re
from datetime import date

import pytest

from app.config import ROOT_DIR, Settings
from app.gmail import service as service_mod
from app.gmail.attachments import MAX_DEPTH, PartInfo, eligibility, walk_parts
from app.gmail.client import GMAIL_CLIENT_METHODS, GmailClient
from app.gmail.errors import GmailError
from app.gmail.fake import FakeGmailClient
from app.gmail.query import check_and_finalize, finalize, validate_query
from app.gmail.service import GmailService
from tests.gmail.helpers import fake_client, gmail_api, inbox_data, table_counts

S = Settings()
TODAY = date(2026, 10, 1)


def q(text):
    return validate_query(text, max_chars=S.gmail_query_max_chars, max_terms=S.gmail_query_max_terms)


# ------------------------------------------------------------------------------------------ the allowlist

ALLOWED = [
    ("invoice", "invoice"),
    ("Acme invoice", "Acme invoice"),
    ('"Acme Supplies"', '"Acme Supplies"'),
    ("from:billing@superstore.example", "from:billing@superstore.example"),
    ("from:superstore.example", "from:superstore.example"),
    ('from:"SuperStore Billing"', 'from:"SuperStore Billing"'),
    ("to:ap@company.example", "to:ap@company.example"),
    ("subject:invoice", "subject:invoice"),
    ('subject:"tax invoice"', 'subject:"tax invoice"'),
    ("after:2026/08/01", "after:2026/08/01"),
    ("after:2026-08-01", "after:2026/08/01"),
    ("before:2026/9/30", "before:2026/09/30"),
    ("newer_than:30d", "newer_than:30d"),
    ("older_than:1y", "older_than:1y"),
    ("newer_than:6m", "newer_than:6m"),
    ("filename:pdf", "filename:pdf"),
    ("filename:invoice_10963.pdf", "filename:invoice_10963.pdf"),
    ("larger:100K", "larger:100K"),
    ("smaller:5M", "smaller:5M"),
    ("has:attachment", "has:attachment"),
    ("HAS:Attachment", "has:attachment"),
    ("invoice OR receipt", "invoice OR receipt"),
    ("-newsletter", "-newsletter"),
    ("-from:noreply@example.com", "-from:noreply@example.com"),
    ('-"out of office"', '-"out of office"'),
    ("-subject:reminder", "-subject:reminder"),
    ("invoice 10963", "invoice 10963"),
    ("Müller Rechnung", "Müller Rechnung"),
    ("acme-supplies inv-7781", "acme-supplies inv-7781"),
    ("", ""),
]


@pytest.mark.parametrize("text, canonical", ALLOWED)
def test_allowed_queries(text, canonical):
    assert q(text).text == canonical


REFUSED = [
    ("in:anywhere invoice", "spam, trash or all mail"),
    ("in:spam", "spam, trash or all mail"),
    ("in:trash", "spam, trash or all mail"),
    ("is:unread", "'is:'"),
    ("label:finance", "'label:'"),
    ("category:promotions", "'category:'"),
    ("deliveredto:me@example.com", "'deliveredto:'"),
    ("list:announce@example.com", "'list:'"),
    ("rfc822msgid:abc@mail", "'rfc822msgid:'"),
    ("cc:boss@example.com", "not an allowed search operator"),
    ("has:drive", "only has:attachment"),
    ("has:userlabels", "only has:attachment"),
    ("-has:attachment", "cannot be negated"),
    ("-after:2026/01/01", "cannot be negated"),
    ("(invoice OR receipt)", "brackets"),
    ("{invoice receipt}", "brackets"),
    ("invoice OR", "OR needs a search term on both sides"),
    ("OR invoice", "OR needs a search term on both sides"),
    ("invoice OR OR receipt", "OR needs a search term on both sides"),
    ("invoice AND receipt", "AND"),
    ("from:", "needs a value"),
    ("after:yesterday", "YYYY/MM/DD"),
    ("after:2026/13/01", "YYYY/MM/DD"),
    ("before:1960/01/01", "YYYY/MM/DD"),
    ("newer_than:0d", "number and d, m or y"),
    ("newer_than:30w", "number and d, m or y"),
    ("from:bad<addr>", "not allowed for from:"),
    ('filename:"a b.pdf"', "does not take a quoted value"),
    ("larger:10G", "not allowed for larger:"),
    ('subject:"unclosed', "quote is not closed"),
    ('"unclosed phrase', "quote is not closed"),
    ('""', "quoted text is empty"),
    ("-", "lone '-'"),
    ("http://evil.example/x", "not an allowed search operator"),
    ("in\x00voice", "control characters"),
    ("a b c d e f g h i j k l m", "more than 12 terms"),
    ("x" * 301, "longer than 300 characters"),
]


@pytest.mark.parametrize("text, problem", REFUSED)
def test_refused_queries_name_the_problem_and_are_never_fixed(text, problem):
    with pytest.raises(GmailError) as err:
        q(text)
    assert err.value.code == "query_invalid" and err.value.status == 422
    assert any(problem in p for p in err.value.problems), err.value.problems


def test_every_problem_is_listed_not_just_the_first():
    with pytest.raises(GmailError) as err:
        q("in:spam is:unread label:x invoice")
    assert len(err.value.problems) == 3


def test_finalize_always_adds_has_attachment_and_a_default_window():
    assert finalize(q("invoice"), S, TODAY) == ("invoice has:attachment newer_than:180d", ["has:attachment", "newer_than:180d"])
    assert finalize(q(""), S, TODAY)[0] == "has:attachment newer_than:180d"


def test_finalize_keeps_a_lower_bound_and_does_not_repeat_has_attachment():
    assert finalize(q("has:attachment after:2026/08/01"), S, TODAY) == ("has:attachment after:2026/08/01", [])
    assert finalize(q("newer_than:30d"), S, TODAY) == ("newer_than:30d has:attachment", ["has:attachment"])


def test_finalize_anchors_the_window_before_an_upper_bound():
    assert finalize(q("before:2026/06/30"), S, TODAY)[1] == ["has:attachment", "after:2026/01/01"]
    assert finalize(q("older_than:30d"), S, TODAY)[1] == ["has:attachment", "after:2026/03/05"]       # 2026-09-01 - 180 days


def test_check_and_finalize_uses_the_configured_limits():
    small = Settings(gmail_query_max_terms=2)
    with pytest.raises(GmailError, match="more than 2 terms"):
        check_and_finalize("a b c", small, TODAY)


# ------------------------------------------------------------------------------------------ attachments

def part(name="x.pdf", mime="application/pdf", size=1000, pid="1"):
    return PartInfo(part_id=pid, filename=name, mime_type=mime, size=size, inline=False)


@pytest.mark.parametrize("p, ok, code", [
    (part(), True, None),
    (part("scan.png", "image/png"), True, None),
    (part("photo.jpg", "image/jpeg"), True, None),
    (part("inv.pdf", "application/octet-stream"), True, None),
    (part("inv.bin", "application/octet-stream"), False, "type"),
    (part("all.zip", "application/zip"), False, "type"),
    (part("all.zip", "application/octet-stream"), False, "type"),
    (part("inv.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"), False, "type"),
    (part("inv.xls", "application/vnd.ms-excel"), False, "type"),
    (part("notes.txt", "text/plain"), False, "type"),
    (part("cal.ics", "text/calendar"), False, "type"),
    (part(size=0), False, "empty"),
    (part(size=20 * 1024 * 1024), True, None),
    (part(size=20 * 1024 * 1024 + 1), False, "too_large"),
])
def test_eligibility(p, ok, code):
    eligible, reason_code, reason = eligibility(p, S)
    assert (eligible, reason_code) == (ok, code) and (reason is None) == ok


def test_walk_parts_lists_only_named_parts_and_stops_at_the_depth_limit():
    def nest(depth):
        node = {"partId": f"d{depth}", "mimeType": "application/pdf", "filename": f"deep{depth}.pdf", "body": {"size": 5}}
        for d in range(depth - 1, -1, -1):
            node = {"partId": f"m{d}", "mimeType": "multipart/mixed", "filename": "", "parts": [node]}
        return node
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"partId": "0", "mimeType": "text/html", "filename": "", "body": {"size": 10}},
        {"partId": "1", "mimeType": "image/png", "filename": "", "headers": [{"name": "Content-Disposition", "value": "inline"}]},
        {"partId": "2", "mimeType": "application/pdf", "filename": "C:\\Users\\x\\inv\u0007.pdf", "body": {"size": 7}},
        nest(MAX_DEPTH - 1), nest(MAX_DEPTH + 1)]}
    names = [p.filename for p in walk_parts(payload)]
    assert names == ["inv.pdf", f"deep{MAX_DEPTH - 1}.pdf"]           # path and control character stripped; the too-deep one skipped


# ------------------------------------------------------------------------------------------ the fake client

def test_the_fake_client_implements_exactly_the_interface_and_nothing_that_writes():
    public = {n for n, _ in inspect.getmembers(FakeGmailClient, inspect.isfunction) if not n.startswith("_")}
    assert public == set(GMAIL_CLIENT_METHODS)
    protocol = {n for n in vars(GmailClient) if not n.startswith("_")}
    assert protocol == set(GMAIL_CLIENT_METHODS)
    assert not any(re.search(r"send|draft|modify|trash|label|delete|insert|import|watch", n) for n in public | protocol)


def test_a_fake_inbox_must_say_it_is_fake():
    data = inbox_data()
    data["_notice"] = "real mail"
    with pytest.raises(ValueError, match="FAKE"):
        FakeGmailClient(data)


@pytest.mark.parametrize("query, expected", [
    ("has:attachment newer_than:180d", ["fake-injection", "fake-ss-two", "fake-zip", "fake-acme", "fake-large", "fake-ss-24429",
                                        "fake-ss-10963", "fake-ss-6459", "fake-iq"]),
    ("from:superstore.example after:2026/09/01 has:attachment", ["fake-injection", "fake-ss-two", "fake-zip", "fake-ss-24429",
                                                                 "fake-ss-10963"]),
    ('subject:"tax invoice"', ["fake-iq"]),
    ("acme has:attachment", ["fake-acme"]),
    ("10963 OR 6459", ["fake-ss-10963", "fake-ss-6459"]),
    ("superstore -subject:invoices has:attachment newer_than:180d", ["fake-injection", "fake-zip", "fake-ss-24429", "fake-ss-10963",
                                                                     "fake-ss-6459"]),
    ("filename:jpg", ["fake-iq"]),
    ("before:2026/01/01", ["fake-old"]),
])
def test_the_fake_search_subset(query, expected):
    ids, estimate = fake_client().search(query, 50)
    assert ids == expected and estimate == len(expected)


def test_the_fake_returns_gmail_shaped_messages_and_bytes_with_the_size_cap():
    c = fake_client()
    m = c.message("fake-ss-two")
    assert set(m) == {"id", "threadId", "internalDate", "snippet", "payload"} and m["internalDate"].isdigit()
    assert [p.filename for p in walk_parts(m["payload"])] == ["invoice_Bill Eplett_14021.pdf", "invoice_Liz Thompson_14130.pdf"]
    data = c.attachment("fake-ss-two", "2", S.max_file_bytes)
    assert data == (ROOT_DIR / "data" / "invoices" / "invoice_Liz Thompson_14130.pdf").read_bytes()
    with pytest.raises(GmailError) as big:
        c.attachment("fake-large", "1", S.max_file_bytes)
    with pytest.raises(GmailError) as missing:
        c.message("nope")
    assert big.value.code == "too_large" and missing.value.code == "not_found" and c.downloads == [("fake-ss-two", "2")]


# ------------------------------------------------------------------------------------------ status

def test_status_with_the_fake_backend(tmp_path):
    with gmail_api(tmp_path) as (c, app):
        s = c.get("/api/gmail/status").json()
    assert s["backend"] == "fake" and s["fake"] and s["connected"] and s["account_email"] == "fake-inbox@example.test"
    assert s["caps"] == {"max_results": 25, "max_import": 10, "query_max_chars": 300, "request_max_chars": 300, "default_window_days": 180}
    assert s["translator_available"] is False


def test_status_when_not_set_up_names_what_is_missing_and_search_refuses(tmp_path):
    with gmail_api(tmp_path, gmail_backend=None) as (c, app):
        s = c.get("/api/gmail/status").json()
        r = c.post("/api/gmail/search", json={"query": "invoice"})
    assert s["backend"] == "disabled" and not s["available"] and not s["connected"]
    assert s["missing"] == ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_ENCRYPTION_KEY"]
    assert r.status_code == 503 and r.json()["error"] == "not_set_up" and "OAUTH_ENCRYPTION_KEY" in r.json()["message"]


def test_the_google_backend_without_a_connected_account(tmp_path):
    from cryptography.fernet import Fernet
    secrets = dict(google_client_id="id-CANARY", google_client_secret="GOCSPX-CANARY", oauth_encryption_key=Fernet.generate_key().decode())
    with gmail_api(tmp_path, gmail_backend=None, **secrets) as (c, app):
        s = c.get("/api/gmail/status")
        r = c.post("/api/gmail/search", json={"query": "invoice"})
    assert s.json()["backend"] == "google" and not s.json()["connected"] and "CANARY" not in s.text
    assert r.status_code == 409 and r.json()["error"] == "not_connected"


# ------------------------------------------------------------------------------------------ search through the API

def test_search_returns_the_preview_list_with_eligibility_and_writes_nothing(tmp_path):
    with gmail_api(tmp_path) as (c, app):
        before = table_counts(app.state.api.db_path)
        r = c.post("/api/gmail/search", json={"query": "after:2026/08/01"})
        after = table_counts(app.state.api.db_path)
    assert r.status_code == 200 and before == after
    body = r.json()
    assert body["query_sent"] == "after:2026/08/01 has:attachment" and body["added_terms"] == ["has:attachment"]
    assert body["account_email"] == "fake-inbox@example.test" and body["backend"] == "fake" and not body["truncated"]
    by_id = {m["message_id"]: m for m in body["messages"]}
    assert set(by_id) == {"fake-injection", "fake-ss-two", "fake-zip", "fake-acme", "fake-large", "fake-ss-24429", "fake-ss-10963",
                          "fake-ss-6459", "fake-iq"}
    two = by_id["fake-ss-two"]
    assert two["sender"] == "SuperStore Billing <billing@superstore.example>" and two["date"] == "2026-09-20T11:00:00Z"
    assert [(a["part_id"], a["eligible"]) for a in two["attachments"]] == [("1", True), ("2", True)]
    assert by_id["fake-zip"]["attachments"][0]["reason_code"] == "type"
    assert by_id["fake-large"]["attachments"][0]["reason_code"] == "too_large"
    acme = by_id["fake-acme"]["attachments"]
    assert [(a["filename"], a["inline"], a["eligible"]) for a in acme] == [("acme_logo.png", True, True), ("acme_inv_7781.pdf", False, True)]


def test_text_addressed_to_an_ai_is_flagged_and_changes_nothing_else(tmp_path):
    with gmail_api(tmp_path) as (c, app):
        body = c.post("/api/gmail/search", json={"query": "newer_than:60d"}).json()
    flagged = {m["message_id"]: m["reader_instruction_fields"] for m in body["messages"] if m["reader_instructions"]}
    assert flagged == {"fake-injection": ["subject", "snippet"]}
    injection = next(m for m in body["messages"] if m["message_id"] == "fake-injection")
    assert injection["attachments"][0]["eligible"]                     # still listed, still the user's choice, nothing pre-ticked


def test_the_default_window_hides_old_mail_unless_the_query_asks_for_it(tmp_path):
    with gmail_api(tmp_path) as (c, app):
        recent = c.post("/api/gmail/search", json={"query": "superstore"}).json()
        old = c.post("/api/gmail/search", json={"query": "superstore before:2026/01/01"}).json()
    assert "fake-old" not in {m["message_id"] for m in recent["messages"]} and recent["added_terms"][-1] == "newer_than:180d"
    assert [m["message_id"] for m in old["messages"]] == ["fake-old"]


def test_results_are_capped_and_marked_truncated(tmp_path):
    with gmail_api(tmp_path, gmail_max_results=2) as (c, app):
        body = c.post("/api/gmail/search", json={"query": "newer_than:180d"}).json()
    assert len(body["messages"]) == 2 and body["truncated"] and body["result_estimate"] == 9


def test_an_invalid_query_is_a_422_listing_the_problems_and_gmail_is_not_called(tmp_path):
    client = fake_client()
    with gmail_api(tmp_path, gmail_client=client) as (c, app):
        r = c.post("/api/gmail/search", json={"query": "in:anywhere label:x"})
    assert r.status_code == 422 and r.json()["error"] == "query_invalid" and len(r.json()["problems"]) == 2
    assert client.queries == []


def test_an_unknown_request_field_is_refused(tmp_path):
    with gmail_api(tmp_path) as (c, app):
        assert c.post("/api/gmail/search", json={"query": "x", "auto_import": True}).status_code == 422


def test_a_gmail_failure_is_a_plain_502_without_details(tmp_path):
    class Broken(type(fake_client())):
        def search(self, query, max_results):
            raise RuntimeError("socket exploded: token ya29.CANARY")
    with gmail_api(tmp_path, gmail_client=Broken(inbox_data())) as (c, app):
        r = c.post("/api/gmail/search", json={"query": "invoice"})
    assert r.status_code == 502 and r.json()["error"] == "unavailable" and "CANARY" not in r.text and "socket" not in r.text


def test_an_already_imported_attachment_is_marked_with_its_run(tmp_path):
    import sqlite3
    from contextlib import closing
    with gmail_api(tmp_path) as (c, app):
        size = (ROOT_DIR / "data" / "invoices" / "invoice_Scot Wooten_10963.pdf").stat().st_size
        with closing(sqlite3.connect(app.state.api.db_path)) as conn:
            conn.execute("INSERT INTO gmail_imports (account_email, message_id, attachment_sha256, part_id, mime_type, size_bytes, run_id) "
                         "VALUES ('fake-inbox@example.test', 'fake-ss-10963', 'x', '1', 'application/pdf', ?, 'run-earlier')", (size,))
            conn.commit()
        body = c.post("/api/gmail/search", json={"query": "10963"}).json()
    assert body["messages"][0]["attachments"][0]["imported_run_id"] == "run-earlier"


def test_the_gmail_routes_are_behind_the_access_token(tmp_path):
    with gmail_api(tmp_path, access_token="tok-123") as (c, app):
        assert c.get("/api/gmail/status").status_code == 401
        assert c.post("/api/gmail/search", json={"query": "x"}).status_code == 401
        assert c.get("/api/gmail/status", headers={"Authorization": "Bearer tok-123"}).status_code == 200


# ------------------------------------------------------------------------------------------ sessions: the candidate set

def test_a_search_session_holds_only_eligible_attachments_and_expires(tmp_path):
    now = [1000.0]
    svc = GmailService(Settings(gmail_backend="fake", gmail_search_ttl_s=60), tmp_path / "x.db", client=fake_client(),
                       clock=lambda: now[0])
    from app.db.reset import reset_database
    from tests.pipeline.helpers import DEMO
    reset_database(tmp_path / "x.db", DEMO)
    result = svc.search("after:2026/08/01")
    cands = svc.session(result.search_id).candidates
    assert ("fake-ss-two", "2") in cands and ("fake-zip", "1") not in cands and ("fake-large", "1") not in cands
    assert cands[("fake-ss-two", "2")].sender == "SuperStore Billing <billing@superstore.example>"
    now[0] += 61
    with pytest.raises(GmailError) as err:
        svc.session(result.search_id)
    assert err.value.code == "search_expired"


def test_old_sessions_are_dropped_beyond_the_cap(tmp_path, monkeypatch):
    from app.db.reset import reset_database
    from tests.pipeline.helpers import DEMO
    reset_database(tmp_path / "x.db", DEMO)
    monkeypatch.setattr(service_mod, "MAX_SESSIONS", 2)
    svc = GmailService(Settings(gmail_backend="fake"), tmp_path / "x.db", client=fake_client())
    first, second, third = (svc.search("invoice").search_id for _ in range(3))
    with pytest.raises(GmailError):
        svc.session(first)
    assert svc.session(second) and svc.session(third)


# ------------------------------------------------------------------------------------------ structure

def test_only_the_gmail_store_touches_the_gmail_tables():
    sql = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+(?:oauth_credentials|gmail_imports)\b", re.IGNORECASE)
    offenders = [str(p.relative_to(ROOT_DIR)) for p in (ROOT_DIR / "backend" / "app").rglob("*.py")
                 if sql.search(p.read_text(encoding="utf-8")) and p.name != "store.py"]
    assert offenders == []
