"""Loan portfolio read API: list, detail, amortization schedule, payment history."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schedule
from ..database import get_session
from ..schemas import (
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
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(models.Loan, models.Balance).join(
        models.Balance, models.Balance.loan_id == models.Loan.id, isouter=True
    )
    count_stmt = select(func.count(models.Loan.id))
    if status and status != "all":
        stmt = stmt.where(models.Loan.status == status)
        count_stmt = count_stmt.where(models.Loan.status == status)
    total = session.scalar(count_stmt) or 0
    stmt = stmt.order_by(models.Loan.id).limit(limit).offset(offset)
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


@router.get("/{loan_id}/payments", response_model=PaymentsOut)
def loan_payments(loan_id: int, session: Session = Depends(get_session)):
    rows = session.scalars(
        select(models.Payment).where(models.Payment.loan_id == loan_id)
        .order_by(models.Payment.created_at.desc())
    ).all()
    items = [
        PaymentItem(
            id=p.id, amount=p.amount, method=p.method, masked_pan=_display_last4(p),
            created_at=p.created_at.isoformat() if p.created_at else None,
        )
        for p in rows
    ]
    return PaymentsOut(loan_id=loan_id, items=items)
