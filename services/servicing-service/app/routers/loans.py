"""Loan portfolio read API: list, detail, schedule, payment history, activity."""
import logging

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schedule
from ..database import get_session
from ..schemas import (
    ActivityItem,
    ActivityOut,
    LoanDetail,
    LoanListItem,
    Page,
    PaymentItem,
    PaymentsOut,
    ScheduleOut,
    ScheduleRow,
)

log = logging.getLogger("loans")

router = APIRouter(prefix="/loans", tags=["loans"])


def _display_last4(payment) -> str | None:
    """Masked card for payment history. Never returns more than four digits.

    ADR 0008: new rows carry `last4` directly, from the processor's token
    response rather than from a card number this service never sees.

    **There is no `pan` fallback.** It was removed in the contract step it was
    annotated for, and the columns it read are gone (db/migrations/0031). This
    function reads `last4` and nothing else; a row without one renders nothing.

    This docstring is the reason that sentence is stated first. Until now it
    opened with "THE `pan` FALLBACK IS DELIBERATE AND TEMPORARY" and closed with
    an instruction to remove it in a later PR -- describing, in the present tense
    and in capitals, a card-number read that the code below had already stopped
    doing. Anyone auditing PCI scope by reading this function would have found a
    PAN read that does not exist, and `docs/RUNBOOK-pan-cvv-contract.md` cited
    exactly this behaviour as the reason the migration was dangerous.

    The history, kept because the sequencing was the point: the fallback existed
    for the deployment window between `0029` back-filling `last4` and every
    instance running the new image. Deploys are not atomic, so a row could
    legitimately have `last4` NULL and a `pan` holding the only display value,
    and removing the read too early would have blanked the card column on every
    historical payment -- no error, just missing data. `0031` refuses to drop the
    columns until the back-fill is complete, which is what made removing it safe.

    Storing and displaying the last four digits is permitted under PCI-DSS;
    storing the PAN was what was not, and nothing here stores or reads one.
    `tests/test_pan_mask.py::test_the_display_never_reads_a_pan_attribute` raises
    on any attribute access other than `last4`, so a reinstated fallback fails
    loudly rather than quietly reviving card-number handling.
    """
    if payment.last4:
        return "•••• " + payment.last4
    # No fallback. The expand-phase read of `pan` was removed here, in the
    # contract step it was annotated for: 0031 refuses to drop the columns until
    # `last4` is back-filled for every row that had a PAN, so a row reaching this
    # line has no card digits recorded anywhere and there is nothing to fall back
    # TO. Returning None renders nothing rather than a placeholder a reader could
    # mistake for real digits.
    return None


def _proven_note_rate(loan) -> tuple:
    """(rate, proven) for a loan. One column, one answer.

    `loans.note_rate_pct` says what it holds and is NOT NULL since the contract
    step (db/migrations/0039), which refused to run while any loan lacked a
    proven rate. So there is nothing left to infer and nothing left to fall back
    to -- the tuple's second element is kept because callers branch on it, and
    because a future source of unproven rates would need it again.

    **What this used to be, because the sequence is the point.** It read
    `loans.apr` and reported it only when `schedule_version` proved the boarding
    path had written a contractual rate there -- `apr` held the DISCLOSED APR
    under the pre-change path (5.196% for a contract priced at 7.99%), so
    reporting it unconditionally would have stated a term the borrower never
    agreed to. 0038 moved that inference into the data, 0039 removed the column
    it was inferring from, and this is what is left.
    """
    return float(loan.note_rate_pct), True


