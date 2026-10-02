"""Plan 2, stage B1: the Query translator. A sentence becomes a Gmail query that code validates (allowlist + the company-name rule);
one repair retry; a graceful fallback to the manual box. Scripted model doubles only; no network."""
import json
from decimal import Decimal

import pytest

from app.config import Settings
from app.gmail.prompts import QUERY_SYSTEM, fingerprint, query_schema
from app.gmail.translate import check_translation
from app.llm.budget import CostTracker
from app.llm.errors import LLMConfigError, ReplayMiss
from tests.gmail.helpers import fake_client, gmail_api
from tests.llm.fakes import FakeLLMClient, ok_response

S = Settings()


def reply(query, notes="Finds the emails you described."):
    return ok_response(json.dumps({"query": query, "notes": notes}), input_tokens=720, output_tokens=40)


def search(c, **body):
    return c.post("/api/gmail/search", json=body)


# ------------------------------------------------------------------------------------------ the code checks on the model's query

@pytest.mark.parametrize("query, ok", [
    ("Meridian after:2026/08/01", True),
    ("from:billing@meridian.com", True),
    ("from:meridian.com invoice", True),
    ('"Meridian Supplies" newer_than:30d', True),
    ("", True),                                                             # empty = "cannot be expressed": not a check failure
    ("from:Meridian", False),
    ('from:"Meridian Supplies"', False),
    ("to:Accounts", False),
    ("-from:Newsletter", False),
    ("in:anywhere invoice", False),
    ("label:finance", False),
])
def test_check_translation(query, ok):
    problems = check_translation({"query": query, "notes": ""}, S)
    assert (problems == []) == ok, problems
    if query.startswith(("from:M", 'from:"', "to:A", "-from:")):
        assert "plain keywords" in problems[0]


def test_a_malformed_reply_is_a_problem():
    assert check_translation({"query": 3}, S) and check_translation(["x"], S) and check_translation({"query": "x"}, S)


# ------------------------------------------------------------------------------------------ the happy path

def test_a_sentence_becomes_a_validated_query_and_a_search(tmp_path):
    model = FakeLLMClient(reply("Acme after:2026/08/01"))
    gmail = fake_client()
    with gmail_api(tmp_path, inner_client=model, mode="live", gmail_client=gmail) as (c, app):
        r = search(c, sentence="invoices from Acme since August")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["translation"] == {"sentence": "invoices from Acme since August", "query": "Acme after:2026/08/01",
                                   "notes": "Finds the emails you described."}
    assert body["query_sent"] == "Acme after:2026/08/01 has:attachment" and gmail.queries == ["Acme after:2026/08/01 has:attachment"]
    assert [m["message_id"] for m in body["messages"]] == ["fake-acme"]
    assert body["cost"] == {"translate_usd": "0.001840", "tokens_in": 720, "tokens_out": 40}   # 720 x $2 + 40 x $10 per Mtok
    [req] = model.requests
    assert req.system == QUERY_SYSTEM and req.schema == query_schema() and req.purpose == "gmail_query"
    assert req.run_id == f"gmail-search-{body['search_id']}" and req.max_output_tokens == 300
    assert len(req.parts) == 1 and json.loads(req.parts[0].text) == {"sentence": "invoices from Acme since August",
                                                                    "today": json.loads(req.parts[0].text)["today"], "timezone": "UTC"}


def test_the_translator_sees_only_the_sentence_date_and_timezone(tmp_path):
    model = FakeLLMClient(reply("Acme"))
    with gmail_api(tmp_path, inner_client=model, mode="live") as (c, app):
        search(c, sentence="Acme invoices")
    text = model.requests[0].parts[0].text
    assert set(json.loads(text)) == {"sentence", "today", "timezone"}
    assert "fake-inbox" not in text and "superstore" not in text.lower() and "billing" not in text


# ------------------------------------------------------------------------------------------ repair and fallback

def test_a_company_name_in_from_is_repaired_once(tmp_path):
    model = FakeLLMClient(reply("from:Acme after:2026/08/01"), reply("Acme after:2026/08/01"))
    with gmail_api(tmp_path, inner_client=model, mode="live") as (c, app):
        r = search(c, sentence="invoices from Acme since August")
    assert r.status_code == 200 and r.json()["translation"]["query"] == "Acme after:2026/08/01"
    assert len(model.requests) == 2 and "plain keywords" in model.requests[1].parts[1].text
    assert r.json()["cost"]["tokens_in"] == 1440                               # both attempts are counted


def test_a_query_that_still_fails_goes_to_the_manual_box_with_the_problems_and_gmail_is_not_called(tmp_path):
    model = FakeLLMClient(reply("in:anywhere invoice"), reply("in:anywhere invoice"))
    gmail = fake_client()
    with gmail_api(tmp_path, inner_client=model, mode="live", gmail_client=gmail) as (c, app):
        r = search(c, sentence="every invoice anywhere")
    body = r.json()
    assert r.status_code == 422 and body["error"] == "translation_failed" and body["reason"] == "refused"
    assert body["query"] == "in:anywhere invoice" and any("spam, trash or all mail" in p for p in body["problems"])
    assert "edit it in the Gmail search box" in body["message"] and gmail.queries == [] and body["cost"]["tokens_in"] == 1440


