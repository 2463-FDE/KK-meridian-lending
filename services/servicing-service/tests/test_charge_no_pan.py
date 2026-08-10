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


# --- the numeric fields are channels too -------------------------------------
#
# Second round of the same finding on PR #16. Constraining `method` was not
# enough: charge() logs `loan_id` BEFORE the insert that would reject a
# nonexistent loan, so an unbounded integer carried a PAN into the log just as
# well as a string did -- and the charge failing afterwards does not unwrite a
# log line.

@pytest.mark.parametrize("loan_id", [
    4111111111111111,          # a Luhn-valid PAN as an integer
    9_999_999_999_999,         # any 13-digit-plus run, the PAN length floor
])
def test_loan_id_cannot_carry_a_card_number(loan_id):
    """PAN-length only, and deliberately not more than that.

    A nine-digit integer is an SSN and is also a perfectly plausible loan id;
    no bound can separate them, so this asserts what the int4 range actually
    buys -- a card number will not fit -- rather than a protection the schema
    cannot provide. The SSN limit is named in logging_config.py instead.
    """
    with pytest.raises(Exception) as exc:
        PaymentIn(loan_id=loan_id, processor_token="tok_test_placeholder",
                  last4="1111", brand="visa", amount=10.0)
    assert "loan_id" in str(exc.value)


@pytest.mark.parametrize("amount", [4111111111111111.0, 412559981.0])
def test_amount_cannot_carry_card_or_identity_data(amount):
    with pytest.raises(Exception) as exc:
        PaymentIn(loan_id=1, processor_token="tok_test_placeholder",
                  last4="1111", brand="visa", amount=amount)
    assert "amount" in str(exc.value)


def test_a_real_loan_id_and_amount_are_still_accepted():
    """The bounds are the column's own range, so nothing legitimate is refused."""
    ok = PaymentIn(loan_id=2_147_483_647, processor_token="tok_test_placeholder",
                   last4="1111", brand="visa", amount=1250.75)
    assert ok.loan_id == 2_147_483_647 and ok.amount == 1250.75


def test_every_logged_field_is_shape_bounded():
    """The claim in logging_config.py, stated as an assertion.

    charge() logs exactly loan_id, amount and method. If a field is ever added
    to that line, this test should fail until its shape is bounded too --
    which is the property the docstring asserts, rather than "there is no pan
    field".
    """
    import inspect
    source = inspect.getsource(payments.charge)
    logged = {"loan_id", "amount", "method"}
    line = [ln for ln in source.splitlines() if "log.info" in ln or "loan_id=%s" in ln]
    assert line, "charge() no longer has the log line this test describes"

    import typing
    fields = PaymentIn.model_fields
    for name in logged:
        field = fields[name]
        # Either numeric bounds (metadata) or a closed set of values
        # (Literal). Both make the field incapable of carrying a card number;
        # a bare `int`/`str` does not.
        bounded = bool(field.metadata) or typing.get_origin(field.annotation) is typing.Literal
        assert bounded, (
            f"{name} is written to the log line but has no shape constraint -- "
            f"an unbounded field is a channel whatever it is called"
        )
