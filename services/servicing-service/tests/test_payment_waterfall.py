"""The waterfall allocates fees -> interest -> principal (D14).

The order is published policy, not a choice this code makes:
`policies/fee_schedule.md` states it as the source of truth. These tests are
written from the cases that decide whether the order means anything -- a SHORT
payment, which is the payment a delinquent borrower actually makes.

Pure arithmetic, so no database: `allocate` takes what is owed and returns the
split. The wiring to `ledger_entries` is tested against real PostgreSQL in
`test_payment_waterfall_posts_components.py`, because that is where the
projection and the per-component uniqueness live.
"""
import datetime
from decimal import Decimal

import pytest

from app import waterfall
from app.waterfall import (AmountIsNotWholeCents, PaymentExceedsAmountOwed,
                           allocate)


def D(v):
    return Decimal(v)


# --- the order itself ---------------------------------------------------------

def test_a_payment_covering_everything_pays_all_three():
    got = allocate("1000.00", fees_owed="35.00", interest_owed="59.92",
                   principal_owed="905.08")
    assert got.fees == D("35.00")
    assert got.interest == D("59.92")
    assert got.principal == D("905.08")
    assert got.total == D("1000.00")


def test_fees_are_paid_before_interest_and_principal():
    """The whole point of D14. Before this, a borrower carrying a $35 late fee
    had their payment reduce principal while the fee stayed owed and kept the
    loan delinquent."""
    got = allocate("35.00", fees_owed="35.00", interest_owed="59.92",
                   principal_owed="905.08")
    assert got == (D("35.00"), D("0.00"), D("0.00"))


def test_a_short_payment_stops_partway_through_fees():
    """Not an error. A borrower who pays less than their fees pays fees only."""
    got = allocate("10.00", fees_owed="35.00", interest_owed="59.92",
                   principal_owed="905.08")
    assert got == (D("10.00"), D("0.00"), D("0.00"))


def test_a_short_payment_spills_into_interest_but_not_principal():
    got = allocate("50.00", fees_owed="35.00", interest_owed="59.92",
                   principal_owed="905.08")
    assert got == (D("35.00"), D("15.00"), D("0.00"))
    assert got.principal == D("0.00"), (
        "principal was reduced while interest was still owed -- the order is "
        "fees, then interest, then principal"
    )


def test_a_payment_reaches_principal_only_after_fees_and_interest():
    got = allocate("100.00", fees_owed="35.00", interest_owed="59.92",
                   principal_owed="905.08")
    assert got == (D("35.00"), D("59.92"), D("5.08"))


def test_with_nothing_owed_but_principal_it_all_goes_to_principal():
    """A current borrower's ordinary payment. The waterfall must not change the
    common case -- this is what every payment did before D14."""
    got = allocate("500.00", fees_owed="0.00", interest_owed="0.00",
                   principal_owed="9000.00")
    assert got == (D("0.00"), D("0.00"), D("500.00"))


def test_the_allocation_always_sums_to_the_payment():
    for amount in ("0.01", "1.00", "34.99", "35.00", "35.01", "94.92", "1000.00"):
        got = allocate(amount, fees_owed="35.00", interest_owed="59.92",
                       principal_owed="905.08")
        assert got.total == Decimal(amount), f"{amount} did not sum back"


# --- overpayment is refused ---------------------------------------------------

def test_a_payment_larger_than_everything_owed_is_refused():
    """Decision recorded on the PR: what happens to the excess is a Lending
    Operations question no document here answers. Refusing states that; applying
    it to principal or holding it as credit would answer it silently."""
    with pytest.raises(PaymentExceedsAmountOwed) as exc:
        allocate("1000.01", fees_owed="35.00", interest_owed="59.92",
                 principal_owed="905.08")
    assert "exceeds total owed" in str(exc.value)


def test_the_refusal_names_the_excess():
    """An operator has to be able to tell an overpayment from a rejected one."""
    with pytest.raises(PaymentExceedsAmountOwed) as exc:
        allocate("5000.00", fees_owed="35.00", interest_owed="59.92",
                 principal_owed="905.08")
    message = str(exc.value)
    assert "5000.00" in message, "the refusal does not name the payment"
    assert "1000.00" in message, "the refusal does not name what was owed"
    assert "4000.00" in message, f"the refusal does not name the excess: {message}"


def test_paying_exactly_the_total_owed_is_allowed():
    """The boundary. A payoff to the cent must not be refused as an
    overpayment -- an off-by-one here would reject every final payment."""
    got = allocate("1000.00", fees_owed="35.00", interest_owed="59.92",
                   principal_owed="905.08")
    assert got.total == D("1000.00")


# --- cents are exact ----------------------------------------------------------

def test_a_sub_cent_payment_is_refused_rather_than_rounded():
    with pytest.raises(AmountIsNotWholeCents):
        allocate("10.005", fees_owed="35.00", interest_owed="0.00",
                 principal_owed="905.08")


def test_sub_cent_amounts_owed_are_refused_too():
    """The residual this design avoids can enter through what is OWED just as
    easily as through the payment."""
    with pytest.raises(AmountIsNotWholeCents):
        allocate("10.00", fees_owed="35.005", interest_owed="0.00",
                 principal_owed="905.08")


