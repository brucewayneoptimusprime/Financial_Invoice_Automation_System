"""PO candidate scoring, ranking and ambiguity. Hand-written data only."""
import random
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import MatchConfig
from app.engine.facts import POLineFact
from app.engine.matching import (
    amount_signal, classify, lines_signal, rank_po_candidates, reference_signal, score_candidate, vendor_signal,
)
from app.engine.vendor_match import resolve_vendor
from app.enums import MatchStatus
from app.models import POCandidate
from app.models.run import VendorMatch
from tests.factories import field, make_extracted, make_facts, make_po, make_vendor

CFG = MatchConfig()
D = Decimal
EXACT_VENDOR = VendorMatch(vendor_id=1, score=1.0, method="exact_name")


def rank(extracted=None, pos=None, vm=EXACT_VENDOR, cfg=CFG, **kw):
    facts = make_facts(pos=pos if pos is not None else [make_po()])
    return rank_po_candidates(extracted or make_extracted(), vm, facts, cfg, **kw)


def pc(score, n="P"):
    return POCandidate(po_id=1, po_number=n, score=score)


# ------------------------------------------------------------------------------ reference signal

def test_exact_explicit_reference():
    s, reasons = reference_signal(make_extracted(), make_po(), CFG)
    assert s == 1.0 and reasons == ["reference:exact"]


@pytest.mark.parametrize("ref", ["po a 1", "PO/A/1", "po-a-1", "PO A1", " PO-A-1 "])
def test_reference_formatting_variants_are_exact(ref):
    assert reference_signal(make_extracted(po_reference=ref), make_po(), CFG)[0] == 1.0


def test_leading_zeros_do_not_break_an_exact_reference():
    assert reference_signal(make_extracted(po_reference="PO-0001"), make_po(po_number="PO-1"), CFG)[0] == 1.0


def test_contained_reference_scores_the_configured_amount():
    s, reasons = reference_signal(make_extracted(po_reference="1001"), make_po(po_number="PO-1001"), CFG)
    assert s == CFG.reference_contained_score and reasons == ["reference:contained"]


def test_very_short_contained_reference_is_not_trusted():
    assert reference_signal(make_extracted(po_reference="1"), make_po(po_number="PO-A-1"), CFG)[0] == 0.0


def test_fuzzy_reference_is_discounted():
    s, reasons = reference_signal(make_extracted(po_reference="P0-1001"), make_po(po_number="PO-1001"), CFG)
    assert 0 < s <= CFG.reference_fuzzy_factor and reasons[0].startswith("reference:fuzzy")


def test_unrelated_reference_scores_zero():
    assert reference_signal(make_extracted(po_reference="ZZZ-9"), make_po(), CFG) == (0.0, ["reference:none"])


@pytest.mark.parametrize("ref", [None, field(None, 0.99)])
def test_null_reference_scores_zero_whatever_the_confidence(ref):
    assert reference_signal(make_extracted(po_reference=ref), make_po(), CFG) == (0.0, ["reference:none"])


@pytest.mark.parametrize("explicit", [False, None])
def test_inferred_or_unknown_explicitness_halves_the_reference(explicit):
    ex = make_extracted(po_reference={"value": "PO-A-1", "explicit": explicit, "confidence": 0.9})
    s, reasons = reference_signal(ex, make_po(), CFG)
    assert s == CFG.inferred_reference_factor and reasons == ["reference:exact", "reference:inferred"]


# ------------------------------------------------------------------------------ vendor signal

def test_vendor_signal():
    po = make_po(vendor_id=1)
    assert vendor_signal(EXACT_VENDOR, po) == (1.0, ["vendor:match"])
    assert vendor_signal(VendorMatch(vendor_id=2, score=1.0, method="exact_name"), po) == (0.0, ["vendor:mismatch"])
    assert vendor_signal(VendorMatch(vendor_id=None, method="none"), po) == (0.0, ["vendor:unresolved"])
    assert vendor_signal(None, po) == (0.0, ["vendor:unresolved"])
    assert vendor_signal(VendorMatch(vendor_id=1, score=0.9, method="fuzzy", ambiguous=True), po)[0] == pytest.approx(0.45)


