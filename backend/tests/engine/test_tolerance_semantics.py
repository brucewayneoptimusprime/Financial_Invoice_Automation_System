"""Tolerance semantics (SPEC section 11), the ledger convention for approvals, and PO status derivation.

  B = remaining balance before the invoice, I = invoice amount, E = I - B.
  A = min(floor(max(B,0) * pct/100), abs)  [lesser_of, default]   or   max(...)  [greater_of]
  within balance: E <= 0    within tolerance: E <= A
"""
import random
from decimal import Decimal

import pytest

from app.db.queries import get_po_balance_minor
from app.engine.loader import load_facts
from app.engine.po_status import commit_entry, derive_po_status, reversal_entry
from app.engine.tolerance import evaluate_tolerance, pct_allowance_minor
from app.enums import LedgerType, Outcome, POStatus
from app.money import from_minor, to_minor
from tests.engine.real import ev_builtin
from tests.factories import make_ctx, make_extracted

M = to_minor  # major -> minor shorthand


def tol(invoice, balance, pct=2.0, abs_=50.0, mode="lesser_of"):
    return evaluate_tolerance(M(invoice), M(balance), pct, abs_, mode)


# ------------------------------------------------------------------------------ pure allowance maths

def test_pct_allowance_is_a_percentage_of_the_remaining_balance():
    assert pct_allowance_minor(M("1000.00"), 2.0) == M("20.00")
    assert pct_allowance_minor(M("1000.00"), 0.1) == M("1.00")
    assert pct_allowance_minor(M("1000.00"), 0) == 0


def test_pct_allowance_rounds_down_to_whole_cents_never_up():
    assert pct_allowance_minor(M("333.33"), 2.0) == 666            # 666.66 minor units -> 6.66, not 6.67
    assert pct_allowance_minor(M("0.99"), 2.0) == 1                # 1.98 -> 1
    assert pct_allowance_minor(M("0.49"), 2.0) == 0                # 0.98 -> 0: tiny balances get no pct allowance


@pytest.mark.parametrize("balance,lesser,greater", [
    ("1000.00", "20.00", "50.00"),       # pct part 20 < abs 50
    ("10000.00", "50.00", "200.00"),     # pct part 200 > abs 50
    ("2500.00", "50.00", "50.00"),       # pct part == abs
])
def test_lesser_of_and_greater_of(balance, lesser, greater):
    assert from_minor(tol("0.01", balance, mode="lesser_of").allowance_minor) == Decimal(lesser)
    assert from_minor(tol("0.01", balance, mode="greater_of").allowance_minor) == Decimal(greater)


def test_an_excess_is_within_tolerance_exactly_up_to_the_allowance():
    for excess_cents, expected in [(0, True), (1, True), (1999, True), (2000, True), (2001, False), (5000, False)]:
        t = evaluate_tolerance(M("1000.00") + excess_cents, M("1000.00"), 2.0, 50.0, "lesser_of")
        assert t.excess_minor == excess_cents and t.within_tolerance is expected, excess_cents
    assert tol("1020.00", "1000.00").within_tolerance and not tol("1020.01", "1000.00").within_tolerance


def test_within_balance_is_always_within_tolerance():
    for inv in ("0.01", "500.00", "1000.00"):
        t = tol(inv, "1000.00", pct=0, abs_=0)
        assert t.within_balance and t.within_tolerance


@pytest.mark.parametrize("balance", ["0.00", "-15.00", "-1000.00"])
def test_no_remaining_balance_means_no_percentage_allowance(balance):
    t = tol("10.00", balance)
    assert t.pct_allowance_minor == 0 and t.allowance_minor == 0 and not t.within_tolerance      # lesser_of
    g = tol("10.00", balance, mode="greater_of")
    assert g.pct_allowance_minor == 0 and g.allowance_minor == M("50.00")                        # abs still applies
    assert g.excess_minor == M("10.00") - M(balance)


def test_zero_settings_allow_no_overrun_at_all():
    assert not tol("1000.01", "1000.00", pct=0, abs_=0).within_tolerance
    assert not tol("1000.01", "1000.00", pct=0, abs_=50.0).within_tolerance          # lesser_of: min(0, 50) = 0
    assert tol("1000.01", "1000.00", pct=0, abs_=50.0, mode="greater_of").within_tolerance


def test_either_is_greater_of_and_both_is_lesser_of():
    """'within if excess <= pct OR excess <= abs' == greater_of; 'within if excess <= pct AND <= abs' == lesser_of."""
    rng = random.Random(11)
    for _ in range(500):
        balance, inv = rng.randint(0, 2_000_000), rng.randint(1, 2_100_000)
        pct, abs_ = rng.choice([0, 0.5, 1, 2, 5, 10]), rng.choice([0, 1.0, 25.5, 50.0, 500.0])
        pct_part, abs_part, excess = pct_allowance_minor(balance, pct), M(str(abs_)), inv - balance
        either = excess <= pct_part or excess <= abs_part
        both = excess <= pct_part and excess <= abs_part
        assert evaluate_tolerance(inv, balance, pct, abs_, "greater_of").within_tolerance is either
        assert evaluate_tolerance(inv, balance, pct, abs_, "lesser_of").within_tolerance is both