def test_a_float_that_cannot_be_represented_exactly_is_still_handled():
    """`0.1 + 0.2` is the repository's own D1/D12 story. Values arrive at this
    boundary as floats from the request model, so the conversion goes through
    `str()` -- `Decimal(0.1)` would carry binary noise and be refused as
    sub-cent, rejecting a perfectly ordinary payment."""
    got = allocate(0.30, fees_owed=0.10, interest_owed=0.20, principal_owed=100.00)
    assert got == (D("0.10"), D("0.20"), D("0.00"))


def test_no_component_is_ever_negative():
    got = allocate("1.00", fees_owed="35.00", interest_owed="59.92",
                   principal_owed="905.08")
    assert all(part >= 0 for part in got)


def test_a_zero_or_negative_payment_is_refused():
    for bad in ("0.00", "-1.00"):
        with pytest.raises(AmountIsNotWholeCents):
            allocate(bad, fees_owed="0.00", interest_owed="0.00",
                     principal_owed="100.00")


def test_a_credit_on_a_component_is_treated_as_nothing_owed():
    """`past_due` genuinely goes negative -- waiving a fee larger than the fees
    outstanding leaves the borrower in credit on that component, which is how
    this case was found (a real-postgres concurrency test does exactly that).

    Clamped to zero rather than refused. Refusing would reject a borrower's
    payment because they hold a credit, which is worse than the state it
    guards. The credit is untouched and stays visible in `balances`.
    """
    got = allocate("10.00", fees_owed="-25.00", interest_owed="0.00",
                   principal_owed="100.00")
    assert got == (D("0.00"), D("0.00"), D("10.00"))


def test_a_credit_does_not_inflate_what_can_be_paid():
    """The clamp must not let a credit on one component absorb a payment that
    exceeds everything genuinely owed."""
    with pytest.raises(PaymentExceedsAmountOwed):
        allocate("150.00", fees_owed="-25.00", interest_owed="0.00",
                 principal_owed="100.00")


# --- only real movements become entries ---------------------------------------

def test_components_omits_the_zero_parts():
    """`ledger_entries` has CHECK (amount <> 0), so a zero entry cannot be
    written -- and one that could would claim a movement that never happened."""
    got = allocate("35.00", fees_owed="35.00", interest_owed="59.92",
                   principal_owed="905.08")
    assert list(got.components()) == [("fees", D("35.00"))]


def test_components_are_yielded_in_waterfall_order():
    got = allocate("100.00", fees_owed="35.00", interest_owed="59.92",
                   principal_owed="905.08")
    assert [name for name, _ in got.components()] == ["fees", "interest",
                                                      "principal"]


# --- interest owed comes from the signed schedule -----------------------------

_LOAN = {
    "principal": 18000.00,
    "note_rate_pct": 7.99,
    "term_months": 48,
    "regular_payment": 439.35,
    "final_payment": 439.24,
    "schedule_version": "B1",
    "opened_at": datetime.date(2026, 1, 15),
}


def test_interest_owed_is_billed_from_the_stored_contract():
    """Two periods elapsed -> the first two periods' interest, and nothing paid
    yet, so all of it is owed."""
    owed = waterfall.interest_owed(
        _LOAN, interest_already_paid="0.00", as_of=datetime.date(2026, 3, 20))
    rows = __import__("app.schedule", fromlist=["schedule"]).amortization_from_contract(
        _LOAN["principal"], _LOAN["note_rate_pct"], _LOAN["term_months"],
        _LOAN["regular_payment"], _LOAN["final_payment"],
        start=_LOAN["opened_at"])
    expected = sum(Decimal(str(r["interest"])) for r in rows[:2])
    assert owed == expected
    assert owed > 0, "the fixture elapsed no periods -- the case is vacuous"


def test_interest_already_paid_is_deducted():
    full = waterfall.interest_owed(_LOAN, interest_already_paid="0.00",
                                   as_of=datetime.date(2026, 3, 20))
    part = waterfall.interest_owed(_LOAN, interest_already_paid="50.00",
                                   as_of=datetime.date(2026, 3, 20))
    assert part == full - Decimal("50.00")


def test_interest_owed_never_goes_negative():
    """A loan credited more interest than the schedule billed owes none.
    Reporting a negative would make interest a component the waterfall pays
    into."""
    owed = waterfall.interest_owed(_LOAN, interest_already_paid="99999.00",
                                   as_of=datetime.date(2026, 3, 20))
    assert owed == Decimal("0.00")


def test_no_interest_is_owed_before_the_first_period_falls_due():
    owed = waterfall.interest_owed(_LOAN, interest_already_paid="0.00",
                                   as_of=datetime.date(2026, 1, 20))
    assert owed == Decimal("0.00")


def test_a_legacy_loan_with_no_stored_schedule_owes_no_interest():
    """Deliberate, and the direction matters. There is no way to establish what
    a pre-0030 loan has accrued without inventing a convention, so it is billed
    none -- the payment goes to fees and then principal. That under-allocates to
    interest rather than guessing, which is the conservative direction; the
    alternative would quietly favour the lender."""
    legacy = dict(_LOAN, schedule_version=None)
    assert waterfall.interest_owed(
        legacy, interest_already_paid="0.00",
        as_of=datetime.date(2028, 1, 1)) == Decimal("0.00")


def test_a_loan_missing_its_stored_payment_amounts_owes_no_interest():
    """`loans_schedule_all_or_nothing` should prevent this, but the reader must
    not depend on a constraint it does not enforce itself."""
    partial = dict(_LOAN, regular_payment=None)
    assert waterfall.interest_owed(
        partial, interest_already_paid="0.00",
        as_of=datetime.date(2028, 1, 1)) == Decimal("0.00")
