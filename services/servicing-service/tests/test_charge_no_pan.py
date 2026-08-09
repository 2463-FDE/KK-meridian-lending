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


# --- card data through a PERMITTED field -------------------------------------
#
# Reviewed on PR #16. Rejecting `pan`/`cvv`/`ssn` by NAME is only half a
# contract: `extra="forbid"` says nothing about what a permitted field
# contains. `method` was a free string that charge() writes straight into the
# log line, so `method="4111111111111111"` put a raw PAN in
# payment-service.log -- while this service's logging_config docstring claimed
# no card data could reach it.

@pytest.mark.parametrize("value", [
    "4111111111111111",          # a Luhn-valid PAN
    "4111 1111 1111 1111",       # spaced, as a human would paste it
    "412-55-9981",               # an SSN
])
def test_method_cannot_carry_card_or_identity_data(value):
    """The field is a payment method, so it takes payment methods."""
    with pytest.raises(Exception) as exc:
        PaymentIn(loan_id=1, processor_token="tok_test_placeholder", last4="1111",
                  brand="visa", amount=10.0, method=value)
    assert "method" in str(exc.value)


@pytest.mark.parametrize("field,value", [
    ("last4", "4111111111111111"),
    ("brand", "4111111111111111"),
])
def test_the_display_fields_cannot_carry_a_pan_either(field, value):
    """`last4` and `brand` are persisted, so they get the same treatment.

    Neither is logged today, but both are written to the payments row, and a
    field whose shape is unconstrained is a channel whatever it is used for.
    """
    payload = {"loan_id": 1, "processor_token": "tok_test_placeholder",
               "last4": "1111", "brand": "visa", "amount": 10.0}
    payload[field] = value
    with pytest.raises(Exception) as exc:
        PaymentIn(**payload)
    assert field in str(exc.value)


@pytest.mark.parametrize("method", ["card", "ach"])
def test_the_real_payment_methods_still_work(method):
    """The constraint must not break the endpoint it protects."""
    assert PaymentIn(loan_id=1, processor_token="tok_test_placeholder", last4="1111",
                     brand="visa", amount=10.0, method=method).method == method


def test_no_permitted_field_can_put_a_pan_in_the_log_line(monkeypatch, caplog):
    """End of the argument, exercised rather than reasoned about.

    Drives charge() with the most sensitive values the schema will now accept
    and asserts the emitted log line carries none of them.
    """
    fake_db, fake_balance = _FakeDb(), _FakeBalance()
    monkeypatch.setattr(payments, "db", fake_db)
    monkeypatch.setattr(payments, "balance", fake_balance)

    accepted = PaymentIn(loan_id=7, processor_token="tok_test_placeholder",
                         last4="1111", brand="visa", amount=10.0, method="card")
    with caplog.at_level(logging.INFO):
        payments.charge(accepted.loan_id, accepted.processor_token, accepted.last4,
                        accepted.brand, accepted.amount, method=accepted.method)

    emitted = " ".join(r.getMessage() for r in caplog.records)
    assert "4111" not in emitted.replace("last4=1111", "")
    assert "tok_test_placeholder" not in emitted
    assert "method=card" in emitted
