"""SQLAlchemy ORM model for the payments table.

Not actually used anywhere in this service (payment-service reads/writes
`payments` via raw psycopg2 -- see db.py/payments.py) -- kept only as schema
documentation, mirroring servicing-service's own Payment model since this
service was split out of servicing-service's payments.py.

D12 fix: amount maps to Numeric now (NUMERIC in Postgres, not DOUBLE
PRECISION) -- exact base-10 decimal storage. `asdecimal=False` keeps the
Python-side value a plain float, matching payments.py's own
Decimal-internally/float-at-the-boundary quantization.

ADR 0008 (Week 5 tokenization): `pan`/`cvv` are legacy, nullable, dead-
going-forward columns for rows that predate tokenization -- no code path
writes to them anymore. New rows populate `last4`/`brand` instead, from the
processor's own token response.

Review fix: `auth_status` ('pending' | 'captured' | 'failed', db/migrations/
0017) tracks whether a real processor authorization was ever confirmed for
this row -- see app/processor.py::authorize_charge().

Review fix: `authorization_id` (db/migrations/0019) is the processor's own
authorization id, persisted in the same UPDATE that flips auth_status to
'captured' -- see app/processor.py::get_authorization().
"""
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), nullable=True)
    pan: Mapped[str | None] = mapped_column(String, nullable=True)   # legacy rows only (debt)
    cvv: Mapped[str | None] = mapped_column(String, nullable=True)   # legacy rows only (debt)
    last4: Mapped[str | None] = mapped_column(String, nullable=True)
    brand: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    method: Mapped[str | None] = mapped_column(String, default="card")
    auth_status: Mapped[str] = mapped_column(String, default="captured")
    authorization_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
