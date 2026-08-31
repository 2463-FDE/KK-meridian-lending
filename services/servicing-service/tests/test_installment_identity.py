"""Installment identity, and the two things it refuses to derive.

`docs/DEBT.md` D23 fact (a) -- installment identity, due date, scheduled principal
and scheduled interest -- is AVAILABLE from the stored contract. This file proves
that, and proves the module refuses fact (c), "remaining unpaid scheduled P&I",
in every case where answering it would need an allocation order nobody published.

The refusal cases matter more than the happy path here. A module that guessed
would pass a test suite that only checked its arithmetic.
"""
import datetime
from decimal import Decimal

import pytest

from app import installments, waterfall


def a_loan(**over):
    """A boarded loan whose stored contract amortizes exactly.

    $15,000 over 36 months at 7.99% is the shape every seeded loan in this
    repository carries, so the figures below are the ones the rest of the suite
    and the demo data already agree on.
    """
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


# --------------------------------------------------------------------------
# Identity.
# --------------------------------------------------------------------------

def test_every_installment_of_the_term_is_named_once_in_order():
    rows = installments.installments_for(a_loan())
    assert len(rows) == 36
    assert [r.n for r in rows] == list(range(1, 37))
    assert rows == sorted(rows, key=lambda r: r.due_date)


def test_scheduled_pi_is_principal_plus_interest_and_nothing_else():
    """The base the decided rule prices against, with no fee in it."""
    for row in installments.installments_for(a_loan()):
        assert row.scheduled_pi == row.scheduled_principal + row.scheduled_interest
        assert row.scheduled_pi > 0


def test_the_anchor_is_opened_at_not_today():
    """Installment 1 is due one month after the loan opened, whenever we ask.

    `routers/loans.py::loan_schedule` calls `amortization_from_contract` with no
    start date, so the schedule it renders drifts with `date.today()`. That is a
    display defect tracked separately. This module must not inherit it: a fee that
    cited installment 3 today and installment 4 tomorrow would be citing nothing.
    """
    loan = a_loan(opened_at=datetime.datetime(2026, 1, 15, 12, 0, 0))
    first = installments.installment(loan, 1)
    assert first.due_date == datetime.date(2026, 2, 15)
    # Asked again, unchanged -- the anchor is stored, not the clock.
    assert installments.installment(loan, 1).due_date == datetime.date(2026, 2, 15)


def test_a_date_only_opened_at_anchors_the_same_way():
    """`opened_at` arrives as a datetime from psycopg and a date from a fixture."""
    as_datetime = installments.installment(
        a_loan(opened_at=datetime.datetime(2026, 1, 15, 9, 30)), 1)
    as_date = installments.installment(
        a_loan(opened_at=datetime.date(2026, 1, 15)), 1)
    assert as_datetime.due_date == as_date.due_date


def test_identity_agrees_with_the_interest_the_money_path_bills():
    """The two walkers of this schedule must not disagree about a period.

    `waterfall.scheduled_interest_due` sums the interest of every period due by a
    date. Summing this module's rows to the same cutoff must give the same figure,
    or a fee would be citing a different schedule from the one the payment path
    bills against.
    """
    loan = a_loan()
    as_of = datetime.date(2026, 6, 20)
    from_waterfall = waterfall.scheduled_interest_due(loan, as_of=as_of)
    from_here = sum(
        (i.scheduled_interest for i in installments.installments_for(loan)
         if i.due_date <= as_of),
        Decimal("0.00"),
    )
    assert from_here == from_waterfall


def test_installment_numbers_are_one_based_and_bounded():
    loan = a_loan()
    with pytest.raises(ValueError):
        installments.installment(loan, 0)
    with pytest.raises(ValueError):
        installments.installment(loan, 37)
    assert installments.installment(loan, 36).n == 36


# --------------------------------------------------------------------------
# Refusals: a loan with no contract.
# --------------------------------------------------------------------------

def test_a_legacy_loan_with_no_schedule_is_refused_not_zeroed():
    """Returning "zero scheduled P&I" would price a fee at zero indistinguishably.

    `waterfall.scheduled_interest_due` returns zero interest for this case, which
    is conservative there because it under-charges interest. The conservative
    answer HERE is to refuse: a percentage-of-P&I fee computed from a zeroed
    schedule looks like a real answer and is not one.
    """
    with pytest.raises(installments.ScheduleNotAvailable):
        installments.installments_for(a_loan(schedule_version=None))


