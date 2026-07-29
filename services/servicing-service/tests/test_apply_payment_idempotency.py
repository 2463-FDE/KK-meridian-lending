"""Tests for balance.apply_payment_once (db/migrations/0013).

Review finding: apply-payment applied the balance unconditionally on every
call, with no idempotency of its own -- it trusted payment-service to never
call it twice for the same payment. Once payment-service started retrying a
pending apply on a same-key retry, a duplicate call here (the retry itself,
or two requests racing) had to be guaranteed not to double-apply the same
payment_id. These tests exercise apply_payment_once() directly.
"""
import pytest

from app import balance


class _FakeDb:
    """Stands in for app.db -- one balances row, plus a payment_applications
    table keyed on payment_id (PRIMARY KEY -> INSERT ... ON CONFLICT DO
    NOTHING only lands a row once per payment_id, mirroring the real unique
    constraint from db/migrations/0013)."""

    def __init__(self, balance=0.0):
        self.balance = balance
        self.applications = set()

    def query(self, sql, params=None):
        stmt = sql.strip()
        if stmt.startswith("INSERT INTO payment_applications"):
            payment_id, loan_id, amount = params
            if payment_id in self.applications:
                return []  # ON CONFLICT DO NOTHING -- already applied
            self.applications.add(payment_id)
            return [{"payment_id": payment_id}]
        if stmt.startswith("SELECT balance"):
            return [{"balance": self.balance}]
        if "SET balance" in stmt:
            self.balance = params[0]
            return []
        raise AssertionError(f"unexpected query: {sql}")


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