def test_allowance_is_never_negative_and_greater_of_is_never_stricter():
    rng = random.Random(5)
    for _ in range(300):
        b, i = rng.randint(-50_000, 1_000_000), rng.randint(1, 1_000_000)
        lo, hi = evaluate_tolerance(i, b, 2.0, 50.0, "lesser_of"), evaluate_tolerance(i, b, 2.0, 50.0, "greater_of")
        assert 0 <= lo.allowance_minor <= hi.allowance_minor
        assert (not lo.within_tolerance) or hi.within_tolerance


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        evaluate_tolerance(100, 100, 2.0, 50.0, "either")
    with pytest.raises(ValueError):
        evaluate_tolerance(100, 100, 2.0, 0.005, "lesser_of")           # sub-cent absolute limit


# ------------------------------------------------------------------------------ the tolerance RULE at the boundaries

def rule_result(invoice, balance_total, committed="0.00", **overrides):
    from tests.factories import make_facts, make_po
    ctx = make_ctx(extracted=make_extracted(total=invoice), facts=make_facts(pos=[make_po(total=balance_total, net_committed=committed)]))
    return ev_builtin("r_tolerance_pct", ctx, **overrides)


def test_rule_boundary_one_cent_either_side_of_the_allowance():
    at = rule_result("1020.00", "1000.00")
    over = rule_result("1020.01", "1000.00")
    assert (at.outcome, at.outcome_key) == (Outcome.PASS, "within_tolerance")
    assert (over.outcome, over.outcome_key, over.severity) == (Outcome.FLAG, "over_tolerance", 1)
    assert at.detail["allowance"] == over.detail["allowance"] == "20.00" and over.detail["excess"] == "20.01"


def test_rule_uses_the_lesser_by_default_and_the_greater_when_configured():
    assert rule_result("1030.00", "1000.00").outcome_key == "over_tolerance"                       # allowance 20
    assert rule_result("1030.00", "1000.00", mode="greater_of").outcome_key == "within_tolerance"  # allowance 50


def test_rule_on_a_fully_consumed_po_flags_any_further_billing_in_lesser_of_mode():
    assert rule_result("0.01", "1000.00", committed="1000.00").outcome_key == "over_tolerance"
    assert rule_result("0.01", "1000.00", committed="1015.00").detail["balance"] == "-15.00"


# ------------------------------------------------------------------------------ PO status derivation

@pytest.mark.parametrize("total,net,current,expected", [
    (100000, 0, POStatus.OPEN, POStatus.OPEN),
    (100000, 1, POStatus.OPEN, POStatus.PARTIALLY_BILLED),
    (100000, 99999, POStatus.OPEN, POStatus.PARTIALLY_BILLED),
    (100000, 100000, POStatus.PARTIALLY_BILLED, POStatus.FULLY_BILLED),
    (100000, 101500, POStatus.PARTIALLY_BILLED, POStatus.FULLY_BILLED),        # slightly over: still fully billed
    (100000, 0, POStatus.FULLY_BILLED, POStatus.OPEN),                          # a full reversal reopens it
    (100000, 40000, POStatus.FULLY_BILLED, POStatus.PARTIALLY_BILLED),          # a partial reversal
    (100000, -500, POStatus.PARTIALLY_BILLED, POStatus.OPEN),                   # more reversed than committed: not billed
    (100000, 100000, POStatus.CLOSED, POStatus.CLOSED),                         # closed is human-only, never auto-changed
    (100000, 0, POStatus.CLOSED, POStatus.CLOSED),
])
def test_derive_po_status(total, net, current, expected):
    assert derive_po_status(total, net, current) is expected


def test_ledger_entry_conventions():
    assert commit_entry(101500) == (101500, LedgerType.COMMIT)                 # the FULL invoice amount, never capped
    assert reversal_entry(101500) == (-101500, LedgerType.REVERSAL)
    for bad in (0, -1):
        with pytest.raises(ValueError):
            commit_entry(bad)
        with pytest.raises(ValueError):
            reversal_entry(bad)


# ------------------------------------------------------------------------------ end to end through the database

@pytest.fixture
def po_db(conn):
    """One approved vendor, one PO of 1000.00, and three empty invoices to hang ledger rows on."""
    with conn:
        conn.execute("INSERT INTO vendors (id, name, status) VALUES (1, 'Vendor Alpha Ltd', 'approved')")
        conn.execute("INSERT INTO purchase_orders (id, po_number, vendor_id, currency, total_amount, status) "
                     "VALUES (1, 'PO-A-1', 1, 'USD', ?, 'open')", (M("1000.00"),))
        for i in (1, 2, 3):
            conn.execute("INSERT INTO invoices (id, vendor_id, po_id, status) VALUES (?, 1, 1, 'pending')", (i,))
    return conn


