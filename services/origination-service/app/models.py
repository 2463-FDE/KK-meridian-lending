"""SQLAlchemy ORM models for the LOS tables.

D12 fix: money columns map to Numeric now (NUMERIC in Postgres, not DOUBLE
PRECISION) -- exact base-10 decimal storage. `asdecimal=False` keeps the
Python-side value a plain float, so this is a storage-layer fix, not a
ripple of Decimal typing through every caller that reads these columns. The
`decisions` table is mapped exactly as it exists: outcome only, no reason /
no model drivers / no timestamp (the missing decision audit trail).
"""
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Applicant(Base):
    __tablename__ = "applicants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    dob: Mapped[str | None] = mapped_column(Date, nullable=True)
    ssn: Mapped[str | None] = mapped_column(String, nullable=True)  # plaintext (debt)
    ein: Mapped[str | None] = mapped_column(String, nullable=True)
    is_entity: Mapped[bool] = mapped_column(Boolean, default=False)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    zip_code: Mapped[str | None] = mapped_column(String, nullable=True)  # W8: fair-lending ZIP-level check
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    applicant_id: Mapped[int | None] = mapped_column(ForeignKey("applicants.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    term_months: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str | None] = mapped_column(String, nullable=True)
    income: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    employer: Mapped[str | None] = mapped_column(String, nullable=True)
    job_title: Mapped[str | None] = mapped_column(String, nullable=True)
    employment_years: Mapped[float | None] = mapped_column(Float, nullable=True)  # a duration, not money -- left as-is
    status: Mapped[str | None] = mapped_column(String, default="submitted")
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)

    applicant: Mapped[Applicant | None] = relationship(lazy="joined")


class KycCheck(Base):
    __tablename__ = "kyc_checks"
    # CIP only — no sanctions_screened / ubo_identified / ongoing_monitoring columns (debt).
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    applicant_id: Mapped[int | None] = mapped_column(ForeignKey("applicants.id"), nullable=True)
    name_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dob_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    address_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ssn_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Decision(Base):
    __tablename__ = "decisions"
    # OUTCOME ONLY. No reason, no drivers, no inputs, no model-run timestamp. (debt D4)
    app_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), primary_key=True)
    outcome: Mapped[str] = mapped_column(String)


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    # unique: one canonical offer per decision -- makes offer creation idempotent
    # against a retried/duplicated create_offer call (W4 review fix).
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.app_id"), unique=True, nullable=True)
    fee_pct_used: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False), nullable=True)
    # The contractual note rate (db/migrations/0030). Declared here because
    # _offer_disclosure_or_none() and _complete_offer_exists() treat it as a
    # canonical term via getattr -- an undeclared column reads as None, so a
    # perfectly good offer was reported "missing=note_rate_pct" and refused.
    note_rate_pct: Mapped[float | None] = mapped_column(Numeric(7, 3, asdecimal=False), nullable=True)
    apr: Mapped[float | None] = mapped_column(Numeric(7, 3, asdecimal=False), nullable=True)
    finance_charge: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    monthly_payment: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    amount_financed: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    total_of_payments: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
