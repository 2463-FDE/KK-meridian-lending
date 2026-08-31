"""Installment identity, derived from the contract the borrower signed.

`docs/DEBT.md` D23 traced the engineering gate for the client's late-fee rule of
2026-08-29 and split it into four facts. This module is the first of them, and it
turned out to need no new state at all:

  (a) installment identity, contractual due date, scheduled principal and
      scheduled interest -- **AVAILABLE, DERIVED**.

The derivation is not new here either, which is the reason this module is a
reading of existing behaviour rather than a new source of truth.
`waterfall.scheduled_interest_due` already expands the stored contract with
`schedule.amortization_from_contract(..., start=opened_at.date())` and walks it
period by period to decide how much interest the contract has billed. That code
is in the money path and is tested. All this module does is name the rows it
walks, so a fee can cite one.

**No table, and that is deliberate.** ADR 0010's stance is that the ledger is
authoritative and a second running total kept in step with it is a defect waiting
to happen. Every field below is a pure function of columns already on `loans` --
`principal`, `note_rate_pct`, `term_months`, `regular_payment`, `final_payment`,
`schedule_version`, `opened_at` -- so persisting them would create exactly that
second source of truth. What *is* persisted is the one fact that is observed
rather than derived: which installment a fee was assessed against
(`ledger_entries.installment_no`, db/migrations/0046).

**The anchor is `loans.opened_at`, and it is inherited rather than chosen.**
`scheduled_interest_due` already anchors there. Note that
`routers/loans.py::loan_schedule` calls `amortization_from_contract` WITHOUT a
start date, so the schedule it renders is anchored on `date.today()` and its due
dates move every day. That is a display defect, tracked separately; it is named
here because a reader comparing the two call sites will otherwise assume one of
them is wrong about the anchor. The money path's anchor is the one this module
uses.

WHAT THIS MODULE REFUSES TO DO

`unpaid_scheduled_pi` answers "how much of installment n's scheduled principal
and interest is still unpaid" only when the answer is certain. It is certain when
no principal or interest has been applied to the loan at all: nothing has been
paid, so every installment's unpaid scheduled P&I is its full scheduled P&I,
under any allocation order whatsoever.

The moment a payment exists, the answer depends on **which installment that
payment satisfied**, and no spec, ADR or policy in this repository says. D23 puts
it plainly: oldest-unpaid-first is the natural reading of the client's own rule,
it is written down nowhere, and inventing it would be the exact class of guess the
debt register exists to prevent. So this function raises
`InstallmentAttributionUnknown` rather than picking an order. A caller that cannot
get a number gets an exception naming the missing decision, not a plausible
figure.

That refusal is the honest half of this module and the reason it can ship before
the allocation question is answered.
"""
import datetime
from decimal import Decimal
from typing import NamedTuple

from . import schedule

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

#: Columns that must be present for the contract to be expandable. Mirrors the
#: guard in `waterfall.scheduled_interest_due`, which returns zero interest for a
#: loan missing any of them rather than recomputing the contract.
_CONTRACT_FIELDS = ("principal", "note_rate_pct", "term_months",
                    "regular_payment", "final_payment")


class ScheduleNotAvailable(Exception):
    """This loan has no contractual schedule to expand.

    A loan boarded before `db/migrations/0030` carries no `schedule_version` and
    no stored payment amounts, so it has no installments in the contractual sense
    this module means. `waterfall.scheduled_interest_due` handles the same case by
    returning zero interest -- under-allocating rather than guessing, which is
    conservative in the borrower's favour.

    Here the equivalent conservative answer is to REFUSE. Returning "installment
    n has zero scheduled P&I" would make a percentage-of-P&I fee come out at zero
    and be indistinguishable from a genuine zero, and returning a made-up
    schedule would bill a borrower against terms they never signed.
    """


class InstallmentAttributionUnknown(Exception):
    """Unpaid scheduled P&I for this installment is not derivable.

    Raised when principal or interest has been applied to the loan, because the
    split of that money across installments was never recorded and the order in
    which a payment satisfies installments is published nowhere.

    This is not a defect to be worked around by the caller. It is the missing
    client decision, surfaced at the point where a guess would otherwise be made.
    """


class Installment(NamedTuple):
    """One scheduled installment of a boarded loan.

    `scheduled_pi` is the figure the client's late-fee rule prices against: the
    scheduled PRINCIPAL plus INTEREST for this installment, and nothing else. It
    excludes every fee by construction rather than by subtraction -- there is no
    fee in an amortization row to exclude. That is what makes "previous late fees
    and all other fees are excluded from the base" true here without any filtering
    step that could be got wrong.
    """
    n: int
    due_date: datetime.date
    scheduled_principal: Decimal
    scheduled_interest: Decimal
    scheduled_pi: Decimal


def _anchor(loan) -> datetime.date:
    """The date installment 1 is measured from: `loans.opened_at`.

    Inherited from `waterfall.scheduled_interest_due` rather than chosen here, so
    the two cannot disagree about which period a date falls in.
    """
    opened = loan.get("opened_at")
    if opened is None:
        raise ScheduleNotAvailable(
            "loan has no opened_at, so its installments have no anchor date")
    return opened.date() if hasattr(opened, "date") else opened