# ------------------------------------------------------------------------------ amount signal

@pytest.mark.parametrize("total,expected", [
    ("1200.00", 1.0), ("600.00", 0.75), ("1.00", 0.5 + 0.5 / 1200), ("1224.00", 0.98), ("2400.00", 0.0), ("3000.00", 0.0),
])
def test_amount_signal_against_the_remaining_balance(total, expected):
    s, _ = amount_signal(make_extracted(total=total), make_po(total="1200.00"), "total")
    assert s == pytest.approx(expected)


def test_amount_uses_the_ledger_derived_balance():
    po = make_po(total="1200.00", net_committed="900.00")             # balance 300
    assert amount_signal(make_extracted(total="300.00"), po, "total")[0] == 1.0
    assert amount_signal(make_extracted(total="1200.00"), po, "total")[0] == 0.0


@pytest.mark.parametrize("extracted,po", [
    (make_extracted(total=None), make_po()),
    (make_extracted(total=field(None, 0.99)), make_po()),
    (make_extracted(currency="EUR"), make_po()),
    (make_extracted(currency=None), make_po()),
    (make_extracted(total="10.005"), make_po()),
    (make_extracted(total="-5.00"), make_po()),
    (make_extracted(total="0.00"), make_po()),
    (make_extracted(), make_po(total="100.00", net_committed="100.00")),      # nothing left
    (make_extracted(), make_po(total="100.00", net_committed="150.00")),      # over-billed already
])
def test_amount_signal_is_zero_when_not_comparable(extracted, po):
    assert amount_signal(extracted, po, "total")[0] == 0.0


def test_amount_compare_field_is_configurable():
    ex = make_extracted(total="2400.00", subtotal="1200.00")             # total is 100% over the balance
    po = make_po(total="1200.00")
    assert amount_signal(ex, po, "total")[0] == 0.0 and amount_signal(ex, po, "subtotal")[0] == 1.0
    assert amount_signal(make_extracted(total="2000.00"), po, "total")[0] == pytest.approx(1 / 3)   # 66.7% over -> 0.333


# ------------------------------------------------------------------------------ lines signal

def _lines(*descs):
    return tuple(POLineFact(line_no=i, description=d, unit_price=D(p)) for i, (d, p) in enumerate(descs, start=1))


def _inv(*descs):
    return make_extracted(line_items=[{"description": d, "quantity": 1, "unit_price": p, "amount": p, "confidence": 0.9}
                                      for d, p in descs])


def test_identical_lines_score_one():
    assert lines_signal(make_extracted(), make_po(), CFG)[0] == pytest.approx(1.0)


def test_line_order_and_word_order_do_not_matter():
    po = make_po(lines=_lines(("blue widget large", "5.00"), ("red gadget", "7.00")))
    ex = _inv(("Gadget, Red", "7.00"), ("Large blue widget", "5.00"))
    assert lines_signal(ex, po, CFG)[0] == pytest.approx(1.0)


def test_price_disagreement_costs_the_price_share():
    po = make_po(lines=_lines(("blue widget", "5.00")))
    assert lines_signal(_inv(("blue widget", "6.00")), po, CFG)[0] == pytest.approx(CFG.line_desc_weight)


def test_only_some_lines_overlapping_gives_partial_credit():
    po = make_po(lines=_lines(("blue widget", "5.00")))
    assert lines_signal(_inv(("blue widget", "5.00"), ("unrelated service fee", "9.00")), po, CFG)[0] == pytest.approx(0.5)


def test_no_usable_lines_scores_zero():
    po = make_po(lines=_lines(("blue widget", "5.00")))
    assert lines_signal(make_extracted(line_items=[]), po, CFG)[0] == 0.0
    assert lines_signal(_inv(("blue widget", "5.00")), make_po(lines=()), CFG)[0] == 0.0
    assert lines_signal(_inv(("totally different thing", "5.00")), po, CFG)[0] == 0.0
    blank = make_extracted(line_items=[{"description": None, "quantity": 1, "unit_price": "5.00", "amount": "5.00"}])
    assert lines_signal(blank, po, CFG)[0] == 0.0


