"""LIVE tests for the cross-check reader: real API calls that cost money. Deselected by default (`-m "not live"`) and skipped when
no ANTHROPIC_API_KEY is configured. Run with:  pytest -m live -s tests/crosscheck/test_live_crosscheck.py

Two calls, sharing ONE tracker capped at $0.10 for the whole module: (1) the crosscheck-v1 schema is accepted by the API as a strict
structured-output schema; (2) one real document (a real sample invoice PDF, as "anything else" a person might attach) is read end
to end through ingest, the reader and the grounding check. Tokens and cost are printed; no key or setting value is.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.crosscheck.prompts import document_parts
from app.crosscheck.reader import read_document
from app.extraction.prompts import PagePayload
from app.llm.budget import CostTracker
from app.llm.client import build_llm_client
from app.po.readers import read_po_document
from tests.crosscheck.helpers import NOTE_TEXT
from tests.extraction.real import real_pdf

pytestmark = pytest.mark.live
_TRACKER = CostTracker(Decimal("0.06"), Decimal("0.10"))


def _live():
    settings = Settings()                                             # reads the key from the environment / .env
    if settings.anthropic_api_key is None:
        pytest.skip("no ANTHROPIC_API_KEY configured")
    return settings, build_llm_client(settings, _TRACKER)


def _show(capsys, label, facts):
    with capsys.disabled():
        print(f"\n[live] {label}: status={facts.status} code={facts.failure_code} attempts={facts.attempts} model={facts.model} "
              f"tokens_in={facts.tokens_in} tokens_out={facts.tokens_out} cost=${facts.cost_usd:.6f} "
              f"session_total=${_TRACKER.session_spent:.6f}")
        if facts.status == "failed":
            print(f"[live] message: {facts.failure_message}")


def test_the_crosscheck_v1_schema_is_accepted_by_the_api(capsys):
    settings, client = _live()
    parts = document_parts([PagePayload(number=1, text=NOTE_TEXT)], 1)
    facts = read_document(parts, {1: NOTE_TEXT}, True, client=client, settings=settings, run_key="crosscheck-live-schema")
    _show(capsys, "schema check (typed delivery note, text only)", facts)
    assert facts.failure_code != "auth", "the API rejected the key: ANTHROPIC_API_KEY in .env may be expired"
    assert facts.failure_code != "schema_rejected", facts.failure_message
    assert facts.status == "ok" and facts.attempts == 1, facts.failure_message
    assert facts.fields["vendor_name"]["value"] and len(facts.lines) == 2
    assert any(m["kind"] == "po_number" and m["value"] == "PO-7001" for m in facts.mentions)
    with capsys.disabled():
        print(f"[live] kind={facts.document_kind} fields={ {k: (v or {}).get('value') for k, v in facts.fields.items()} }")
        print(f"[live] lines={[(l['description'], l['quantity'], l['unit'], l['unit_price'], l['amount'], l['grounding']) for l in facts.lines]}")


def test_one_real_document_is_read_end_to_end(tmp_path, capsys):
    settings, client = _live()
    settings = settings.model_copy(update={"po_drafts_dir": Path(tmp_path)})
    source = real_pdf("superstore_10963")
    doc = read_po_document(source, source.name, "livedoc", settings)
    assert doc.failure_code is None
    facts = read_document(document_parts(doc.pages, doc.total_pages), doc.page_texts, doc.text_usable, client=client,
                          settings=settings, run_key="crosscheck-live-doc")
    _show(capsys, f"real document ({doc.total_pages} page, path {doc.path})", facts)
    assert facts.status == "ok", facts.failure_message
    with capsys.disabled():
        print(f"[live] kind={facts.document_kind} fields={ {k: (v or {}).get('value') for k, v in facts.fields.items()} }")
        print(f"[live] mentions={[(m['kind'], m['label'], m['value'], m['grounding']) for m in facts.mentions]}")
        print(f"[live] lines={[(l['description'], l['quantity'], l['unit_price'], l['amount'], l['grounding']) for l in facts.lines]}")
        print(f"[live] notes={facts.notes} model_notes={facts.model_notes!r} injection={facts.injection_suspected}")
    assert facts.fields["vendor_name"] is not None and facts.lines and facts.cost_usd < Decimal("0.06")
    assert _TRACKER.session_spent <= Decimal("0.10")
