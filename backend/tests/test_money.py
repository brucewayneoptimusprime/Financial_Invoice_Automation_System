from decimal import Decimal

import pytest

from app.money import from_minor, to_minor


@pytest.mark.parametrize("value,expected", [
    ("12.34", 1234), (12.34, 1234), (Decimal("12.34"), 1234), (12, 1200), ("0.10", 10), ("-5.00", -500),
    (Decimal("5000.0"), 500000), (0, 0),
])
def test_to_minor(value, expected):
    assert to_minor(value) == expected


@pytest.mark.parametrize("bad", ["10.005", 0.1 + 0.2, Decimal("NaN"), Decimal("Infinity"), True, "abc"])
def test_to_minor_rejects_inexact_or_invalid(bad):
    with pytest.raises((ValueError, ArithmeticError)):
        to_minor(bad)


def test_from_minor_is_exact_decimal():
    assert from_minor(1234) == Decimal("12.34")
    assert from_minor(-500) == Decimal("-5.00")
    assert from_minor(0) == Decimal("0")
    assert isinstance(from_minor(1), Decimal)


@pytest.mark.parametrize("cents", [0, 1, 99, 100, 123456789, -42])
def test_round_trip(cents):
    assert to_minor(from_minor(cents)) == cents
