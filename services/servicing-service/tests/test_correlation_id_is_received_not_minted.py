"""Servicing stamps the id it was GIVEN. It never invents one.

The half of the cross-service trace that lives here. payment-service mints one
identifier and sends it with the apply; this service writes it onto every ledger
entry the payment produces, so "show me this charge" returns the whole
allocation -- fees, interest and principal -- rather than one row of it.

The defect worth guarding is not a missing id. It is a SECOND one. If servicing
generated its own when none arrived, or replaced what it was sent, both sides
would hold a perfectly good identifier and neither could find the other's rows.
Every log line would look right. That is the failure this file exists for, so
the assertions compare against the value that was sent rather than checking that
something id-shaped got stored.

`correlation_id` is inert here by design: nothing keys, joins, dedupes or
reconciles on it. `test_the_apply_still_works_without_one` is the proof -- a
rolling deploy where payment-service is older must still credit the borrower.
"""
from contextlib import contextmanager
from decimal import Decimal

import pytest

from app import balance


class _FakeCursor:
    """Stands in for the psycopg2 RealDictCursor db.transaction() now yields
    (review fix -- see db.py). apply_payment_once() runs its statements
    through this cursor's execute()/fetchall(), not db.query()."""

    def __init__(self, db):
        self._db = db
        self._last_result = []

    def execute(self, sql, params=None):
        self._last_result = self._db._run(sql, params)

    def fetchall(self):
        return self._last_result


def _D(v):
    return v if isinstance(v, Decimal) else Decimal(str(v))


class _FakeDb:
    """Stands in for app.db -- one balances row, plus a payment_applications
    table keyed on payment_id (PRIMARY KEY -> INSERT ... ON CONFLICT DO
    NOTHING only lands a row once per payment_id, mirroring the real unique
    constraint from db/migrations/0013). transaction() mimics real Postgres
    rollback: state changes made inside the block are reverted if it raises."""

    def __init__(self, balance=0.0, past_due=0.0):
        self.balance = balance
        # The waterfall (D14) reads what is owed before allocating. Zero fees
        # and no stored schedule mean nothing is owed but principal, so the
        # whole payment goes to principal -- which is what these idempotency
        # tests were written against and must keep asserting.
        self.past_due = past_due
        self.applications = {}
        self.ledger = set()
        self.ledger_params = []
        self.ledger_sql = []
        self.payment_statuses = {}

    def _run(self, sql, params=None):
        stmt = sql.strip()
        if stmt.startswith("SELECT auth_status FROM payments"):
            status = self.payment_statuses.get(params[0], "captured")
            return [{"auth_status": status}] if status is not None else []
        if stmt.startswith("INSERT INTO payment_applications"):
            payment_id, loan_id, amount = params
            if payment_id in self.applications:
                return []  # ON CONFLICT DO NOTHING -- already applied
            self.applications[payment_id] = (loan_id, amount)
            return [{"payment_id": payment_id}]
        if stmt.startswith("SELECT pa.loan_id"):
            payment_id = params[0]
            loan_id, amount = self.applications[payment_id]
            return [{"loan_id": loan_id, "amount": amount,
                     "auth_status": self.payment_statuses.get(payment_id, "captured"),
                     "balance": self.balance}]
        if stmt.startswith("SELECT balance"):
            return [{"balance": self.balance}]
        if stmt.startswith("SELECT l.principal"):
            # The loan the waterfall reads. `schedule_version` is None, so no
            # contractual interest can be derived and none is owed.
            return [{"principal": self.balance, "note_rate_pct": 7.99,
                     "term_months": 48, "regular_payment": None,
                     "final_payment": None, "schedule_version": None,
                     "opened_at": None, "balance": self.balance,
                     "past_due": self.past_due}]
        if "COALESCE(-SUM(amount), 0)" in stmt:
            return [{"paid": 0}]
        if stmt.startswith("INSERT INTO ledger_entries"):
            # One row per component since the waterfall landed, so the key is
            # (payment_id, component) -- mirroring the real unique index from
            # db/migrations/0035, which is per component and not per payment
            # precisely so a payment can be split.
            # correlation_id joined the ledger INSERT with db/migrations/0043.
            loan_id, component, amount, payment_id, correlation_id = params
            self.ledger_params.append(params)
            self.ledger_sql.append(" ".join(sql.split()))
            if (payment_id, component) in self.ledger:
                raise RuntimeError("duplicate ledger payment component")
            self.ledger.add((payment_id, component))
            # The real column is NUMERIC and the entry amount arrives as a
            # Decimal, so the fake keeps the same type rather than mixing.
            if component == "principal":
                self.balance = float(_D(self.balance) + _D(amount))
            elif component == "fees":
                self.past_due = float(_D(self.past_due) + _D(amount))
            return []
        if "SET balance" in stmt:
            self.balance = params[0]
            return []
        raise AssertionError(f"unexpected query: {sql}")

    def query(self, sql, params=None):
        return self._run(sql, params)

    @contextmanager
    def transaction(self):
        snapshot_balance = self.balance
        snapshot_applications = dict(self.applications)
        snapshot_ledger = set(self.ledger)
        try:
            yield _FakeCursor(self)
        except Exception:
            self.balance = snapshot_balance
            self.applications = snapshot_applications
            self.ledger = snapshot_ledger
            raise




