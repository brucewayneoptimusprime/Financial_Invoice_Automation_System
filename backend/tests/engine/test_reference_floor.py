"""Reference floor: a PO reached through a PO reference that is NOT an exact normalised match can never be
approved automatically. It is an engine floor (not a rule), shown as its own `engine_floor_reference` result."""
import pytest

from app.engine.floor import REFERENCE_FLOOR_RULE_ID
from app.enums import Decision, MatchStatus, Outcome
from tests.engine.real import BUILTIN, pipeline
from tests.factories import field, make_extracted, make_facts, make_po

FLOOR = REFERENCE_FLOOR_RULE_ID


def two_sibling_pos():
    """Two real POs of the same vendor whose numbers differ by one character."""
    return make_facts(pos=[make_po(id=1, po_number="PO-100001"), make_po(id=2, po_number="PO-100002")])


def one_po():
    return make_facts(pos=[make_po(id=1, po_number="PO-100001")])


def applied(res):
    return res[FLOOR].outcome is Outcome.FLAG


# ------------------------------------------------------------------------------ the two required scenarios

def test_reference_one_character_off_a_real_po_when_a_sibling_po_exists_is_never_approved():
    """PO-100011 is one character from PO-100001 (the other real PO, PO-100002, is further away)."""
    ctx, res, _ = pipeline(make_extracted(po_reference="PO-100011"), two_sibling_pos())
    assert ctx.matched_po.po_number == "PO-100001" and ctx.match_status is MatchStatus.MATCHED   # everything else fits
    assert applied(res) and res[FLOOR].severity == 1
    assert "reference PO-100011 resembles PO PO-100001" in res[FLOOR].message
    assert ctx.decision is Decision.REVIEW


def test_non_existent_reference_one_character_off_a_real_po_is_never_approved():
    ctx, res, _ = pipeline(make_extracted(po_reference="PO-100009"), one_po())
    assert ctx.matched_po.po_number == "PO-100001"
    assert applied(res) and "reference PO-100009 resembles PO PO-100001" in res[FLOOR].message
    assert res[FLOOR].detail["reference_match"].startswith("reference:fuzzy") and res[FLOOR].detail["matched"] is True
    assert ctx.decision is Decision.REVIEW


def test_without_the_floor_these_would_have_been_approved():
    """Guards the guard: with the reference floor removed from the picture, every other rule passes."""
    ctx, res, _ = pipeline(make_extracted(po_reference="PO-100009"), one_po())
    others = {k: v.outcome for k, v in res.items() if k != FLOOR}
    assert all(o is Outcome.PASS for o in others.values()), others


# ------------------------------------------------------------------------------ exact normalised references are unaffected

@pytest.mark.parametrize("ref", ["PO-100001", "po 100001", "PO/100001", " po-100001 ", "PO-0100001", "P.O.100001"])
def test_exact_normalised_references_are_unaffected(ref):
    ctx, res, _ = pipeline(make_extracted(po_reference=ref), one_po())
    assert not applied(res) and res[FLOOR].outcome_key == "reference_floor_not_applied"
    assert ctx.decision is Decision.APPROVE


def test_an_exact_reference_to_one_sibling_is_unaffected_by_the_other_sibling():
    ctx, res, _ = pipeline(make_extracted(po_reference="PO-100002"), two_sibling_pos())
    assert ctx.matched_po.po_number == "PO-100002"
    assert any(r.startswith("reference:fuzzy") for c in ctx.candidates if c.po_number == "PO-100001" for r in c.reasons)
    assert not applied(res) and ctx.decision is Decision.APPROVE


def test_an_exact_but_inferred_reference_is_still_exact():
    ex = make_extracted(po_reference={"value": "PO-100001", "explicit": False, "confidence": 0.9})
    ctx, res, _ = pipeline(ex, one_po())
    assert not applied(res) and ctx.match_status is MatchStatus.MATCHED


@pytest.mark.parametrize("ref", [None, field(None, 0.99)])
def test_no_reference_at_all_is_not_a_reference_match(ref):
    ctx, res, _ = pipeline(make_extracted(po_reference=ref), one_po())
    assert not applied(res) and ctx.decision is Decision.APPROVE


# ------------------------------------------------------------------------------ other inexact forms and situations

def test_a_contained_reference_is_inexact_too():
    ctx, res, _ = pipeline(make_extracted(po_reference="100001"), one_po())
    assert applied(res) and "reference 100001 resembles PO PO-100001" in res[FLOOR].message
    assert res[FLOOR].detail["reference_match"] == "reference:contained" and ctx.decision is Decision.REVIEW


def test_floor_applies_to_the_top_candidate_even_when_nothing_was_matched():
    ex = make_extracted(po_reference="PO-100009", vendor_name="Nobody Known Trading", line_items=[])
    ctx, res, _ = pipeline(ex, one_po())
    assert ctx.match_status is MatchStatus.LOW_SCORE and ctx.matched_po is None
    assert applied(res) and res[FLOOR].detail["matched"] is False and "resembles PO PO-100001" in res[FLOOR].message


def test_floor_holds_with_every_builtin_rule_disabled():
    off = [r.model_copy(update={"enabled": False}) for r in BUILTIN.values()]
    ctx, res, (_, validate, _) = pipeline(make_extracted(po_reference="PO-100009"), one_po(), rules=off)
    assert applied(res) and validate.outputs["final_severity"] >= 1 and ctx.decision is Decision.REVIEW


def test_floor_is_its_own_result_and_event_and_never_lowers_a_higher_severity():
    ctx, res, (_, validate, _) = pipeline(make_extracted(po_reference="PO-100009", total="5000.00"), one_po())
    events = [e for e in validate.events if e.rule_id == FLOOR]
    assert len(events) == 1 and events[0].event_type == "engine_floor" and events[0].outcome is Outcome.FLAG
    assert FLOOR in validate.outputs["triggered_rule_ids"] and "engine_floor" in res
    assert res["r_tolerance_pct"].outcome_key == "over_tolerance" and ctx.decision is Decision.REVIEW


def test_floor_is_deterministic():
    runs = [pipeline(make_extracted(po_reference="PO-100011"), two_sibling_pos()) for _ in range(3)]
    dumps = [[r.model_dump(mode="json") for r in ctx.rule_results] for ctx, _, _ in runs]
    assert dumps[0] == dumps[1] == dumps[2]


# ------------------------------------------------------------------------------ the truthful po_found message

@pytest.mark.parametrize("ref,fragment", [
    ("PO-100001", "by its explicit reference"),
    ("PO-100009", "only resembles it"),
    ("ZZZ-9", "matches no PO"),
])
def test_po_found_describes_how_the_po_was_actually_found(ref, fragment):
    _, res, _ = pipeline(make_extracted(po_reference=ref), one_po())
    assert fragment in res["r_po_found"].message


def test_known_gap_a_stated_reference_that_matches_no_po_is_not_floored():
    """DOCUMENTED GAP (see STATUS.md, decision 1): a reference resembling nothing, with the PO matched on
    vendor + amount + lines alone, is neither floored nor flagged. Change this test if that is decided otherwise."""
    ctx, res, _ = pipeline(make_extracted(po_reference="ZZZ-9"), one_po())
    assert ctx.matched_po.po_number == "PO-100001" and not applied(res)
    assert ctx.candidates[0].reasons[0] == "reference:none"
