"""SQLAlchemy ORM models for the LSS tables.

D12 fix: money columns now map to Numeric (NUMERIC in Postgres, not DOUBLE
PRECISION) -- the underlying storage is exact base-10 decimal now, not a
binary float. `asdecimal=False` keeps the Python-side value a plain float
(matching balance.py's own Decimal-internally/float-at-the-boundary pattern)
so this is a storage-layer fix, not a ripple of Decimal typing through every
caller that reads these columns.

The `balances` table is still a single mutable balance column (no ledger).

ADR 0008 (Week 5 tokenization) removed card storage entirely. This model no
longer declares `pan`/`cvv`, and payment-service never receives a raw PAN/CVV to
write here. The columns still exist in the database -- db/migrations/0029 (this
release) only back-fills `last4`; the DROP is the contract step,
db/migrations/0031, on its own PR. New rows populate
`last4`/`brand` instead.

Review fix: `auth_status` ('pending' | 'captured' | 'failed', db/migrations/
0017) tracks whether payment-service ever confirmed a real processor
authorization for this row -- see payment-service/app/processor.py.
"""
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applicant_name: Mapped[str | None] = mapped_column(String, nullable=True)
    principal: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    apr: Mapped[float] = mapped_column(Numeric(7, 3, asdecimal=False))
    term_months: Mapped[int] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String, default="current")
    opened_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Balance(Base):
    __tablename__ = "balances"

    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id"), primary_key=True)
    balance: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))  # single mutable column, no ledger (debt)
    past_due: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False), default=0)
    updated_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), nullable=True)
    last4: Mapped[str | None] = mapped_column(String, nullable=True)
    brand: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    method: Mapped[str | None] = mapped_column(String, default="card")
    auth_status: Mapped[str] = mapped_column(String, default="captured")
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
