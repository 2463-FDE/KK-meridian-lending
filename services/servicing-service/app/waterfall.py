"""The payment waterfall: fees -> accrued interest -> principal (D14).

The order is not a choice made here. `policies/fee_schedule.md` publishes it as
the source of truth:

    The payment waterfall on a received payment is:
    fees -> accrued interest -> principal.
    Interest accrues on the outstanding principal at the loan's note rate.

Until now a payment posted one `principal` entry for its whole amount, so a
borrower carrying a late fee had it reduce principal while the fee stayed owed
and kept accruing delinquency. The ledger has been able to hold the split since
`db/migrations/0035` -- its uniqueness rule is per `(payment_id, component)`
rather than per payment, deliberately so one payment can be split (ADR 0010).
What did not exist was the allocation. This is it.

**Three decisions were required and none of them is invented here.**

1. **Where "accrued interest" comes from.** Nothing stored or computed it, and
   ADR 0010 deliberately makes `interest` project nowhere: it is recorded inside
   a payment, never carried as a separate balance the borrower owes. So interest
   owed is DERIVED from the loan's own stored contractual schedule -- the
   amounts boarding copied from the signed offer (db/migrations/0030) -- minus
   the interest already posted to the ledger. No new table, no accrual job, and
   no day-count convention invented: the schedule is the disclosure the borrower
   signed, so billing from it cannot disagree with what they were quoted.

2. **Overpayment is REFUSED**, not absorbed. A payment larger than everything
   owed raises `PaymentExceedsAmountOwed` and writes nothing. Applying the
   excess to principal would be an early-paydown decision, and holding it as
   unapplied credit would be a new balance the borrower can see -- both are
   Lending Operations policy questions that no document in this repository
   answers. Refusing states the limitation instead of picking one silently.

3. **Cents are exact, so no residual can arise.** Every amount is a `Decimal`
   quantized to cents before allocation, and allocation is subtraction. There is
   no tie-break rule for a leftover cent because the arithmetic cannot produce
   one; a non-cent input is refused rather than quietly rounded into a
   borrower's balance.
"""
import datetime
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from . import schedule

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class PaymentExceedsAmountOwed(Exception):
    """The payment is larger than fees + interest + principal owed.

    Refused rather than absorbed. See the module docstring: what happens to the
    excess is an unanswered policy question, and every available answer changes
    what the borrower owes.
    """


class AmountIsNotWholeCents(Exception):
    """An amount carried sub-cent precision.

    Refused rather than rounded. Rounding here would introduce exactly the
    residual this design avoids by construction, and it would do it inside a
    money movement rather than at a display boundary.
    """


class Allocation(NamedTuple):
    """What one payment paid, per component. Always sums to the payment."""
    fees: Decimal
    interest: Decimal
    principal: Decimal

    @property
    def total(self) -> Decimal:
        return self.fees + self.interest + self.principal

    def components(self):
        """(component, amount) for the non-zero parts, in waterfall order.

        Zero-valued components are omitted deliberately: `ledger_entries` has a
        `CHECK (amount <> 0)`, so a zero entry cannot be written, and one that
        could would claim a movement that did not happen.
        """
        for name, amount in (("fees", self.fees),
                             ("interest", self.interest),
                             ("principal", self.principal)):
            if amount > ZERO:
                yield name, amount


