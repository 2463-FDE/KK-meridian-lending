"""Loan portfolio read API: list, detail, amortization schedule, payment history."""
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

router = APIRouter(prefix="/loans", tags=["loans"])


def _display_last4(payment) -> str | None:
    """Masked card for payment history. Never returns more than four digits.

    ADR 0008: new rows carry `last4` directly, from the processor's token
    response rather than from a card number this service never sees.

    THE `pan` FALLBACK IS DELIBERATE AND TEMPORARY (expand phase, PR #11).
    An earlier version of this PR removed it on the grounds that
    db/migrations/0029 back-fills `last4` from `pan`. Automated review caught the
    ordering assumption: nothing in this change enforces that the migration has
    run before this service version serves traffic. Deploys are not atomic, so
    there is a real window -- new code live, 0029 not yet applied, or applied to a
    replica but not the primary -- in which `last4` is NULL and `pan` holds the
    only display value. Removing the fallback there blanks the card column on
    every historical payment: no error, just missing data, exactly the failure the
    back-fill exists to prevent.

    So the order is: read `last4`; fall back to the last four digits of `pan` only
    when `last4` is absent. Storing and displaying the last four digits is
    permitted under PCI-DSS -- storing the PAN is what was not, and this only
    reads a column that already exists.

    REMOVE THIS FALLBACK IN PR #15, the contract step, after 0029 is deployed and
    verified. At that point `pan` no longer exists and the branch is dead code.
    """
    if payment.last4:
        return "•••• " + payment.last4
    # Expand-phase compatibility only. Guarded with getattr so this keeps working
    # after 0031 drops the column and the attribute disappears from the model.
    legacy_pan = getattr(payment, "pan", None)
    if legacy_pan:
        return "•••• " + str(legacy_pan)[-4:]
    return None


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
            apr=loan.apr, term_months=loan.term_months, status=loan.status,
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
    return LoanDetail(
        id=loan.id, applicant_name=loan.applicant_name, principal=loan.principal,
        apr=loan.apr, term_months=loan.term_months, status=loan.status,
        balance=(bal.balance if bal else 0.0), past_due=(bal.past_due if bal else 0.0),
        opened_at=loan.opened_at.isoformat() if loan.opened_at else None,
    )


@router.get("/{loan_id}/schedule", response_model=ScheduleOut)
def loan_schedule(loan_id: int, session: Session = Depends(get_session)):
    loan = session.get(models.Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="loan not found")
    rows = schedule.amortization(loan.principal, loan.apr, loan.term_months)
    return ScheduleOut(loan_id=loan_id, schedule=[ScheduleRow(**r) for r in rows])


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