# ------------------------------------------------------------------------------ candidate score

def test_a_perfect_candidate_has_a_transparent_score_breakdown():
    c = rank().candidates[0]
    assert c.breakdown == {"reference": 0.4, "vendor": 0.25, "amount": pytest.approx(0.191667, abs=1e-6), "lines": 0.15}
    assert c.score == pytest.approx(0.991667, abs=1e-6) and c.score == pytest.approx(sum(c.breakdown.values()), abs=1e-5)
    assert c.reasons[:2] == ["reference:exact", "vendor:match"] and any(r.startswith("amount:fits") for r in c.reasons)


def test_a_po_with_no_evidence_is_not_a_candidate_even_if_the_amount_fits():
    other = make_po(id=2, po_number="PO-Z-9", vendor_id=2, lines=())
    ex = make_extracted(po_reference="UNRELATED", line_items=[])
    assert score_candidate(ex, EXACT_VENDOR, other, CFG) is None
    assert rank(ex, pos=[other]).status is MatchStatus.NO_CANDIDATES


def test_weights_come_from_config():
    heavy = MatchConfig(weight_reference=0.7, weight_vendor=0.1, weight_amount=0.1, weight_lines=0.1)
    c = rank(cfg=heavy).candidates[0]
    assert c.breakdown["reference"] == pytest.approx(0.7) and c.breakdown["vendor"] == pytest.approx(0.1)


def test_weights_must_sum_to_one_and_thresholds_are_bounded():
    with pytest.raises(ValidationError):
        MatchConfig(weight_reference=0.5)
    with pytest.raises(ValidationError):
        MatchConfig(min_score=1.5)
    with pytest.raises(ValidationError):
        MatchConfig(unknown_option=1)


# ------------------------------------------------------------------------------ status and ranking

def test_exact_reference_vendor_amount_lines_is_a_confident_match():
    r = rank()
    assert r.status is MatchStatus.MATCHED and r.matched.po_number == "PO-A-1"


def test_no_reference_can_still_match_on_vendor_amount_and_lines():
    r = rank(make_extracted(po_reference=None))
    assert r.status is MatchStatus.MATCHED and r.matched.score == pytest.approx(0.591667, abs=1e-6)
    assert r.matched.reasons[0] == "reference:none"


def test_no_reference_and_weak_other_signals_is_low_score():
    r = rank(make_extracted(po_reference=None, line_items=[]))
    assert r.status is MatchStatus.LOW_SCORE and r.matched is None and r.candidates[0].score < CFG.min_score


def test_different_vendor_no_reference_no_lines_is_no_candidates():
    r = rank(make_extracted(po_reference=None, line_items=[]), vm=VendorMatch(vendor_id=9, score=1.0, method="exact_name"))
    assert r.status is MatchStatus.NO_CANDIDATES and r.candidates == []


def test_reference_to_a_po_of_another_vendor_still_becomes_the_top_candidate():
    """The vendor mismatch is a RULE's business (r_vendor_po_mismatch); matching just reports the best PO."""
    r = rank(vm=VendorMatch(vendor_id=2, score=1.0, method="exact_name"))
    assert r.status is MatchStatus.MATCHED and r.matched.breakdown["vendor"] == 0.0


def test_two_equally_plausible_pos_are_ambiguous_and_none_is_chosen():
    pos = [make_po(id=1, po_number="PO-A-1"), make_po(id=2, po_number="PO-A-2")]
    r = rank(make_extracted(po_reference=None), pos=pos)
    assert r.status is MatchStatus.AMBIGUOUS and r.matched is None
    assert [c.po_number for c in r.candidates] == ["PO-A-1", "PO-A-2"] and r.candidates[0].score == r.candidates[1].score


def test_an_explicit_reference_breaks_the_tie():
    pos = [make_po(id=1, po_number="PO-A-1"), make_po(id=2, po_number="PO-A-2")]
    r = rank(make_extracted(po_reference="PO-A-2"), pos=pos)
    assert r.status is MatchStatus.MATCHED and r.matched.po_number == "PO-A-2"


