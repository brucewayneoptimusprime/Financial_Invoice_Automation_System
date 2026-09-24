from decimal import Decimal

import pytest

from app.engine.normalize import (
    field_present, is_missing, normalize_identifier, normalize_name, normalize_text, similarity, token_similarity,
)
from app.models.extraction import EvidencedField


@pytest.mark.parametrize("value,missing", [
    (None, True), ("", True), ("   ", True), ("\t\n", True),
    (0, False), (Decimal("0"), False), (0.0, False), ("0", False), ("x", False), (False, False),
])
def test_is_missing(value, missing):
    assert is_missing(value) is missing


def test_null_is_missing_regardless_of_confidence():
    assert not field_present(EvidencedField[str](value=None, confidence=0.99))
    assert not field_present(EvidencedField[str](value=None, confidence=0.0))
    assert field_present(EvidencedField[str](value="x", confidence=0.0))
    assert not field_present(None)


def test_normalize_text_strips_case_accents_punctuation():
    assert normalize_text("  Café--Müller,  S.A. ") == "cafe muller s a"
    assert normalize_text("Общество ООО") == "общество ооо"  # non-latin scripts are kept, not erased


@pytest.mark.parametrize("a,b", [
    ("INV-0001", "inv 1"), ("INV/001", "Inv-1"), ("PO 1001", "po-1001"), ("A.B.C", "abc"),
])
def test_identifier_normalisation_equates_formatting_variants(a, b):
    assert normalize_identifier(a) == normalize_identifier(b)


@pytest.mark.parametrize("a,b", [("INV-10", "INV-1"), ("INV-100", "INV-10"), ("A-1", "B-1")])
def test_identifier_normalisation_keeps_real_differences(a, b):
    assert normalize_identifier(a) != normalize_identifier(b)


def test_identifier_zero_edge_cases():
    assert normalize_identifier("0") == "0"
    assert normalize_identifier("000") == "0"
    assert normalize_identifier("INV-0") == "inv0"
    assert normalize_identifier("INV-10") == "inv10"  # interior zeros preserved


def test_normalize_name_drops_generic_tokens_but_never_returns_empty():
    drop = ["ltd", "inc"]
    assert normalize_name("Vendor Alpha, Ltd.", drop) == "vendor alpha"
    assert normalize_name("LTD INC", drop) == "ltd inc"


def test_similarity_bounds_and_empty():
    assert similarity("abc", "abc") == 1.0
    assert similarity("abc", "xyz") == 0.0
    assert similarity("", "abc") == 0.0
    assert 0 < similarity("po1001", "po1002") < 1
    assert token_similarity("standard widget", "widget standard") == 1.0
    assert token_similarity("", "x") == 0.0