@pytest.fixture
def db(monkeypatch):
    """The same fake `test_apply_payment_idempotency.py` uses, so this file and
    that one cannot disagree about what the apply path does -- with the ledger
    parameters recorded, which is what these assertions read."""
    fake = _FakeDb(balance=100.0)
    monkeypatch.setattr(balance, "db", fake)
    return fake


def _apply(db, correlation_id):
    return balance.apply_payment_once(7, 1, 10.0, correlation_id=correlation_id)


def _ledger_params(db):
    assert db.ledger_params, "the apply wrote no ledger entry"
    return db.ledger_params


def test_the_ledger_entry_carries_the_id_it_was_sent(db):
    _apply(db, "pay_fromtheotherservice")

    for params in _ledger_params(db):
        assert params[-1] == "pay_fromtheotherservice", (
            "servicing stored %r instead of the id payment-service sent" % (params[-1],)
        )


def test_every_component_of_one_payment_shares_the_id(db):
    """One payment can write three entries. All three must carry the same id, or
    "show me this charge" returns part of the allocation and the operator draws
    the wrong conclusion from a complete-looking answer."""
    _apply(db, "pay_onetrace")

    ids = {params[-1] for params in _ledger_params(db)}
    assert ids == {"pay_onetrace"}, ids


def test_the_insert_names_the_column_rather_than_relying_on_position(db):
    """A positional guard: if the column list and the parameter tuple ever drift,
    the id silently lands in another column."""
    _apply(db, "pay_positional")
    sql = db.ledger_sql[0]
    params = _ledger_params(db)[0]

    columns = sql.split("(", 1)[1].split(")", 1)[0]
    names = [c.strip() for c in columns.split(",")]
    assert names[-1] == "correlation_id", names

    # Placeholders, not column count: `entry_type` is written as a literal
    # 'payment' in the VALUES list, so the two differ by one BY DESIGN. Counting
    # columns here would have made this test fail on correct code, which is how a
    # guard gets weakened or deleted.
    placeholders = sql.split("VALUES", 1)[1].count("%s")
    assert placeholders == len(params), (sql, params)


def test_the_apply_still_works_without_one(db):
    """Inert by design.

    A rolling deploy can put an older payment-service in front of this one, and
    a captured charge must still reach the borrower's balance. NULL means "no
    trace", which is true, rather than an error or an invented id.
    """
    new_balance, applied = _apply(db, None)

    assert applied is True
    assert new_balance is not None
    for params in _ledger_params(db):
        assert params[-1] is None


def test_servicing_mints_nothing_of_its_own(db):
    """The adversarial case, stated directly.

    If this service ever generated an id when none arrived, both sides would
    hold a good-looking identifier and neither could find the other's rows.
    """
    _apply(db, None)

    stored = {params[-1] for params in _ledger_params(db)}
    assert stored == {None}, (
        "servicing invented a correlation id (%r) instead of recording that "
        "there was none" % (stored,)
    )


def test_the_guard_would_notice_a_replaced_id(db):
    """Guard the guard.

    Every assertion above compares against a value chosen by the test. If the
    parameter were read from the wrong position, or the column dropped, these
    comparisons must fail rather than pass on a coincidence -- so this asserts
    the negative directly.
    """
    _apply(db, "pay_expected")

    stored = {params[-1] for params in _ledger_params(db)}
    assert "pay_somethingelse" not in stored
    assert stored == {"pay_expected"}
