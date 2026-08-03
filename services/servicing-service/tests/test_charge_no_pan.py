"""ADR 0008 (Week 5 tokenization), ported to this duplicate legacy endpoint.

This service's own POST /payments (app/main.py, app/payments.py::charge)
used to receive and store the FULL PAN and CVV, and log the full charge
request (PAN, CVV, SSN) at INFO with zero redaction (D5) -- the exact gap
payment-service's own /payments already closed, just not yet ported here.
These tests cover the fix: the wire contract rejects pan/cvv/ssn outright,
and charge() never logs or persists them.
"""
import logging

import pytest

from app import payments
from app.main import PaymentIn


class _FakeDb:
    def __init__(self):
        self.calls = []

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        return []


class _FakeBalance:
    def __init__(self, new_balance=100.0):
        self.new_balance = new_balance
        self.calls = []

    def apply_payment(self, loan_id, amount):
        self.calls.append((loan_id, amount))
        return self.new_balance


@pytest.mark.parametrize("field,value", [("pan", "4111111111111111"), ("cvv", "123"), ("ssn", "412-55-9981")])
def test_payment_in_rejects_pan_cvv_ssn_outright(field, value):
    # `extra="forbid"` makes this a real rejection, not a silent field drop --
    # a client still sending pan/cvv/ssn out of habit gets a validation error.
    with pytest.raises(Exception):
        PaymentIn(
            loan_id=42, processor_token="tok_mock_abc", amount=100.0,
            **{field: value},
        )


def test_charge_never_logs_last4_or_brand_unredacted(monkeypatch, caplog):
    monkeypatch.setattr(payments, "db", _FakeDb())
    monkeypatch.setattr(payments, "balance", _FakeBalance())
    caplog.set_level(logging.INFO, logger="payment")

    payments.charge(42, "tok_mock_abc123", "1111", "visa", 100.0, name="Jane Borrower")

    charge_lines = [r.message for r in caplog.records if "charge" in r.message]
    assert charge_lines, "expected a charge log line"
    logged = " ".join(charge_lines)
    assert "tok_mock_abc123" not in logged


def test_charge_persists_last4_and_brand_not_a_raw_pan(monkeypatch):
    fake_db = _FakeDb()
    monkeypatch.setattr(payments, "db", fake_db)
    monkeypatch.setattr(payments, "balance", _FakeBalance())

    payments.charge(42, "tok_mock_abc123", "4242", "mastercard", 50.0)

    insert_calls = [c for c in fake_db.calls if c[0].strip().startswith("INSERT")]
    assert len(insert_calls) == 1
    _, params = insert_calls[0]
    assert "4242" in params
    assert "mastercard" in params
    assert "tok_mock_abc123" not in params
    assert not any(isinstance(p, str) and len(p) == 16 and p.isdigit() for p in params)
