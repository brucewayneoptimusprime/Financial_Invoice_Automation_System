"""The single place money crosses the DB boundary.

Amounts are stored as INTEGER minor units so SQL SUM() is exact. Everything above the DB
layer uses Decimal. Assumes 2-decimal currencies only (SPEC section 11).
"""
from decimal import Decimal

MINOR_UNIT_EXPONENT = 2


def to_minor(value: Decimal | int | str | float) -> int:
    """Convert a major-unit amount to integer minor units. Raises ValueError if not exact."""
    if isinstance(value, bool):
        raise ValueError("bool is not a money amount")
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if not amount.is_finite():
        raise ValueError(f"non-finite amount: {value!r}")
    scaled = amount.scaleb(MINOR_UNIT_EXPONENT)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount {value!r} has more than {MINOR_UNIT_EXPONENT} decimal places")
    return int(scaled)


def from_minor(minor: int) -> Decimal:
    """Convert integer minor units back to a Decimal in major units (e.g. 1234 -> 12.34)."""
    return Decimal(minor).scaleb(-MINOR_UNIT_EXPONENT)