def _cents(value, *, field: str) -> Decimal:
    """A Decimal in whole cents, or a refusal naming the field."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AmountIsNotWholeCents(f"{field} is not a number: {value!r}") from exc
    if amount != amount.quantize(CENT):
        raise AmountIsNotWholeCents(
            f"{field} carries sub-cent precision: {amount}")
    return amount.quantize(CENT)


def allocate(amount, *, fees_owed, interest_owed, principal_owed) -> Allocation:
    """Split `amount` across fees -> interest -> principal, in that order.

    A SHORT payment fills the components in order and stops: it clears fees
    first, then as much interest as remains, then principal. That is what the
    published order means and it is the case that matters -- a borrower who is
    behind is the borrower whose payment is short.

    Raises `PaymentExceedsAmountOwed` if the payment is larger than the sum of
    the three, and `AmountIsNotWholeCents` if any input carries sub-cent
    precision. Both refuse before anything is allocated.
    """
    payment = _cents(amount, field="payment amount")
    fees = _cents(fees_owed, field="fees owed")
    interest = _cents(interest_owed, field="interest owed")
    principal = _cents(principal_owed, field="principal owed")

    if payment <= ZERO:
        raise AmountIsNotWholeCents(f"payment amount must be positive: {payment}")

    # A NEGATIVE amount owed is a credit, not a debt, and it is clamped to zero
    # rather than refused.
    #
    # `past_due` genuinely reaches negative: waiving a fee larger than the fees
    # outstanding leaves the borrower in credit on that component. The
    # maker-checker route refuses to CREATE such a proposal (spec 0002 AC-20),
    # but the state exists in the data regardless -- and a loan that got there
    # must still be able to take a payment.
    #
    # Refusing here would reject a borrower's payment because they hold a
    # credit, which is a worse outcome than the one it guards against. Clamping
    # says only "you cannot pay into a component nobody owes"; the credit is
    # untouched and stays visible in `balances`, which is where it belongs.
    fees = max(ZERO, fees)
    interest = max(ZERO, interest)
    principal = max(ZERO, principal)

    total_owed = fees + interest + principal
    if payment > total_owed:
        raise PaymentExceedsAmountOwed(
            f"payment {payment} exceeds total owed {total_owed} by "
            f"{payment - total_owed}"
        )

    remaining = payment
    to_fees = min(remaining, fees)
    remaining -= to_fees
    to_interest = min(remaining, interest)
    remaining -= to_interest
    to_principal = remaining

    allocation = Allocation(fees=to_fees, interest=to_interest,
                            principal=to_principal)
    # The property the whole function exists to hold. Asserted rather than
    # trusted: an allocation that does not sum to the payment has either
    # created or destroyed money, and it would do it inside the ledger.
    assert allocation.total == payment, (
        f"allocation {allocation} does not sum to {payment}"
    )
    return allocation


def scheduled_interest_due(loan, *, as_of: datetime.date | None = None) -> Decimal:
    """Interest the CONTRACT has billed by `as_of`, from the stored schedule.

    Sums the interest portion of every period whose due date has passed. The
    amounts come from `amortization_from_contract`, which bills the payment
    figures stored on the loan rather than recomputing them -- so this agrees
    with the schedule the borrower signed instead of re-deriving it at read time.

    **Returns zero for a loan with no stored schedule**, and that is deliberate.
    A legacy loan boarded before `db/migrations/0030` has no contractual
    schedule to bill from, and there is no way to establish what interest it has
    accrued without inventing a convention. Zero means the payment goes to fees
    and then principal, which charges the borrower no interest this system
    cannot substantiate. It under-allocates to interest rather than guessing,
    and that direction is the conservative one -- stated here because the
    alternative would quietly favour the lender.
    """
    if not loan.get("schedule_version"):
        return ZERO
    for field in ("principal", "note_rate_pct", "term_months",
                  "regular_payment", "final_payment"):
        if loan.get(field) is None:
            return ZERO

    as_of = as_of or datetime.date.today()
    opened = loan.get("opened_at")
    if opened is None:
        return ZERO
    start = opened.date() if hasattr(opened, "date") else opened

    rows = schedule.amortization_from_contract(
        loan["principal"], loan["note_rate_pct"], int(loan["term_months"]),
        loan["regular_payment"], loan["final_payment"], start=start,
    )
    billed = ZERO
    for row in rows:
        due = datetime.date.fromisoformat(row["due_date"])
        if due <= as_of:
            billed += Decimal(str(row["interest"])).quantize(CENT)
    return billed


def interest_owed(loan, *, interest_already_paid, as_of=None) -> Decimal:
    """Contractual interest billed to date, less what the ledger already recorded.

    `interest_already_paid` is the sum of the loan's `interest` ledger entries.
    Deriving it from the ledger rather than a column is the point: the ledger is
    the record of what was actually applied, so this cannot drift from it, and
    no new state has to be kept in step.

    Never negative. A loan that has somehow been credited more interest than the
    schedule billed owes none, and reporting a negative would make it a
    component the waterfall pays INTO.
    """
    billed = scheduled_interest_due(loan, as_of=as_of)
    paid = _cents(interest_already_paid, field="interest already paid")
    return max(ZERO, billed - paid)
