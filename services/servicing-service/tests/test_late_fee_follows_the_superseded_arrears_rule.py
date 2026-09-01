"""The late fee is the SMALLER of $35 and 5% of arrears -- the SUPERSEDED rule.

**This file was named `test_late_fee_follows_the_published_schedule.py` until
2026-08-29, and that name stopped being true on the day the schedule changed.**
`policies/fee_schedule.md` now publishes the decided installment-level rule --
one fee per missed scheduled installment, priced off that installment's unpaid
scheduled principal and interest -- and the arrears formula below survives there
only inside the "Current implementation differs" section. A test file asserting
the code "follows the published schedule" would now be asserting the opposite of
the truth, in its filename, where nobody has to open it to be misled.

What these cases still pin is real and unchanged: the code computes

    $35 flat, or 5% of the past-due amount, whichever is less

and it is the only rule it computes. That is the older published rule, kept
deliberately rather than approximated toward the new one. PR #143 landed fee
installment identity, installment bounds and one-fee-per-installment database
enforcement, but the runtime does not use them. Payment-to-installment
attribution is still unknown, and the current authority artifacts supply
neither its allocation order nor the exact grace-period boundary
(`docs/DEBT.md` D23). When the runtime is legitimately cut over, these cases are
expected to fail loudly.

Only the flat half was implemented for months. That is not a rounding nit and it
is not conservative: the flat fee is the LARGER of the two for any arrears below
$700, so every borrower under that threshold was charged more than even the
arrears rule allowed. These cases are written from the overcharge, because that
is the defect -- a test that only checked large balances would have passed
throughout.
"""
from decimal import Decimal

import pytest

from app.delinquency import (LATE_FEE_FLAT, NoFeeIsDue, late_fee_for)


def D(v):
    return Decimal(v)


# --- the half that was missing -------------------------------------------------

@pytest.mark.parametrize("arrears, expected", [
    ("100.00", "5.00"),     # the ordinary delinquent borrower
    ("200.00", "10.00"),
    ("400.00", "20.00"),
    ("699.98", "34.99"),    # 34.9990 rounded DOWN -- never above five per cent
])
def test_a_small_arrears_balance_is_charged_five_per_cent_not_thirty_five(arrears, expected):
    """The overcharge, case by case. On $100 of arrears the borrower owed $5 and
    was billed $35 -- seven times the published fee."""
    assert late_fee_for(arrears) == D(expected)


def test_the_flat_fee_would_have_overcharged_by_thirty_dollars():
    """Stated as the amount, because that is what a borrower experiences."""
    charged_before = LATE_FEE_FLAT
    charged_now = late_fee_for("100.00")
    assert charged_before - charged_now == D("30.00")


# --- the half that was implemented ---------------------------------------------

@pytest.mark.parametrize("arrears", ["700.00", "700.01", "1000.00", "5000.00", "99999.99"])
def test_a_large_arrears_balance_is_capped_at_the_flat_fee(arrears):
    """Above $700 the flat fee is the smaller of the two, so nothing changes for
    these borrowers. The fix must not quietly start charging 5% of a large
    balance -- that would be a far worse defect in the other direction."""
    assert late_fee_for(arrears) == LATE_FEE_FLAT


def test_the_crossover_is_seven_hundred():
    """Where the two halves meet. 5% of $700 is exactly $35, so this is the
    point either rule gives the same answer -- and the boundary a reader should
    be able to check by hand."""
    assert late_fee_for("700.00") == D("35.00")
    assert late_fee_for("699.00") == D("34.95")
    assert late_fee_for("701.00") == D("35.00")


def test_the_fee_is_never_more_than_the_published_maximum():
    for arrears in ("0.20", "1.00", "50.00", "699.99", "700.00", "12345.67"):
        assert late_fee_for(arrears) <= LATE_FEE_FLAT


def test_the_fee_is_never_more_than_five_per_cent_of_the_arrears():
    """The other side of 'whichever is less'. Asserted independently of the
    implementation's own comparison, so a reversed `min` fails here."""
    for arrears in ("0.20", "1.00", "50.00", "400.00", "699.99"):
        assert late_fee_for(arrears) <= Decimal(arrears) * Decimal("0.05")


# --- cents, and the cases that produce no fee at all ---------------------------

def test_the_fee_is_whole_cents():
    for arrears in ("33.33", "66.67", "123.45", "0.33"):
        fee = late_fee_for(arrears)
        assert fee == fee.quantize(Decimal("0.01"))


def test_a_current_loan_is_refused_rather_than_charged_zero():
    """Five per cent of nothing is nothing, and `ledger_entries` refuses a zero
    amount by CHECK. Refusing names the reason instead of surfacing a
    constraint violation that says nothing about the fee schedule."""
    with pytest.raises(NoFeeIsDue):
        late_fee_for("0.00")


def test_a_credit_balance_is_refused_too():
    """`past_due` reaches negative when a waiver exceeds the fees outstanding."""
    with pytest.raises(NoFeeIsDue):
        late_fee_for("-25.00")


def test_arrears_too_small_to_round_to_a_cent_are_refused():
    """Below $0.20, five per cent is under a cent once rounded down. Found by
    running the boundary rather than by reading the rule."""
    for arrears in ("0.01", "0.19"):
        with pytest.raises(NoFeeIsDue):
            late_fee_for(arrears)
    assert late_fee_for("0.20") == D("0.01")


def test_a_float_arrears_value_is_handled_exactly():
    """The column is NUMERIC but the value arrives as a float on some paths;
    conversion goes through `str()` so binary noise never reaches the fee."""
    assert late_fee_for(100.00) == D("5.00")