@pytest.mark.parametrize("missing", [
    "principal", "note_rate_pct", "term_months", "regular_payment", "final_payment",
])
def test_a_partial_contract_is_refused(missing):
    with pytest.raises(installments.ScheduleNotAvailable):
        installments.installments_for(a_loan(**{missing: None}))


def test_a_loan_with_no_opened_at_has_no_anchor():
    with pytest.raises(installments.ScheduleNotAvailable):
        installments.installments_for(a_loan(opened_at=None))


# --------------------------------------------------------------------------
# Refusals: the allocation order nobody published. This is D23's live blocker.
# --------------------------------------------------------------------------

def test_with_nothing_paid_the_unpaid_figure_is_certain():
    """Under EVERY allocation order, nothing paid means nothing paid off."""
    loan = a_loan()
    third = installments.installment(loan, 3)
    assert installments.unpaid_scheduled_pi(
        loan, 3, principal_paid=Decimal("0.00"), interest_paid=Decimal("0.00")
    ) == third.scheduled_pi


@pytest.mark.parametrize("principal_paid,interest_paid", [
    ("0.01", "0.00"),
    ("0.00", "0.01"),
    ("469.98", "0.00"),
    ("300.00", "169.98"),
    ("100000.00", "0.00"),
])
def test_any_payment_at_all_makes_the_figure_undecidable(principal_paid, interest_paid):
    """The moment money has been applied, the answer needs an ordering rule.

    Not "hard to compute" -- undecidable from what was recorded. The split of that
    money across installments was never captured, and which installment a payment
    satisfies first is published in no spec, ADR or policy here. Guessing
    oldest-first would produce a number that looks like the client's rule and is
    not it, which D23 rules out by name.

    The exception exists so a caller gets the missing DECISION rather than a
    plausible figure.
    """
    with pytest.raises(installments.InstallmentAttributionUnknown):
        installments.unpaid_scheduled_pi(
            a_loan(), 3,
            principal_paid=Decimal(principal_paid),
            interest_paid=Decimal(interest_paid),
        )


def test_the_refusal_names_the_register_entry():
    """A caller reading the message must be able to find the missing decision."""
    with pytest.raises(installments.InstallmentAttributionUnknown) as exc:
        installments.unpaid_scheduled_pi(
            a_loan(), 1, principal_paid=Decimal("1.00"), interest_paid=Decimal("0.00"))
    assert "D23" in str(exc.value)


def test_negative_paid_totals_are_rejected():
    with pytest.raises(ValueError):
        installments.unpaid_scheduled_pi(
            a_loan(), 1, principal_paid=Decimal("-1.00"), interest_paid=Decimal("0.00"))


def test_sub_cent_paid_totals_are_rejected_rather_than_rounded():
    """A third of a cent is not a ledger amount; rounding it would invent money."""
    with pytest.raises(ValueError):
        installments.unpaid_scheduled_pi(
            a_loan(), 1, principal_paid=Decimal("0.001"), interest_paid=Decimal("0.00"))


# --------------------------------------------------------------------------
# The grace period that does not exist.
# --------------------------------------------------------------------------

def test_overdue_requires_a_grace_period_to_be_supplied():
    """There is no default, because there is no grace period to default to.

    The client's rule says a fee comes "after the existing grace period". No
    constant, column or published figure in this repository defines one. A default
    here would be this module inventing the number that decides when a borrower
    starts being charged.
    """
    with pytest.raises(TypeError):
        installments.overdue_installments(a_loan(), as_of=datetime.date(2026, 12, 1))


def test_grace_days_shifts_the_cutoff_by_exactly_that_many_days():
    """The mechanism works and is tested; only the VALUE is missing.

    Installment 1 of this loan is due 2026-02-15. Asked on the due date with no
    grace it is overdue; with one day of grace it is not yet.
    """
    loan = a_loan()
    on_due_date = datetime.date(2026, 2, 15)
    assert installments.overdue_installments(
        loan, as_of=on_due_date, grace_days=0)[0].n == 1
    assert installments.overdue_installments(
        loan, as_of=on_due_date, grace_days=1) == []
    assert installments.overdue_installments(
        loan, as_of=on_due_date + datetime.timedelta(days=10), grace_days=10)[0].n == 1


def test_a_negative_grace_period_is_refused():
    with pytest.raises(ValueError):
        installments.overdue_installments(
            a_loan(), as_of=datetime.date(2026, 12, 1), grace_days=-1)
