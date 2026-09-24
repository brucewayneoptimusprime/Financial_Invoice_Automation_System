"""Amount tolerance semantics (SPEC section 11). Pure integer-minor-unit arithmetic.

  B = remaining PO balance BEFORE this invoice, I = invoice amount, E = I - B (the excess).
  pct part   = floor( max(B, 0) * pct / 100 )    rounded DOWN to whole minor units: never grants an extra cent
  abs part   = abs (converted to minor units)
  allowance A = min(pct part, abs part)  for mode "lesser_of" (the default, stricter), or max(...) for "greater_of".
  "Either" and "larger-of" are the same thing as "greater_of"; "both" is the same thing as "lesser_of".
  within balance   : E <= 0
  within tolerance : E <= A   (includes within balance)
When B <= 0 the pct part is 0.
"""
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from app.money import to_minor

Mode = Literal["lesser_of", "greater_of"]


@dataclass(frozen=True)
class Tolerance:
    invoice_minor: int
    balance_minor: int
    excess_minor: int
    pct_allowance_minor: int
    abs_allowance_minor: int
    allowance_minor: int
    mode: str

    @property
    def within_balance(self) -> bool:
        return self.excess_minor <= 0

    @property
    def within_tolerance(self) -> bool:
        return self.excess_minor <= self.allowance_minor


def pct_allowance_minor(balance_minor: int, pct: float | Decimal) -> int:
    base = Decimal(max(balance_minor, 0))
    return int((base * Decimal(str(pct)) / Decimal(100)).to_integral_value(rounding=ROUND_DOWN))


def evaluate_tolerance(invoice_minor: int, balance_minor: int, pct: float | Decimal,
                       abs_amount: float | Decimal | str, mode: Mode = "lesser_of") -> Tolerance:
    pct_part = pct_allowance_minor(balance_minor, pct)
    abs_part = to_minor(Decimal(str(abs_amount)))
    if mode == "lesser_of":
        allowance = min(pct_part, abs_part)
    elif mode == "greater_of":
        allowance = max(pct_part, abs_part)
    else:
        raise ValueError(f"unknown tolerance mode {mode!r}")
    return Tolerance(invoice_minor, balance_minor, invoice_minor - balance_minor, pct_part, abs_part, allowance, mode)
