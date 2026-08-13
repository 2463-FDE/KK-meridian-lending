"""Tests for balance.apply_payment_once (db/migrations/0013).

Review finding: apply-payment applied the balance unconditionally on every
call, with no idempotency of its own -- it trusted payment-service to never
call it twice for the same payment. Once payment-service started retrying a
pending apply on a same-key retry, a duplicate call here (the retry itself,
or two requests racing) had to be guaranteed not to double-apply the same
payment_id. These tests exercise apply_payment_once() directly.

Review finding (follow-up): the marker INSERT and the balance UPDATE used to
each auto-commit on their own -- a balance-update failure after the marker
had already landed left a permanent marker with no balance ever applied,
silently skipping every future retry forever. test_apply_payment_once_rolls_
back_marker_and_retries_after_a_failed_balance_update covers the fix:
db.transaction() rolling both statements back together.
"""
from contextlib import contextmanager

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


class _FakeDb:
    """Stands in for app.db -- one balances row, plus a payment_applications
    table keyed on payment_id (PRIMARY KEY -> INSERT ... ON CONFLICT DO
    NOTHING only lands a row once per payment_id, mirroring the real unique
    constraint from db/migrations/0013). transaction() mimics real Postgres
    rollback: state changes made inside the block are reverted if it raises."""

    def __init__(self, balance=0.0):
        self.balance = balance
        self.applications = set()
        self.ledger = set()

    def _run(self, sql, params=None):
        stmt = sql.strip()
        if stmt.startswith("INSERT INTO payment_applications"):
            payment_id, loan_id, amount = params
            if payment_id in self.applications:
                return []  # ON CONFLICT DO NOTHING -- already applied
            self.applications.add(payment_id)
            return [{"payment_id": payment_id}]
        if stmt.startswith("SELECT balance"):
            return [{"balance": self.balance}]
        if stmt.startswith("INSERT INTO ledger_entries"):
            loan_id, amount, payment_id = params
            if payment_id in self.ledger:
                raise RuntimeError("duplicate ledger payment")
            self.ledger.add(payment_id)
            self.balance -= amount
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
        snapshot_applications = set(self.applications)
        snapshot_ledger = set(self.ledger)
        try:
            yield _FakeCursor(self)
        except Exception:
            self.balance = snapshot_balance
            self.applications = snapshot_applications
            self.ledger = snapshot_ledger
            raise


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDb(balance=100.0)
    monkeypatch.setattr(balance, "db", db)
    return db


def test_apply_payment_once_applies_on_first_call(fake_db):
    new_balance, applied = balance.apply_payment_once(payment_id=1, loan_id=5, amount=30.0)

    assert applied is True
    assert new_balance == 70.0
    assert fake_db.balance == 70.0


def test_apply_payment_once_is_a_noop_on_duplicate_payment_id(fake_db):
    """The exact scenario the review flagged: apply-payment called twice for
    the same payment_id (a payment-service retry, or a race) must move the
    balance once, not twice."""
    first_balance, first_applied = balance.apply_payment_once(payment_id=7, loan_id=5, amount=30.0)
    second_balance, second_applied = balance.apply_payment_once(payment_id=7, loan_id=5, amount=30.0)

    assert first_applied is True
    assert second_applied is False
    assert first_balance == second_balance == 70.0
    assert fake_db.balance == 70.0  # not 40.0 -- the second call never re-applied


def test_apply_payment_once_applies_separately_for_different_payment_ids(fake_db):
    balance.apply_payment_once(payment_id=1, loan_id=5, amount=30.0)
    _, applied = balance.apply_payment_once(payment_id=2, loan_id=5, amount=20.0)

    assert applied is True
    assert fake_db.balance == 50.0


def test_apply_payment_once_rolls_back_marker_and_retries_after_a_failed_balance_update(fake_db):
    """The exact review scenario: the balance UPDATE fails AFTER the marker
    INSERT would otherwise have landed. The marker must roll back with it --
    otherwise a retry for this payment_id hits the ON CONFLICT path forever
    and the balance never moves, even though the marker claims it's applied."""
    real_run = fake_db._run

    def _fail_the_balance_update(sql, params=None):
        if "INSERT INTO ledger_entries" in sql.strip():
            raise RuntimeError("simulated balance update failure")
        return real_run(sql, params)

    fake_db._run = _fail_the_balance_update

    with pytest.raises(RuntimeError):
        balance.apply_payment_once(payment_id=9, loan_id=5, amount=30.0)

    # Rolled back: no marker, balance untouched.
    assert 9 not in fake_db.applications
    assert fake_db.balance == 100.0

    # A retry with no failure this time must actually apply -- not silently
    # skip because a stale marker survived the failed attempt.
    fake_db._run = real_run
    new_balance, applied = balance.apply_payment_once(payment_id=9, loan_id=5, amount=30.0)

    assert applied is True
    assert new_balance == 70.0
    assert fake_db.balance == 70.0