def test_sequential_po_numbers_do_not_confuse_an_exact_reference():
    pos = [make_po(id=1, po_number="PO-100001"), make_po(id=2, po_number="PO-100002")]
    r = rank(make_extracted(po_reference="PO-100002"), pos=pos)
    assert r.status is MatchStatus.MATCHED and r.matched.po_number == "PO-100002"
    assert r.candidates[1].reasons[0].startswith("reference:fuzzy") and r.matched.score - r.candidates[1].score > CFG.ambiguity_margin


def test_a_partially_consumed_po_wins_when_the_invoice_fits_only_its_balance():
    pos = [make_po(id=1, po_number="PO-A-1", total="5000.00", net_committed="4400.00"),      # balance 600
           make_po(id=2, po_number="PO-A-2", total="5000.00", net_committed="0.00")]         # balance 5000
    r = rank(make_extracted(po_reference=None, total="600.00", subtotal="600.00", tax="0.00", line_items=[]), pos=pos)
    assert r.candidates[0].po_number == "PO-A-1" and r.candidates[0].breakdown["amount"] > r.candidates[1].breakdown["amount"]


@pytest.mark.parametrize("scores,expected", [
    ([], MatchStatus.NO_CANDIDATES),
    ([0.5], MatchStatus.MATCHED),                     # exactly the minimum is confident
    ([0.499999], MatchStatus.LOW_SCORE),
    ([0.9, 0.5], MatchStatus.MATCHED),
    ([0.6, 0.5], MatchStatus.MATCHED),                # gap exactly 0.10 is NOT ambiguous
    ([0.6, 0.501], MatchStatus.AMBIGUOUS),            # gap 0.099
    ([0.6, 0.6], MatchStatus.AMBIGUOUS),
    ([0.6, 0.399], MatchStatus.MATCHED),              # runner-up below the ambiguity floor
    ([0.45, 0.45], MatchStatus.LOW_SCORE),            # low score outranks ambiguity
])
def test_classification_boundaries(scores, expected):
    assert classify([pc(s, f"P{i}") for i, s in enumerate(scores)], CFG) is expected


def test_ambiguity_thresholds_come_from_config():
    cfg = MatchConfig(ambiguity_margin=0.02)
    assert classify([pc(0.6), pc(0.55)], cfg) is MatchStatus.MATCHED
    cfg = MatchConfig(ambiguity_min_score=0.45)
    assert classify([pc(0.52), pc(0.44)], cfg) is MatchStatus.MATCHED
    assert classify([pc(0.6)], MatchConfig(min_score=0.7)) is MatchStatus.LOW_SCORE


def test_ranking_is_deterministic_regardless_of_po_order_and_ties_break_by_po_number():
    pos = [make_po(id=i, po_number=f"PO-A-{i}") for i in range(1, 9)]
    expected = rank(make_extracted(po_reference=None), pos=pos)
    assert [c.po_number for c in expected.candidates] == ["PO-A-1", "PO-A-2", "PO-A-3", "PO-A-4", "PO-A-5"]   # capped at 5
    for seed in range(10):
        shuffled = pos[:]
        random.Random(seed).shuffle(shuffled)
        assert rank(make_extracted(po_reference=None), pos=shuffled) == expected


def test_max_candidates_is_configurable():
    pos = [make_po(id=i, po_number=f"PO-A-{i}") for i in range(1, 5)]
    assert len(rank(make_extracted(po_reference=None), pos=pos, cfg=MatchConfig(max_candidates=2)).candidates) == 2


def test_missing_extraction_matches_nothing():
    assert rank_po_candidates(None, EXACT_VENDOR, make_facts(), CFG).status is MatchStatus.NO_CANDIDATES


def test_vendor_resolution_feeds_the_vendor_signal():
    facts = make_facts(vendors=[make_vendor(1, "Vendor Alpha Ltd"), make_vendor(2, "Vendor Beta Inc")])
    vm = resolve_vendor("VENDOR ALPHA", facts.vendors, CFG)
    assert rank_po_candidates(make_extracted(), vm, facts, CFG).matched.breakdown["vendor"] == 0.25
