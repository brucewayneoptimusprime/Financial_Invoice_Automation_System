import random

import pytest

from app.config import MatchConfig
from app.engine.vendor_match import resolve_vendor
from tests.factories import make_vendor

ALPHA = make_vendor(1, "Vendor Alpha Ltd", aliases=("V. Alpha",))
BETA = make_vendor(2, "Vendor Beta Inc")
VENDORS = [ALPHA, BETA]


@pytest.mark.parametrize("name", [
    "Vendor Alpha Ltd", "VENDOR ALPHA LTD.", "vendor alpha", "Vendor  Alpha,  Limited", "Vendor-Alpha Ltd",
    "  vendor alpha ltd  ",
])
def test_exact_match_ignores_case_punctuation_spacing_and_legal_suffix(name):
    m = resolve_vendor(name, VENDORS)
    assert (m.vendor_id, m.method, m.score, m.ambiguous) == (1, "exact_name", 1.0, False)


def test_alias_match():
    m = resolve_vendor("V. ALPHA", VENDORS)
    assert (m.vendor_id, m.method, m.score) == (1, "alias", 1.0)


def test_fuzzy_match_tolerates_a_typo():
    m = resolve_vendor("Vendor Alpah Ltd", VENDORS)
    assert m.vendor_id == 1 and m.method == "fuzzy" and 0.85 <= m.score < 1.0 and not m.ambiguous


@pytest.mark.parametrize("name", ["Completely Different Trading", "Vendor", "Vend", "xyz"])
def test_unrelated_names_do_not_match(name):
    m = resolve_vendor(name, VENDORS)
    assert (m.vendor_id, m.method, m.score) == (None, "none", 0.0)


@pytest.mark.parametrize("name", [None, "", "   "])
def test_missing_name_resolves_to_nothing(name):
    assert resolve_vendor(name, VENDORS).vendor_id is None


def test_no_vendors_resolves_to_nothing():
    assert resolve_vendor("Vendor Alpha Ltd", []).vendor_id is None


def test_near_identical_vendors_are_ambiguous_not_guessed():
    vendors = [make_vendor(1, "Acme Trading Co"), make_vendor(2, "Acme Tradings Co")]
    m = resolve_vendor("Acme Tradin", vendors)
    assert m.ambiguous and m.vendor_id == 1 and m.runner_up_score is not None and m.score - m.runner_up_score < 0.05


def test_an_exact_match_is_not_ambiguous_with_a_near_miss():
    vendors = [make_vendor(1, "Acme Trading Co"), make_vendor(2, "Acme Tradings Co")]
    m = resolve_vendor("Acme Trading", vendors)
    assert (m.vendor_id, m.score, m.ambiguous) == (1, 1.0, False) and m.runner_up_score is not None


def test_two_vendors_sharing_an_alias_are_ambiguous_and_the_lowest_id_is_reported():
    vendors = [make_vendor(7, "Seven Ltd", aliases=("Shared",)), make_vendor(3, "Three Ltd", aliases=("Shared",))]
    m = resolve_vendor("shared", vendors)
    assert m.ambiguous and m.vendor_id == 3


def test_a_typo_of_a_blocked_vendor_still_resolves_to_that_vendor():
    from app.enums import VendorStatus
    vendors = [make_vendor(1, "Good Supplies Ltd"), make_vendor(3, "Bad Actor Corp", status=VendorStatus.BLOCKED)]
    assert resolve_vendor("Bad Acter Corp", vendors).vendor_id == 3


def test_non_latin_names_are_compared_not_erased():
    vendors = [make_vendor(1, "Общество Альфа")]
    assert resolve_vendor("ОБЩЕСТВО   альфа", vendors).method == "exact_name"
    assert resolve_vendor("Общество Бета Гамма", vendors).vendor_id is None


def test_legal_suffixes_come_from_config():
    vendors = [make_vendor(1, "Alpha Widgets Ltd")]
    assert resolve_vendor("Alpha Widgets", vendors).method == "exact_name"
    strict = MatchConfig(legal_suffixes=())
    assert resolve_vendor("Alpha Widgets", vendors, strict).method == "fuzzy"          # 'ltd' now matters


def test_fuzzy_threshold_comes_from_config():
    assert resolve_vendor("Vendor Alpah Ltd", VENDORS, MatchConfig(vendor_fuzzy_min=0.99)).vendor_id is None


def test_result_does_not_depend_on_vendor_order():
    vendors = [make_vendor(i, f"Supplier {n} Ltd") for i, n in enumerate(["Alpha", "Alphb", "Alphc", "Zeta"], start=1)]
    expected = resolve_vendor("Supplier Alphx", vendors)
    for seed in range(10):
        shuffled = vendors[:]
        random.Random(seed).shuffle(shuffled)
        assert resolve_vendor("Supplier Alphx", shuffled) == expected
