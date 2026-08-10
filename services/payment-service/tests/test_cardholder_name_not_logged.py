"""The cardholder name must never reach a log line.

Reviewed finding D5d: `charge()` logged the redacted request object at INFO,
and `name` was in that object while `redact_dict` did not treat it as
sensitive -- so the cardholder's name was written in clear beside a loan id, an
amount and a last4. Together those identify a person and what they paid.

Two independent guards, tested independently, because each fails differently:

  1. `charge()` does not put `name` in the logged object at all. If this
     regresses, the field is present but redacted -- an information leak of
     shape rather than content, and still not what should be logged.
  2. `redact_dict` treats `name` as sensitive. If guard 1 regresses, this one
     still prevents the value appearing.

The test asserts on CAPTURED LOG OUTPUT rather than on the returned dict.
Checking the object `charge()` builds would pass on a version that logs the raw
values through a second, unredacted call -- and logging is exactly where this
defect lived.
"""
import logging

import pytest

from app import payments
from app.redactor import redact_dict

CARDHOLDER = "Rowan Fictional-Cardholder"
# Deliberately low-entropy and obviously fake. A first version used a realistic
# `tok_live_<random>` literal and the repository's gitleaks job flagged it as a
# leaked key -- correctly, since it cannot tell a plausible token from a real
# one. test_redactor.py already carries this warning; I reintroduced it anyway.
# What this test needs is an OPAQUE value (matching no PAN/SSN/CVV pattern),
# not a realistic one.
OPAQUE_TOKEN = "tok_test_placeholder_value"


@pytest.fixture
def captured_logs(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def _charge(monkeypatch, **overrides):
    """Run charge() with the database and processor stubbed out.

    Only the logging behaviour is under test, so persistence is replaced -- but
    deliberately NOT the logging call, which is the thing being asserted.
    """
    monkeypatch.setattr(payments.db, "query", lambda *a, **k: [
        {"id": 1, "loan_id": 42, "amount": 5000}
    ])
    kwargs = dict(
        loan_id=42, amount=50.00, idempotency_key="idem-1",
        processor_token=OPAQUE_TOKEN, last4="1111", brand="visa",
        name=CARDHOLDER, method="card",
    )
    kwargs.update(overrides)
    try:
        return payments.charge(**kwargs)
    except Exception:
        # Downstream persistence/authorization is stubbed and may not complete.
        # The log line under test is emitted before any of that.
        return None


def test_the_cardholder_name_never_appears_in_any_log_record(monkeypatch, captured_logs):
    """The finding itself, stated as an assertion over everything logged."""
    _charge(monkeypatch)

    everything = "\n".join(r.getMessage() for r in captured_logs.records)
    assert CARDHOLDER not in everything, (
        "the cardholder name was written to a log line"
    )
    # Also the surname alone: a partial leak is a leak, and an implementation
    # that logged only part of the name would pass a whole-string check.
    assert "Fictional-Cardholder" not in everything
    assert "Rowan" not in everything


def test_the_charge_log_still_carries_what_it_is_for(monkeypatch, captured_logs):
    """Removing the name must not turn the line into something useless.

    A charge is correlated by loan id, last4 and idempotency key. If this fails,
    the fix went too far and took the diagnostic value with it.
    """
    _charge(monkeypatch)

    everything = "\n".join(r.getMessage() for r in captured_logs.records)
    assert "42" in everything, "loan_id is how a charge is found"
    assert "1111" in everything, "last4 is the non-sensitive card identifier"
    assert "idem-1" in everything, "idempotency key ties retries together"


def test_the_processor_token_is_still_redacted(monkeypatch, captured_logs):
    """Unchanged property, re-asserted beside the new one. A vaulted token is
    sensitive even though it is opaque, and it shares the code path just
    changed."""
    _charge(monkeypatch)

    everything = "\n".join(r.getMessage() for r in captured_logs.records)
    assert OPAQUE_TOKEN not in everything
    assert "[REDACTED]" in everything


def test_redact_dict_treats_a_name_as_sensitive():
    """Guard 2, independent of the call site.

    If a future call site reintroduces the field, the value must still not
    survive redaction.
    """
    out = redact_dict({"name": CARDHOLDER, "loan_id": 42})
    assert out["name"] == "[REDACTED]"
    assert out["loan_id"] == 42


def test_redact_dict_covers_the_cardholder_name_spelling_too():
    """Both plausible spellings of the same value."""
    out = redact_dict({"cardholder_name": CARDHOLDER})
    assert out["cardholder_name"] == "[REDACTED]"


def test_a_nested_name_is_redacted():
    """Payment payloads nest. A guard that only inspected top-level keys would
    pass the tests above and leak in production."""
    out = redact_dict({"card": {"name": CARDHOLDER, "last4": "1111"}})
    assert out["card"]["name"] == "[REDACTED]"
    assert out["card"]["last4"] == "1111"
