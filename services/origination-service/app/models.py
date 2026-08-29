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
    # Part of the postal address, and nothing more. It was added in
    # `db/migrations/0014_add_applicant_zip.sql` to back a ZIP3 fair-lending
    # screen; the client prohibited ZIP and ZIP3 as a protected-class proxy on
    # 2026-08-24, that screen is retired, and no runtime path groups decisions by
    # this field any more. Kept because an address without a ZIP is an
    # incomplete address, not because fairness analysis needs it.
    zip_code: Mapped[str | None] = mapped_column(String, nullable=True)
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
    # Which application this CIP result was run for (db/migrations/0032).
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    name_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dob_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    address_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ssn_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # The CIP verdict (db/migrations/0033). NULL on rows written before it.
    cip_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
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
    # The persisted Model B contractual schedule (db/migrations/0030). Declared
    # for the same reason note_rate_pct is, one line of code above: these are
    # read through getattr() by the boarding gate, and an undeclared column
    # reads as None no matter what SQL holds. That is not a theoretical risk --
    # omitting them here made every freshly generated offer report
    # "missing=regular_payment_count,final_payment,term_months,schedule_version"
    # against a row where all four were populated, which disabled Accept & board
    # in the UI and failed the borrower-workflow e2e. The whole-service unit
    # suite stayed green throughout, because its offer rows are constructed
    # objects that carry the attributes whether the model declares them or not;
    # only Postgres-backed reads go through this mapping.
    # test_models.py::test_boarding_required_fields_are_all_mapped_on_the_offer_model
    # now fails at unit speed if a future canonical field is added without one.
    regular_payment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_payment: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    # The CONTRACTUAL term the schedule above was solved for -- not
    # applications.term_months, which is only what was requested.
    term_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_version: Mapped[str | None] = mapped_column(String, nullable=True)
    # The principal the stored schedule was solved for. Boarding copies the
    # stored payments, so it must open the loan at THIS principal rather than at
    # the application's requested amount (db/migrations/0030).
    principal: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the borrower accepted, or NULL if they have not. Declared for the same
    # reason as the columns above and found the same way: the application
    # lifecycle reads it to tell "offer issued" from "offer accepted", and with
    # the column undeclared that read raised `AttributeError: 'Offer' object has
    # no attribute 'accepted_at'` against real Postgres while the unit suite
    # stayed green -- its offers are constructed objects that carry whatever
    # attribute a test gives them.
    accepted_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
