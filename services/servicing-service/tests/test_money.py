"""Money-correctness tests for balance.py.

The original version of this test never actually called any application code
-- it was a standalone demonstration that raw Python float arithmetic drifts
(`1.0 - 0.1*10 != 0.0`), which is true of the language, not a test of this
service's behavior. It could never pass no matter what this codebase did,
and it could never fail if this codebase's own arithmetic were wrong in some
other way. Replaced with tests that exercise the real apply_payment()/
waive_fee() functions repeatedly, proving the D12 fix (Decimal internally,
see balance.py) is actually what makes repeated float-drift-prone operations
land on an exact value, not just an isolated float fact.
"""
from decimal import Decimal

import pytest

from app import balance


class _FakeDb:
    """Stands in for app.db -- one row per loan_id, mutated in place like a
    real UPDATE would be."""

    def __init__(self, balance=0.0, past_due=0.0):
        self.balance = balance
        self.past_due = past_due

    def query(self, sql, params=None):
        if sql.startswith("SELECT balance"):
            return [{"balance": self.balance}]
        if sql.startswith("SELECT past_due"):
            return [{"past_due": self.past_due}]
        if "SET balance" in sql:
            if "balance -" in sql:
                self.balance = float(Decimal(str(self.balance)) - Decimal(str(params[0])))
            else:
                self.balance = params[0]
            return []
        if "SET past_due" in sql:
            if "past_due, 0) -" in sql:
                self.past_due = float(Decimal(str(self.past_due)) - Decimal(str(params[0])))
            else:
                self.past_due = params[0]
            return []
        raise AssertionError(f"unexpected query: {sql}")


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDb(balance=1.0)
    monkeypatch.setattr(balance, "db", db)
    return db


def test_repeated_ten_cent_payments_land_on_exact_zero(fake_db):
    # The scenario the old test gestured at (1.00 balance, ten 0.10 payments)
    # but run through the REAL apply_payment() path this time. Raw float
    # (`1.0 - 0.1` ten times) does NOT land on exactly 0.0 -- Decimal math
    # inside apply_payment() now does.
    for _ in range(10):
        balance.apply_payment(loan_id=1, amount=0.1)

    assert fake_db.balance == 0.0


def test_apply_payment_is_exact_for_a_single_call(fake_db):
    result = balance.apply_payment(loan_id=1, amount=0.30)
    assert result == 0.7


def test_waive_fee_is_exact_across_repeated_calls(fake_db):
    fake_db.past_due = 1.0
    for _ in range(10):
        balance.waive_fee(loan_id=1, amount=0.1)

    assert fake_db.past_due == 0.0


def test_raw_float_would_have_drifted_here_for_comparison():
    # Not a claim about this codebase -- just keeps the original point (raw
    # Python float arithmetic drifts) visible and documented, separate from
    # whether apply_payment() itself is correct (covered above).
    raw = 1.0
    for _ in range(10):
        raw = raw - 0.1
    assert raw != 0.0  # this is the float drift the D12 fix works around

    exact = Decimal("1.0")
    for _ in range(10):
        exact = exact - Decimal("0.1")
    assert exact == Decimal("0.0")
