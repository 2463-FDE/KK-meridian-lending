"""SQLAlchemy ORM model for the payments table.

Not actually used anywhere in this service (payment-service reads/writes
`payments` via raw psycopg2 -- see db.py/payments.py) -- kept only as schema
documentation, mirroring servicing-service's own Payment model since this
service was split out of servicing-service's payments.py.

D12 fix: amount maps to Numeric now (NUMERIC in Postgres, not DOUBLE
PRECISION) -- exact base-10 decimal storage. `asdecimal=False` keeps the
Python-side value a plain float, matching payments.py's own
Decimal-internally/float-at-the-boundary quantization. The `payments` table
still carries the full PAN + CVV (PCI debt) and has no idempotency key.
"""
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), nullable=True)
    pan: Mapped[str | None] = mapped_column(String, nullable=True)   # full PAN stored (debt)
    cvv: Mapped[str | None] = mapped_column(String, nullable=True)   # CVV stored (debt)
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    method: Mapped[str | None] = mapped_column(String, default="card")
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
