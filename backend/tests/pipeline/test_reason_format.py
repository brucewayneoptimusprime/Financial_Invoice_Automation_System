"""Contract test (no code change): the template reason forms that the UI parses into a plain sentence + technical details.

The frontend (frontend/src/reasons.ts) strips these prefixes; if the wording in digest.py / actions.py changes, this test fails
first instead of the UI silently showing raw identifiers.
"""
import re

import pytest

from app.pipeline.actions import review_reason
from app.pipeline.digest import build_digest
from app.pipeline.explain import template_explanation
from tests.extraction.real import real_reply
from tests.extraction.wire_convert import set_field
from tests.pipeline.helpers import cfg, demo_db, run_real

RULE_FORM = re.compile(r"^(\S+) \((.*?)\) - ([A-Za-z0-9_]+), severity (\d+): ([\s\S]+)$")
FLOOR_FORM = re.compile(r"^Engine floor \(([A-Za-z0-9_]+)\): ([\s\S]+)$")
REVIEW_PART = re.compile(r"^(\S+) \(([A-Za-z0-9_]+)\): ([\s\S]+)$")


@pytest.fixture
def db(tmp_path):
    conn = demo_db(tmp_path)
    yield conn
    conn.close()


def _explained(db, tmp_path, name, reply=None):
    r = run_real(db, tmp_path, name, reply)
    rules = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM rules")}
    digest = build_digest(r.ctx, cfg(tmp_path), rules)
    return r, digest, template_explanation(digest)


@pytest.mark.parametrize("name", ["superstore_10963", "iq_electronics"])
def test_every_triggered_reason_uses_one_of_the_two_parsed_forms(db, tmp_path, name):
    _, digest, ex = _explained(db, tmp_path, name)
    assert ex.reasons
    for text, facts in ex.reasons:
        m = RULE_FORM.match(text) or FLOOR_FORM.match(text)
        assert m, text
        assert facts and all(re.fullmatch(r"F\d+", f) for f in facts)
    rule = [RULE_FORM.match(t) for t, _ in ex.reasons if RULE_FORM.match(t)]
    assert rule and all(m.group(1).startswith("r_") and m.group(4).isdigit() for m in rule)


def test_the_engine_floor_form(db, tmp_path):
    _, _, ex = _explained(db, tmp_path, "iq_electronics")
    floors = [FLOOR_FORM.match(t) for t, _ in ex.reasons if t.startswith("Engine floor")]
    assert floors and floors[0].group(1) == "required_fields_low_confidence"


def test_the_review_queue_reason_splits_into_the_parsed_parts(db, tmp_path):
    r, digest, _ = _explained(db, tmp_path, "iq_electronics")
    text = review_reason(digest, cfg(tmp_path))
    assert text.startswith("Review: ")
    parts = text[len("Review: "):].split(" | ")
    assert len(parts) >= 2
    for p in parts:
        assert FLOOR_FORM.match(p) or REVIEW_PART.match(p), p


def test_a_request_info_reason_also_matches(db, tmp_path):
    reply = real_reply("superstore_10963")
    set_field(reply, "invoice_number", found=False, value="", page=0, source_text="", confidence=0.0)
    _, _, ex = _explained(db, tmp_path, "superstore_10963", reply)
    assert all(RULE_FORM.match(t) or FLOOR_FORM.match(t) for t, _ in ex.reasons)