def installments_for(loan) -> list[Installment]:
    """Every scheduled installment of this loan, in due-date order.

    Expands the STORED contract amounts, exactly as the money path does. The
    interest split is arithmetic on the contractual rate; the payment amounts are
    the ones boarding copied from the signed offer, never recomputed.
    """
    if not loan.get("schedule_version"):
        raise ScheduleNotAvailable(
            "loan has no schedule_version, so it has no contractual schedule")
    for field in _CONTRACT_FIELDS:
        if loan.get(field) is None:
            raise ScheduleNotAvailable(
                f"loan is missing {field}, so its contract cannot be expanded")

    rows = schedule.amortization_from_contract(
        loan["principal"], loan["note_rate_pct"], int(loan["term_months"]),
        loan["regular_payment"], loan["final_payment"], start=_anchor(loan),
    )

    out: list[Installment] = []
    for row in rows:
        principal = Decimal(str(row["principal"])).quantize(CENT)
        interest = Decimal(str(row["interest"])).quantize(CENT)
        out.append(Installment(
            n=int(row["n"]),
            due_date=datetime.date.fromisoformat(row["due_date"]),
            scheduled_principal=principal,
            scheduled_interest=interest,
            # Quantized after the sum, not before: both addends are already whole
            # cents, so this cannot introduce a residue -- it is here so the type
            # is unambiguous rather than to round anything.
            scheduled_pi=(principal + interest).quantize(CENT),
        ))
    return out


def installment(loan, n: int) -> Installment:
    """One installment by its 1-based period number."""
    if n < 1:
        raise ValueError(f"installment numbers are 1-based; got {n}")
    rows = installments_for(loan)
    if n > len(rows):
        raise ValueError(
            f"loan has {len(rows)} installments; {n} is past the end of the term")
    return rows[n - 1]


def overdue_installments(loan, *, as_of: datetime.date | None = None,
                         grace_days: int) -> list[Installment]:
    """Installments whose due date plus the grace period has passed.

    `grace_days` is REQUIRED and has no default, on purpose. The client's rule
    says a fee is assessed "after the existing grace period" -- and there is no
    existing grace period: no constant, no column, no figure in
    `policies/fee_schedule.md`, and no days-late concept anywhere in this
    repository. A default here would be this module inventing the one number that
    decides when a borrower starts being charged.

    So the caller must supply it, and today no caller can supply it from
    authority. That is the point: the function is ready and the number is not.
    """
    if grace_days < 0:
        raise ValueError(f"grace_days must not be negative; got {grace_days}")
    as_of = as_of or datetime.date.today()
    cutoff = as_of - datetime.timedelta(days=grace_days)
    return [i for i in installments_for(loan) if i.due_date <= cutoff]


def unpaid_scheduled_pi(loan, n: int, *, principal_paid, interest_paid) -> Decimal:
    """Scheduled P&I for installment `n` that is still unpaid.

    `principal_paid` and `interest_paid` are the loan's totals from the LEDGER --
    the record of what was actually applied -- rather than from a column, for the
    same reason `waterfall.interest_owed` derives from the ledger: it cannot then
    drift from it.

    **Answers only when the answer is certain.** With nothing paid, installment
    n's unpaid scheduled P&I is its full scheduled P&I under every possible
    allocation order, so the figure is a fact rather than a choice. With anything
    paid, the figure depends on which installment that money satisfied, which was
    never recorded and whose ordering rule is published nowhere -- so this raises
    `InstallmentAttributionUnknown`.

    Bounds are worth stating for anyone tempted to narrow the gap: under an
    arbitrary order, installment n's unpaid P&I lies between
    `max(0, scheduled_pi(n) - total_paid)` and `scheduled_pi(n)`. Those two
    coincide only when `total_paid` is zero or when it covers the whole term, so
    there is no third case to special-case into a definite answer.
    """
    target = installment(loan, n)
    paid = (_cents(principal_paid, "principal_paid")
            + _cents(interest_paid, "interest_paid"))
    if paid < ZERO:
        raise ValueError(f"paid totals must not be negative; got {paid}")
    if paid > ZERO:
        raise InstallmentAttributionUnknown(
            f"loan has {paid} of principal+interest applied and no record of "
            f"which installment it satisfied; the allocation order across "
            f"installments is published in no spec, ADR or policy here "
            f"(docs/DEBT.md D23), so unpaid scheduled P&I for installment {n} "
            f"cannot be derived without inventing one"
        )
    return target.scheduled_pi


def _cents(value, field: str) -> Decimal:
    """A money input as whole cents, refusing anything that is not."""
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - re-raised with the field named
        raise ValueError(f"{field} is not a number: {value!r}") from exc
    if dec != dec.quantize(CENT):
        raise ValueError(f"{field} is not whole cents: {dec}")
    return dec.quantize(CENT)