def write(conn, invoice_id, entry):
    amount, kind = entry
    with conn:
        conn.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (1, ?, ?, ?)",
                     (invoice_id, amount, kind.value))


def snapshot(conn):
    po = load_facts(conn).purchase_orders[0]
    derived = derive_po_status(M(po.total_amount), M(po.net_committed), po.status)
    return po, derived


def tolerance_for(conn, total):
    facts = load_facts(conn)
    ctx = make_ctx(extracted=make_extracted(total=total), facts=facts)
    return ev_builtin("r_tolerance_pct", ctx)


def test_facts_balance_always_agrees_with_the_m0_ledger_query(po_db):
    write(po_db, 1, commit_entry(M("400.00")))
    write(po_db, 2, commit_entry(M("100.10")))
    write(po_db, 2, reversal_entry(M("100.10")))
    po, _ = snapshot(po_db)
    assert M(po.balance) == get_po_balance_minor(po_db, 1) == M("600.00")


def test_partial_billing_then_exact_balance_then_fully_billed(po_db):
    po, status = snapshot(po_db)
    assert (po.balance, status) == (Decimal("1000.00"), POStatus.OPEN)

    write(po_db, 1, commit_entry(M("400.00")))
    po, status = snapshot(po_db)
    assert (po.balance, status) == (Decimal("600.00"), POStatus.PARTIALLY_BILLED)
    assert tolerance_for(po_db, "600.00").outcome_key == "within_balance"           # exactly the remaining balance

    write(po_db, 2, commit_entry(M("600.00")))
    po, status = snapshot(po_db)
    assert (po.balance, status) == (Decimal("0.00"), POStatus.FULLY_BILLED)


def test_approving_slightly_over_balance_commits_the_full_amount_and_goes_negative(po_db):
    write(po_db, 1, commit_entry(M("900.00")))                                       # balance 100
    result = tolerance_for(po_db, "115.00")                                           # excess 15, allowance min(2, 50) = 2 ...
    assert result.outcome_key == "over_tolerance"                                     # ... 2% of 100.00 is only 2.00
    result = tolerance_for(po_db, "101.50")                                           # excess 1.50 <= 2.00
    assert (result.outcome, result.outcome_key) == (Outcome.PASS, "within_tolerance")
    assert result.detail["excess"] == "1.50" and result.detail["allowance"] == "2.00"

    write(po_db, 2, commit_entry(M("101.50")))                                        # the WHOLE invoice is committed
    po, status = snapshot(po_db)
    assert po.net_committed == Decimal("1001.50") and po.balance == Decimal("-1.50")   # over-billed, not clamped to 0
    assert status is POStatus.FULLY_BILLED and get_po_balance_minor(po_db, 1) == M("-1.50")


def test_tolerance_does_not_compound_after_an_over_balance_approval(po_db):
    write(po_db, 1, commit_entry(M("1015.00")))                                       # approved 15.00 over on a 1000.00 PO
    follow_up = tolerance_for(po_db, "1.00")
    assert follow_up.outcome_key == "over_tolerance"                                  # balance -15: no pct allowance at all
    assert follow_up.detail["balance"] == "-15.00" and follow_up.detail["excess"] == "16.00" and follow_up.detail["allowance"] == "0.00"


def test_reversal_restores_the_balance_and_status(po_db):
    write(po_db, 1, commit_entry(M("1015.00")))
    assert snapshot(po_db)[1] is POStatus.FULLY_BILLED
    write(po_db, 1, reversal_entry(M("1015.00")))                                     # reviewer later rejects the invoice
    po, status = snapshot(po_db)
    assert po.balance == Decimal("1000.00") and status is POStatus.OPEN
    assert tolerance_for(po_db, "1000.00").outcome_key == "within_balance"


def test_closed_status_survives_ledger_changes(po_db):
    with po_db:
        po_db.execute("UPDATE purchase_orders SET status = 'closed' WHERE id = 1")
    write(po_db, 1, commit_entry(M("100.00")))
    assert snapshot(po_db)[1] is POStatus.CLOSED


def test_the_po_status_rule_agrees_with_the_derivation_helper(po_db):
    for step, entry in enumerate([None, commit_entry(M("400.00")), commit_entry(M("600.00")), commit_entry(M("50.00"))], start=1):
        if entry:
            write(po_db, min(step, 3), entry)
        facts = load_facts(po_db)
        ctx = make_ctx(facts=facts)
        r = ev_builtin("r_po_status", ctx)
        expected = derive_po_status(M(facts.purchase_orders[0].total_amount), M(facts.purchase_orders[0].net_committed), POStatus.OPEN)
        assert r.detail["derived_status"] == expected.value
        assert (r.outcome is Outcome.FLAG) is (expected is POStatus.FULLY_BILLED)


def test_ledger_helpers_cannot_produce_a_row_the_database_would_reject(po_db):
    for entry in (commit_entry(1), commit_entry(M("999999.99")), reversal_entry(1)):
        write(po_db, 1, entry)                                                        # no IntegrityError
    assert get_po_balance_minor(po_db, 1) == M("1000.00") - 1 - M("999999.99") + 1