@router.get("", response_model=Page[LoanListItem])
def list_loans(
    session: Session = Depends(get_session),
    status: str | None = Query(default=None),
    loan_id: int | None = Query(default=None, ge=1),
    order: Literal["newest", "oldest"] = Query(default="newest"),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """The serviced portfolio, filtered and ordered by the server.

    **Newest first by default, and `loan_id` filters the whole portfolio.**
    Both exist because a loan could board correctly and still be unfindable:
    ids ascend at boarding, the page holds 25, so a freshly boarded loan landed
    on the last page -- rank 192 of 192 in the case that prompted this -- and the
    UI's search box filtered only the rows already fetched, so typing the id on
    page 1 found nothing. The loan was right; the list was the defect.

    Ordering is on `id`, not `opened_at`. `id` is the primary key and is assigned
    monotonically at boarding, so it is both "most recently boarded" and a total
    order. `opened_at` is neither: the seeded portfolio holds 10 distinct
    timestamps across 184 loans, and a non-unique sort key under LIMIT/OFFSET
    lets rows repeat on one page and vanish from the next.

    `order` is a `Literal`, mapped here to an explicit SQLAlchemy expression --
    no column or direction ever arrives from the caller as a string.

    Both filters are applied to the count as well as the page, so `total`
    describes the filtered set the caller is paging through rather than the
    table.
    """
    stmt = select(models.Loan, models.Balance).join(
        models.Balance, models.Balance.loan_id == models.Loan.id, isouter=True
    )
    count_stmt = select(func.count(models.Loan.id))
    if status and status != "all":
        stmt = stmt.where(models.Loan.status == status)
        count_stmt = count_stmt.where(models.Loan.status == status)
    if loan_id is not None:
        # An id that does not exist is an empty page, not a 404: this is a list
        # endpoint answering "which loans match", and none matching is an answer.
        stmt = stmt.where(models.Loan.id == loan_id)
        count_stmt = count_stmt.where(models.Loan.id == loan_id)
    total = session.scalar(count_stmt) or 0
    ordering = models.Loan.id.desc() if order == "newest" else models.Loan.id.asc()
    stmt = stmt.order_by(ordering).limit(limit).offset(offset)
    items = [
        LoanListItem(
            id=loan.id, applicant_name=loan.applicant_name, principal=loan.principal,
            note_rate_pct=_proven_note_rate(loan)[0],
            note_rate_proven=_proven_note_rate(loan)[1],
            term_months=loan.term_months, status=loan.status,
            balance=(bal.balance if bal else 0.0), past_due=(bal.past_due if bal else 0.0),
            opened_at=loan.opened_at.isoformat() if loan.opened_at else None,
        )
        for loan, bal in session.execute(stmt).all()
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{loan_id}", response_model=LoanDetail)
def get_loan(loan_id: int, session: Session = Depends(get_session)):
    loan = session.get(models.Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="loan not found")
    bal = session.get(models.Balance, loan_id)
    rate, proven = _proven_note_rate(loan)
    return LoanDetail(
        id=loan.id, applicant_name=loan.applicant_name, principal=loan.principal,
        note_rate_pct=rate, note_rate_proven=proven,
        term_months=loan.term_months, status=loan.status,
        balance=(bal.balance if bal else 0.0), past_due=(bal.past_due if bal else 0.0),
        opened_at=loan.opened_at.isoformat() if loan.opened_at else None,
    )


@router.get("/{loan_id}/schedule", response_model=ScheduleOut)
def loan_schedule(loan_id: int, session: Session = Depends(get_session)):
    loan = session.get(models.Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="loan not found")
    # Bill the contract that was boarded, not a fresh solve of it.
    #
    # This used to always call amortization(principal, apr, term), which
    # re-derives the payment at read time. That made an accepted disclosure
    # something the system recomputed rather than something it stored, so a
    # later rounding-policy change silently re-wrote the terms of a signed
    # loan. Under Model B the recomputation cannot reproduce the contract at
    # all: the final payment absorbs the cent residue and is not a function of
    # principal, rate and term.
    #
    # loans_schedule_all_or_nothing (db/migrations/0030) guarantees the four
    # columns are either all present or all absent, so testing one is a sound
    # test of the group.
    if loan.schedule_version is not None:
        rows = schedule.amortization_from_contract(
            # The NOTE RATE, explicitly. This read was `loan.apr` -- correct for
            # a loan boarded by the current path and wrong for a legacy one,
            # where that column held the disclosed APR and the schedule would
            # have been expanded at a rate the borrower was never quoted.
            loan.principal, loan.note_rate_pct, loan.term_months,
            loan.regular_payment, loan.final_payment,
        )
        # The closing balance is not clamped, so a contract whose stored amounts
        # do not amortize the principal shows up here instead of being absorbed.
        residue = rows[-1]["balance"] if rows else 0.0
        if abs(residue) >= 0.01:
            log.error(
                "stored schedule does not amortize principal loan_id=%s residue=%s "
                "schedule_version=%s", loan_id, residue, loan.schedule_version,
            )
            return ScheduleOut(
                loan_id=loan_id, schedule=[ScheduleRow(**r) for r in rows],
                source="contract", schedule_version=loan.schedule_version,
                unamortized_residue=residue,
                note=(
                    "The payment amounts recorded for this loan do not fully "
                    f"amortize its principal; {abs(residue):.2f} remains "
                    "unaccounted for. The amounts shown are the ones on record. "
                    "This is a data defect and needs investigation before the "
                    "final payment is taken."
                ),
            )
        return ScheduleOut(
            loan_id=loan_id, schedule=[ScheduleRow(**r) for r in rows],
            source="contract", schedule_version=loan.schedule_version,
        )

    # Legacy: boarded before 0030, so no schedule was ever recorded and 0030
    # deliberately does not back-fill one. Reconstruct it -- a borrower still
    # needs to see what they owe -- but say plainly that it is a reconstruction.
    # Claiming these are the agreed terms is the specific dishonesty this branch
    # exists to avoid; the reconstruction may differ from what was actually
    # billed if the generator has changed since.
    # Reconstructed at the NOTE RATE. Before 0039 this used `loan.apr`, which
    # for exactly the loans reaching this branch -- no schedule on record, i.e.
    # the legacy ones -- was the figure most likely to be a disclosed APR. The
    # reconstruction was being built at the wrong rate for the rows least able to
    # afford it, which is why 0039 refused to drop the column until every loan
    # carried a proven note rate instead.
    rows = schedule.amortization(loan.principal, loan.note_rate_pct, loan.term_months)
    log.info(
        "reconstructed schedule for a pre-0030 loan loan_id=%s -- no contractual "
        "terms on record", loan_id,
    )
    return ScheduleOut(
        loan_id=loan_id, schedule=[ScheduleRow(**r) for r in rows],
        source="reconstructed", schedule_version=None,
        note=(
            "This loan was boarded before its contractual payment schedule was "
            "recorded, so no stored schedule exists. The amounts below are "
            "reconstructed from the principal, rate and term using the current "
            "generator. They are an estimate of the agreed terms, not the "
            "agreed terms themselves, and may differ from what was billed."
        ),
    )


def _allocations_by_payment(session: Session, loan_id: int) -> dict:
    """What each payment actually paid, read from the ledger it wrote.

    Returns {payment_id: {component: amount}} for this loan's `payment` entries,
    with amounts flipped to positive: a payment is stored as a NEGATIVE delta
    because that is what it does to the balance, and "you paid -120.00 towards
    fees" is not a sentence to put in front of a borrower.

    **Read, never recomputed.** Calling `waterfall.allocate` here would produce a
    second opinion about a movement that already happened, and the two could
    disagree the moment a fee is waived or a schedule corrected -- the borrower
    would then be shown an allocation that never occurred. The ledger rows are
    what moved the balance; they are the only faithful answer.

    Scoped to `entry_type = 'payment'` on purpose. A fee assessment, a waiver and
    an approved adjustment all move the same components, and folding them in
    would report money the borrower did not pay as part of what they paid.
    """
    rows = session.execute(
        select(models.LedgerEntry.payment_id,
               models.LedgerEntry.component,
               func.sum(models.LedgerEntry.amount))
        .where(models.LedgerEntry.loan_id == loan_id,
               models.LedgerEntry.entry_type == "payment",
               models.LedgerEntry.payment_id.isnot(None))
        .group_by(models.LedgerEntry.payment_id, models.LedgerEntry.component)
    ).all()
    allocations: dict = {}
    for payment_id, component, total in rows:
        allocations.setdefault(payment_id, {})[component] = -float(total)
    return allocations


@router.get("/{loan_id}/payments", response_model=PaymentsOut)
def loan_payments(loan_id: int, session: Session = Depends(get_session)):
    rows = session.scalars(
        select(models.Payment).where(models.Payment.loan_id == loan_id)
        .order_by(models.Payment.created_at.desc())
    ).all()
    allocations = _allocations_by_payment(session, loan_id)
    items = []
    for p in rows:
        # Absent, not zero. A payment with no ledger entries was applied before
        # the ledger existed (or never applied at all), and the honest answer to
        # "how much went to interest" is "we do not know", not "none".
        split = allocations.get(p.id)
        items.append(PaymentItem(
            id=p.id, amount=p.amount, method=p.method, masked_pan=_display_last4(p),
            created_at=p.created_at.isoformat() if p.created_at else None,
            applied_to_fees=split.get("fees", 0.0) if split else None,
            applied_to_interest=split.get("interest", 0.0) if split else None,
            applied_to_principal=split.get("principal", 0.0) if split else None,
            auth_status=p.auth_status,
            applied=p.applied_at is not None,
        ))
    return PaymentsOut(loan_id=loan_id, items=items)


def _dec(value):
    """Exact cents for the one place this module sums money.

    `models.LedgerEntry.amount` is mapped `asdecimal=False`, so SQLAlchemy hands
    back a float regardless of the NUMERIC(14,2) column. Summing three of those
    for a payment's components and comparing the total against the payment is how
    a cent goes missing on a borrower's screen. `Decimal(str(x))` recovers the
    exact stored cent amount, the same boundary `disclosure-service` draws.
    """
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --- account activity ---------------------------------------------------------
#
# "What authoritative movements changed this account?" -- a different question
# from `/payments` above, which asks "what payments did I make and where did each
# one go". Keeping them apart is deliberate: an approved adjustment and a fee
# waiver change the account without being payments, and folding them into payment
# history would make a staff correction look like a card charge the borrower made.


#: `ledger_entries.entry_type` -> (category, description, provenance).
#:
#: **The raw type never leaves the server.** `legacy_direct_write` is the name of
#: a mechanism -- a balance change captured by the 0035 trigger from a direct
#: UPDATE that predates the ledger -- and it is meaningless to a borrower and
#: alarming to anyone who guesses. It maps to a truthful category with its
#: provenance marked `limited`, because that row genuinely cannot name an actor or
#: a reason: the trigger could prove what changed and in which transaction, not
#: who did it or why.
_CATEGORIES = {
    "payment":             ("payment", "Payment received", "processor"),
    "adjustment":          ("adjustment", "Approved balance adjustment", "recorded"),
    "fee_assessed":        ("fee", "Fee assessed", "recorded"),
    "fee_waived":          ("fee_waiver", "Fee waived", "recorded"),
    "disbursement":        ("disbursement", "Loan funded", "recorded"),
    "opening_balance":     ("opening_balance", "Opening balance when the ledger began", "limited"),
    "legacy_direct_write": ("balance_change", "Recorded balance change", "limited"),
}

#: What the list is, and is not, carried in the payload rather than only in the
#: UI -- a client that renders it under a heading of its own choosing still ships
#: the sentence.
_ACTIVITY_NOTE = (
    "Authoritative movements that changed this account, read from the immutable "
    "ledger. A proposal that has not been approved moves no money and does not "
    "appear here."
)


@router.get("/{loan_id}/activity", response_model=ActivityOut)
def loan_activity(loan_id: int, session: Session = Depends(get_session)):
    """Every authoritative movement on this loan, grouped by what caused it.

    **Read, never recomputed.** The ledger is the record of what moved; deriving
    activity from balances and payments would produce a second account of the
    same history, free to disagree with the first. Nothing here writes, and
    nothing here touches `payments.processor_ref`, `captured_at`,
    `capture_source` or `auth_status` -- the columns Week 7 reconciliation reads.
    An approved adjustment appears in this list and creates no processor capture,
    because no processor money moved.

    **Grouped by `payment_id`, which is authoritative identity.** One $500 card
    payment writes up to three ledger rows -- fees, interest, principal -- and
    listing them separately would show a borrower three charges they did not
    make. Grouping by amount, timestamp or minute would be worse than useless:
    two legitimate payments can share all three, and the duplicate-review work
    (D22) exists precisely because same-loan-same-amount is not identity.

    **Borrower-safe by construction, not by sanitisation.** `reason`, `actor_id`,
    `actor_role` and `correlation_id` are never selected. Staff-entered reason
    text carries internal operations and compliance language, and the only
    identity this route can see is an unsigned `X-User-Role` the gateway
    forwards -- not a verified principal. Gating PII on a header a direct caller
    could assert would be the weaker arrangement; so this route has one
    representation and it is the safe one. Staff provenance, if it is wanted
    later, belongs behind `require_staff_principal` like every other privileged
    read in this service.
    """
    rows = session.execute(
        select(models.LedgerEntry.id, models.LedgerEntry.entry_type,
               models.LedgerEntry.component, models.LedgerEntry.amount,
               models.LedgerEntry.payment_id, models.LedgerEntry.occurred_at)
        .where(models.LedgerEntry.loan_id == loan_id)
        .order_by(models.LedgerEntry.occurred_at.desc(),
                  models.LedgerEntry.id.desc())
    ).all()

    # Keyed by authoritative identity: the payment when there is one, otherwise
    # this single ledger row. An entry_type is part of the key as well as the
    # payment id -- a fee assessed against a payment and the payment itself are
    # two movements even where a row carries both.
    grouped: dict = {}
    order: list = []
    for entry_id, entry_type, component, amount, payment_id, occurred_at in rows:
        key = ("payment", payment_id) if payment_id is not None else ("entry", entry_id)
        if key not in grouped:
            category, description, provenance = _CATEGORIES.get(
                entry_type, ("balance_change", "Recorded balance change", "limited"))
            grouped[key] = {
                "id": "%s:%s" % key,
                "occurred_at": occurred_at.isoformat() if occurred_at else None,
                "category": category,
                "description": description,
                "amount": 0.0,
                "components": {},
                "payment_id": payment_id,
                "provenance": provenance,
            }
            order.append(key)
        item = grouped[key]
        # Decimal on the way in, float only at the boundary: several rows are
        # summed here, and cent errors compound across a group.
        item["components"][component] = float(
            _dec(item["components"].get(component, 0.0)) + _dec(amount))
        item["amount"] = float(_dec(item["amount"]) + _dec(amount))

    return ActivityOut(
        loan_id=loan_id,
        items=[ActivityItem(**grouped[key]) for key in order],
        note=_ACTIVITY_NOTE,
    )
