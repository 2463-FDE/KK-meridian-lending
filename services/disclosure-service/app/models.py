"""SQLAlchemy ORM model for the offers table.

D12 fix: money columns map to Numeric now (NUMERIC in Postgres, not DOUBLE
PRECISION) -- exact base-10 decimal storage, not a binary float.
`asdecimal=False` keeps the Python-side value a plain float (matching
apr.py's own Decimal-internally/float-at-the-boundary pattern), so this is a
storage-layer fix, not a ripple of Decimal typing through every caller that
reads these columns. The disclosure-service reads/writes the same `offers`
table the LOS does.
"""
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    # unique: one canonical offer per decision -- makes offer creation idempotent
    # against a retried/duplicated create_offer call (W4 review fix).
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.app_id"), unique=True, nullable=True)
    fee_pct_used: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False), nullable=True)
    apr: Mapped[float | None] = mapped_column(Numeric(7, 3, asdecimal=False), nullable=True)
    finance_charge: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    monthly_payment: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    amount_financed: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    total_of_payments: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # db/migrations/0021. Non-NULL means the borrower is bound to these terms:
    # the offer is immutable from that point, including to the repair path.
    accepted_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
