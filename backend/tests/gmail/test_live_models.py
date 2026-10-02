"""The ONE live test for Plan 2 (SPEC section 11 items 88-89): one translation and one labelling call against the real model, on the
labelled FAKE inbox's metadata (no Gmail access). Deselected by default; run it with

    python -m pytest -m live -k gmail_models -s

It costs about $0.03, skips itself without ANTHROPIC_API_KEY, and prints the measured tokens and cost (never a key).
"""
from decimal import Decimal

import pytest

from app.config import Settings
from app.db.reset import reset_database
from app.extraction.eval import build_client
from app.gmail.fake import FakeGmailClient
from app.gmail.service import GmailService
from app.gmail.translate import check_translation
from app.llm.budget import CostTracker
from tests.gmail.helpers import FAKE_INBOX
from tests.pipeline.helpers import DEMO

pytestmark = pytest.mark.live


def test_gmail_models_translate_and_label_against_the_real_model(tmp_path):
    base = Settings()
    if not base.api_key_value():
        pytest.skip("ANTHROPIC_API_KEY is not set")
    settings = base.model_copy(update={"gmail_backend": "fake", "db_path": tmp_path / "live.db"})
    reset_database(settings.db_path, DEMO)
    tracker = CostTracker(Decimal("0.25"), Decimal("1.00"))
    svc = GmailService(settings, settings.db_path, client=FakeGmailClient(FAKE_INBOX), tracker=tracker,
                       llm=build_client(settings, tracker, allow_live=True), mode="live")

    result = svc.search(sentence="invoices from SuperStore since September")
    query = result.translation["query"]
    assert check_translation({"query": query, "notes": ""}, settings) == []           # validated, company name not in from:
    assert "superstore" in query.lower() and "from:superstore " not in f"{query.lower()} "

    labels = svc.labels(result.search_id)
    assert labels["fallback"] is None and labels["sent"] >= 1
    model_labels = [x for x in labels["labels"] if x["source"] == "model"]
    assert len(model_labels) >= 0.8 * labels["sent"]

    t, lab = result.cost, labels["cost"]
    total = Decimal(t["translate_usd"]) + Decimal(lab["labels_usd"])
    print(f"\nLIVE gmail models | query: {query!r} | messages: {len(result.messages)} | labelled: {labels['sent']}"
          f"\n  translate: in={t['tokens_in']} out={t['tokens_out']} ${t['translate_usd']}"
          f"\n  labels:    in={lab['tokens_in']} out={lab['tokens_out']} ${lab['labels_usd']}"
          f"\n  search total: ${total:.6f}"
          f"\n  labels: {[(x['message_id'], x['part_id'], x['label'], x['reason']) for x in labels['labels']]}")
    svc.close()
