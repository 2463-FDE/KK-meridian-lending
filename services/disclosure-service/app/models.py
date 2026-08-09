"""SQLAlchemy ORM model for the offers table.

D12 fix: money columns map to Numeric now (NUMERIC in Postgres, not DOUBLE
PRECISION) -- exact base-10 decimal storage, not a binary float.
`asdecimal=False` keeps the Python-side value a plain float (matching
apr.py's own Decimal-internally/float-at-the-boundary pattern), so this is a
storage-layer fix, not a ripple of Decimal typing through every caller that
reads these columns. The disclosure-service reads/writes the same `offers`
table the LOS does.
"""
from sqlalchemy import String, DateTime, ForeignKey, Integer, Numeric
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
    # The contractual interest rate the payment stream is priced at (db/migrations
    # /0030). Distinct from `apr`, which additionally carries the prepaid fee.
    # Nullable only because offers created before 0030 have no stored value --
    # those, and only those, fall back to apr.note_rate_from_payment().
    note_rate_pct: Mapped[float | None] = mapped_column(Numeric(7, 3, asdecimal=False), nullable=True)
    apr: Mapped[float | None] = mapped_column(Numeric(7, 3, asdecimal=False), nullable=True)
    finance_charge: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    monthly_payment: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    amount_financed: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    total_of_payments: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    # Contractual payment schedule, stored as fact (db/migrations/0030). NULL on
    # legacy rows means "never recorded" -- boarding refuses those rather than
    # regenerating terms with current code.
    regular_payment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_payment: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    term_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_version: Mapped[str | None] = mapped_column(String, nullable=True)
    # The principal the schedule was calculated on (db/migrations/0030). Stored
    # because amount_financed is cent-rounded and does not invert back to it.
    principal: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # db/migrations/0021. Non-NULL means the borrower is bound to these terms:
    # the offer is immutable from that point, including to the repair path.
    accepted_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
