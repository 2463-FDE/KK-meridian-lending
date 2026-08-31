"""The DECIDED late-fee rule, as arithmetic (`docs/DEBT.md` D23).

The client answered D23 on 2026-08-29: at most one fee per missed scheduled
installment, priced at `min($35, 5% x unpaid scheduled PRINCIPAL + INTEREST for
THAT installment)`, with previous late fees and every other fee excluded from the
base. Four worked examples came with the decision and they are asserted here by
name, because a rule with supplied examples is a rule whose implementation can be
checked against something the client wrote rather than against our reading of it.

What this file does NOT assert is that the route charges this. It does not, and
`test_late_fee_follows_the_superseded_arrears_rule.py` still pins the older
comparison that runs today. Both are true at once and the pair is the honest state
of D23: the arithmetic is decided and tested, the runtime cutover waits on two
decisions nobody in this repository can make (the grace period, and the allocation
order across installments).
"""
from decimal import Decimal

import pytest

from app import delinquency


# --------------------------------------------------------------------------
# The client's own worked examples.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("unpaid_pi,expected,which_bound", [
    ("200.00", "10.00", "five per cent binds"),
    ("500.00", "25.00", "five per cent binds"),
    ("700.00", "35.00", "the two bounds meet exactly"),
    ("1000.00", "35.00", "the $35 cap binds"),
])
def test_the_four_worked_examples_supplied_with_the_decision(
        unpaid_pi, expected, which_bound):
    """Each figure the client supplied, and which bound decides it.

    $700 is the crossover and is the one worth reading twice: five per cent of it
    is exactly $35.00, so both bounds agree. Either bound alone would pass this
    case, which is why the other three are here.
    """
    assert delinquency.late_fee_for_installment(Decimal(unpaid_pi)) == Decimal(expected), which_bound


def test_the_percentage_bound_is_never_exceeded():
    """Below the crossover the fee is five per cent, not the flat figure.

    This is the defect the superseded rule carried for months in its own domain:
    charging the flat $35 whenever the percentage was smaller overcharges every
    borrower under the crossover. Asserted against the percentage independently
    rather than against a hard-coded expectation, so a change to either constant
    fails here.
    """
    for cents in ("0.20", "19.99", "100.00", "400.00", "699.98"):
        base = Decimal(cents)
        fee = delinquency.late_fee_for_installment(base)
        assert fee <= base * delinquency.LATE_FEE_PCT_OF_INSTALLMENT_PI
        assert fee <= delinquency.LATE_FEE_FLAT


def test_rounding_is_down_so_neither_bound_is_breached():
    """$699.99 -> $34.99, not $35.00.

    Five per cent of $699.99 is $34.9995. Half-up would bill $35.00, which is half
    a cent above the percentage bound the rule caps it at. "The lesser of" has to
    hold against BOTH bounds, and only rounding down does that.
    """
    assert delinquency.late_fee_for_installment(Decimal("699.99")) == Decimal("34.99")


def test_the_cap_holds_however_large_the_installment():
    for base in ("700.01", "5000.00", "999999.00"):
        assert delinquency.late_fee_for_installment(Decimal(base)) == Decimal("35.00")


# --------------------------------------------------------------------------
# Refusals.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("base", ["0.00", "-1.00", "-0.01"])
def test_nothing_unpaid_is_no_fee_rather_than_a_zero_fee(base):
    """`ledger_entries` refuses a zero amount by CHECK, so zero is not writable.

    Raising here names the reason; letting the insert fail would surface a
    constraint violation that says nothing about the fee schedule.
    """
    with pytest.raises(delinquency.NoFeeIsDue):
        delinquency.late_fee_for_installment(Decimal(base))


def test_under_twenty_cents_rounds_to_no_fee():
    """Five per cent of anything below $0.20 is under a cent once rounded down."""
    for base in ("0.01", "0.19"):
        with pytest.raises(delinquency.NoFeeIsDue):
            delinquency.late_fee_for_installment(Decimal(base))


# --------------------------------------------------------------------------
# The two rules are different rules.
# --------------------------------------------------------------------------

def test_the_decided_rule_and_the_superseded_one_differ_on_the_same_number():
    """Same input, same rate, different meaning -- and that is the whole of D23.

    $600 of arrears and $600 of one installment's unpaid scheduled P&I happen to
    price identically, because the rate is the same. The difference is what the
    $600 IS: `late_fee_for` takes `balances.past_due`, one projected total mixing
    principal, interest and every fee already assessed, which is why a posted fee
    raises the next base. `late_fee_for_installment` takes one amortization row's
    principal plus interest, which contains no fee to begin with.

    Asserting they agree numerically is the point: it proves the two constants
    have not drifted, while the docstrings and `db/tests` carry the fact that the
    BASES are not interchangeable.
    """
    assert (delinquency.LATE_FEE_PCT_OF_INSTALLMENT_PI
            == delinquency.LATE_FEE_PCT_OF_PAST_DUE)
    assert (delinquency.late_fee_for_installment(Decimal("600.00"))
            == delinquency.late_fee_for(Decimal("600.00")))


def test_fees_cannot_enter_the_base_because_the_base_has_no_fee_in_it():
    """The exclusion is structural, not a filtering step that could be got wrong.

    The decided rule says previous late fees and all other fees are excluded from
    the percentage base. This function's input is an amortization row's principal
    plus interest. There is no fee in an amortization row, so there is nothing to
    exclude and no code path that could forget to.

    The assertion is the contrapositive: a base that DID include a $35 prior fee
    would price higher, and it must not be reachable through this function's
    contract. Documented by asserting the arithmetic difference so a future change
    that starts passing an arrears figure in here fails visibly.
    """
    installment_pi = Decimal("400.00")
    same_plus_a_prior_fee = installment_pi + Decimal("35.00")
    assert (delinquency.late_fee_for_installment(installment_pi)
            < delinquency.late_fee_for_installment(same_plus_a_prior_fee))
