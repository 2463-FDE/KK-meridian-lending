"""SQLAlchemy ORM models for the LSS tables.

D12 fix: money columns now map to Numeric (NUMERIC in Postgres, not DOUBLE
PRECISION) -- the underlying storage is exact base-10 decimal now, not a
binary float. `asdecimal=False` keeps the Python-side value a plain float
(matching balance.py's own Decimal-internally/float-at-the-boundary pattern)
so this is a storage-layer fix, not a ripple of Decimal typing through every
caller that reads these columns.

The `balances` table is still a single mutable balance column (no ledger).
The `payments` table still carries the full PAN + CVV (PCI debt) and has no
idempotency key.
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
    pan: Mapped[str | None] = mapped_column(String, nullable=True)   # full PAN stored (debt)
    cvv: Mapped[str | None] = mapped_column(String, nullable=True)   # CVV stored (debt)
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    method: Mapped[str | None] = mapped_column(String, default="card")
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