@pytest.mark.parametrize("script, n_requests", [
    ([ok_response("not json at all"), ok_response("still not json")], 2),
    ([LLMConfigError("no key")], 1),
    ([ReplayMiss("no recording")], 1),
])
def test_model_problems_fall_back_without_a_query(tmp_path, script, n_requests):
    model = FakeLLMClient(*script)
    gmail = fake_client()
    with gmail_api(tmp_path, inner_client=model, mode="live", gmail_client=gmail) as (c, app):
        r = search(c, sentence="invoices from Acme")
    body = r.json()
    assert r.status_code == 422 and body["error"] == "translation_failed" and body["reason"] == "unavailable"
    assert body["query"] is None and "use the Gmail search box" in body["message"]
    assert len(model.requests) == n_requests and gmail.queries == []


def test_the_cost_ceiling_falls_back_before_any_call(tmp_path):
    model = FakeLLMClient(reply("Acme"))
    with gmail_api(tmp_path, inner_client=model, mode="live", tracker=CostTracker(Decimal("0.25"), Decimal("0.0001"))) as (c, app):
        r = search(c, sentence="invoices from Acme")
    assert r.status_code == 422 and r.json()["reason"] == "unavailable" and model.requests == []


def test_an_empty_query_means_the_sentence_cannot_be_a_search(tmp_path):
    model = FakeLLMClient(reply("", notes="Gmail cannot search by invoice amount."))
    with gmail_api(tmp_path, inner_client=model, mode="live") as (c, app):
        r = search(c, sentence="invoices over 5000 dollars")
    assert r.status_code == 422 and "Gmail cannot search by invoice amount." in r.json()["message"] and len(model.requests) == 1


def test_offline_has_no_translator_and_makes_no_call(tmp_path):
    model = FakeLLMClient(reply("Acme"))
    with gmail_api(tmp_path, inner_client=model, mode="offline") as (c, app):
        status = c.get("/api/gmail/status").json()
        r = search(c, sentence="invoices from Acme")
    assert status["translator_available"] is False and status["labels_available"] is False
    assert r.status_code == 422 and r.json()["reason"] == "unavailable" and model.requests == []


def test_status_in_live_mode(tmp_path):
    with gmail_api(tmp_path, inner_client=FakeLLMClient(), mode="live") as (c, app):
        s = c.get("/api/gmail/status").json()
    assert s["translator_available"] and s["labels_available"]
    assert s["prompt_versions"] == {"query": "gmail-query-v1", "labels": "gmail-labels-v1"}


def test_the_translator_can_be_switched_off(tmp_path):
    with gmail_api(tmp_path, inner_client=FakeLLMClient(), mode="live", gmail_translator_enabled=False) as (c, app):
        assert c.get("/api/gmail/status").json()["translator_available"] is False


@pytest.mark.parametrize("body, code", [
    ({"query": "x", "sentence": "y"}, "query_invalid"),
    ({"sentence": "   "}, "query_invalid"),
    ({"sentence": "x" * 301}, "query_invalid"),
])
def test_bad_requests(tmp_path, body, code):
    model = FakeLLMClient()
    with gmail_api(tmp_path, inner_client=model, mode="live") as (c, app):
        r = search(c, **body)
    assert r.status_code == 422 and r.json()["error"] == code and model.requests == []


def test_not_connected_is_refused_before_any_model_call(tmp_path):
    model = FakeLLMClient(reply("Acme"))
    with gmail_api(tmp_path, inner_client=model, mode="live", gmail_backend="disabled") as (c, app):
        r = search(c, sentence="invoices from Acme")
    assert r.status_code == 503 and model.requests == []


def test_the_manual_query_path_is_unchanged_and_costs_nothing(tmp_path):
    model = FakeLLMClient()
    with gmail_api(tmp_path, inner_client=model, mode="live") as (c, app):
        r = search(c, query="after:2026/08/01")
        legacy = c.post("/api/gmail/search", json={})                         # an empty body still means an empty query
    assert r.status_code == 200 and r.json()["translation"] is None and model.requests == []
    assert r.json()["cost"] == {"translate_usd": "0.000000", "tokens_in": 0, "tokens_out": 0} and legacy.status_code == 200


def test_the_prompts_are_pinned():
    """Changing a Gmail prompt or schema must be deliberate: update this fingerprint (and the prompt version) together."""
    assert fingerprint() == PINNED


PINNED = "4ed180f34c599b35bc061045ac121264819ca28915c5e827f4e8b9a8479b4caf"
