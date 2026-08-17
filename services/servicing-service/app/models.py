"""SQLAlchemy ORM models for the LSS tables.

D12 fix: money columns now map to Numeric (NUMERIC in Postgres, not DOUBLE
PRECISION) -- the underlying storage is exact base-10 decimal now, not a
binary float. `asdecimal=False` keeps the Python-side value a plain float
(matching balance.py's own Decimal-internally/float-at-the-boundary pattern)
so this is a storage-layer fix, not a ripple of Decimal typing through every
caller that reads these columns.

`balances` is a PROJECTION, not the system of record. ADR 0010 step 2
(db/migrations/0035_ledger_entries.sql) made `ledger_entries` the immutable
record of every movement, and `project_ledger_entry()` maintains the two columns
below by composing signed deltas. This docstring said "still a single mutable
balance column (no ledger)" for as long as the ledger has existed.

What is still true, and is why the model looks unchanged: the columns are read
the same way, and the projection keeps them current.

**The remaining direct writers are `balance.apply_payment`, `adjust_balance`
and `waive_fee`, and none of them is reachable from a route.** `apply_payment`
was superseded by `apply_payment_once`; `adjust_balance` and `waive_fee` were
superseded by the maker-checker proposal flow, where the APPROVAL writes the
ledger entry. They are unreferenced code that would still UPDATE these columns
if anything called them.

This list previously named those three and omitted `delinquency.assess_late_fee`,
which was the one direct writer a route could actually reach -- a writer list
that reads complete while missing one, which is the defect shape this repository
keeps producing. It now writes a `fee_assessed` ledger entry instead (ADR 0010
step 3).

ADR 0010's guard against direct writes stays disabled until the three above are
retired or converted. Until then the compatibility bridge
(`balances_capture_legacy_delta`) records what they would move: it is created by
`db/migrations/0035` on an upgraded database and by `db/init/007` on a fresh
one, so both paths capture a stray direct write.

ADR 0008 (Week 5 tokenization) removed card storage entirely. This model no
longer declares `pan`/`cvv`, and payment-service never receives a raw PAN/CVV to
write here. **The columns are gone from the database too** --
db/migrations/0029 back-filled `last4`, db/migrations/0031 dropped both columns
behind an operator acknowledgement, and `db/init/001_schema.sql` creates neither,
so no migrated or freshly initialised database has them. This docstring described
the drop as future work ("the DROP is the contract step ... on its own PR")
after that PR had merged. New rows populate `last4`/`brand`.

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
    # D19 (db/migrations/0038 expand, 0039 contract). The contractual rate the
    # payment stream is priced at, under its own name. `apr` is gone: it held
    # this value on loans boarded by the current path and the DISCLOSED APR on
    # legacy ones, so the name was the defect.
    #
    # NOT NULL since 0039, which refused to drop `apr` while any loan lacked a
    # proven rate -- so a boarding path that forgets this column now fails at the
    # INSERT rather than creating a loan whose rate is unknown.
    note_rate_pct: Mapped[float] = mapped_column(Numeric(7, 3, asdecimal=False))
    term_months: Mapped[int] = mapped_column(Integer)
    # The Model B contractual schedule copied from the offer at boarding
    # (db/migrations/0030). Nothing in this service reads them yet -- billing
    # from the stored amounts instead of regenerating the schedule is the
    # remaining half of that work. They are declared now anyway: an undeclared
    # column reads as None through the ORM regardless of what SQL holds, and
    # this repository has now been bitten by that twice (servicing `pan` in
    # PR #11, origination's offer schedule columns in this one). Declaring the
    # column when the migration lands is what stops a third.
    regular_payment: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    regular_payment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_payment: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    schedule_version: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, default="current")
    opened_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Balance(Base):
    __tablename__ = "balances"

    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id"), primary_key=True)
    # Maintained by the ledger projection (db/migrations/0035), not by whoever
    # last wrote it. The comment here said "single mutable column, no ledger
    # (debt)" after the ledger landed.
    balance: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    past_due: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False), default=0)
    updated_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), nullable=True)
    # `pan` is deliberately NOT mapped. It was declared during the expand phase so
    # _display_last4() could fall back to a legacy PAN before 0029 back-filled
    # `last4`; that annotation said to remove it here, in the contract step, and
    # this is that removal. Mapping it would defeat the point of dropping it:
    # SQLAlchemy names every mapped column in its SELECT, so leaving this line in
    # would make every payment query fail the moment 0031 commits.
    last4: Mapped[str | None] = mapped_column(String, nullable=True)
    brand: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    method: Mapped[str | None] = mapped_column(String, default="card")
    auth_status: Mapped[str] = mapped_column(String, default="captured")
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
