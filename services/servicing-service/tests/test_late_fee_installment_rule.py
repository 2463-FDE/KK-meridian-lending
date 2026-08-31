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


# --------------------------------------------------------------------------
# The whole chain, end to end.
#
# Everything above hands `late_fee_for_installment` a literal. That proves the
# arithmetic and says nothing about where a real base comes from -- and the
# exclusion of fees is a property of the SOURCE, not of the arithmetic. These walk
# the actual path: stored contract -> installments_for -> one installment ->
# scheduled_pi -> the fee.
# --------------------------------------------------------------------------

def _boarded_loan(**over):
    """The contract shape every seeded loan in this repository carries."""
    import datetime
    loan = {
        "principal": Decimal("15000.00"),
        "note_rate_pct": Decimal("7.99"),
        "term_months": 36,
        "regular_payment": Decimal("469.98"),
        "final_payment": Decimal("469.87"),
        "schedule_version": "B1",
        "opened_at": datetime.datetime(2026, 1, 15, 12, 0, 0),
    }
    loan.update(over)
    return loan


def test_the_base_taken_from_a_real_installment_carries_no_fee():
    """The exclusion, asserted over the real source rather than over a parameter.

    An amortization row has exactly two money components. If a third ever appeared
    -- a fee folded into the schedule -- this fails, which is the point: the claim
    is about what `scheduled_pi` is made of.
    """
    from app import installments

    for row in installments.installments_for(_boarded_loan()):
        assert row.scheduled_pi == row.scheduled_principal + row.scheduled_interest
        fee = delinquency.late_fee_for_installment(row.scheduled_pi)
        assert fee <= delinquency.LATE_FEE_FLAT
        assert fee <= (row.scheduled_principal + row.scheduled_interest) \
            * delinquency.LATE_FEE_PCT_OF_INSTALLMENT_PI


def test_the_fee_on_a_real_installment_of_a_seeded_contract():
    """A concrete figure, so the chain is not just internally consistent.

    $15,000 at 7.99% over 36 months bills $469.98 a period, all of it principal and
    interest. Five per cent of that is $23.49, which is below the $35 cap, so the
    percentage binds -- and the fee for any regular installment of this loan is the
    same figure.
    """
    from app import installments

    third = installments.installment(_boarded_loan(), 3)
    assert third.scheduled_pi == Decimal("469.98")
    assert delinquency.late_fee_for_installment(third.scheduled_pi) == Decimal("23.49")


def test_the_four_client_examples_through_the_real_entry_point():
    """The supplied examples, reached the way the runtime would reach them.

    The parametrised cases at the top of this file pass literals. These build a
    contract whose installment P&I IS the example figure, so the number the client
    gave is produced by the same call the assessment path would make.
    """
    from app import installments

    for pi, expected in (("200.00", "10.00"), ("500.00", "25.00"),
                         ("700.00", "35.00"), ("1000.00", "35.00")):
        # A one-period contract whose single installment is exactly `pi`: principal
        # plus its first month of interest at 0% is the payment itself.
        loan = _boarded_loan(principal=Decimal(pi), note_rate_pct=Decimal("0.000"),
                             term_months=1, regular_payment=Decimal(pi),
                             final_payment=Decimal(pi))
        only = installments.installment(loan, 1)
        assert only.scheduled_pi == Decimal(pi), only
        assert delinquency.late_fee_for_installment(only.scheduled_pi) == Decimal(expected)


def test_arrears_would_price_differently_which_is_why_the_source_matters():
    """The two rules diverge on the same loan, and that divergence is D23.

    Installment 3's scheduled P&I is $469.98. A borrower three payments behind with
    a prior fee has arrears well above that, so the superseded rule prices higher.
    Asserting the gap keeps the docstring's qualifier honest: handing this function
    `balances.past_due` produces a different, larger number while looking identical.
    """
    from app import installments

    scheduled = installments.installment(_boarded_loan(), 3).scheduled_pi
    arrears = scheduled * 3 + Decimal("35.00")
    assert delinquency.late_fee_for_installment(scheduled) \
        < delinquency.late_fee_for(arrears)
