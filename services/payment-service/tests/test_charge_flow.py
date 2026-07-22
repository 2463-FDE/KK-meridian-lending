"""Characterization tests for the payment-service charge flow.

Pins CURRENT behavior before any Week 5+ change touches this service. This is
NOT a fix -- test_pan_mask.py already documents that idempotency (D2) and full
PAN/CVV persistence are deliberately untested "debt". These tests exist so
Week 5's fix has a documented "before" to diff against: they prove today's
actual behavior (including the double-charge bug), they don't hide or excuse it.
"""
import httpx as httpx_module
import pytest
from fastapi.testclient import TestClient

from app import payments
from app.main import app

client = TestClient(app)


class _FakeDb:
    """Stands in for app.db.query -- records every INSERT and hands back an
    incrementing id, like a real `RETURNING id` would for repeated inserts."""

    def __init__(self):
        self.calls = []
        self._next_id = 1

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        row = {"id": self._next_id}
        self._next_id += 1
        return [row]


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr(payments, "db", db)
    return db


class _FakeServicingResponse:
    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def _stub_servicing_call(monkeypatch):
    """_apply_via_servicing calls out to servicing-service over real HTTP by
    default -- stub it so these tests never need a live servicing-service."""
    monkeypatch.setattr(payments.httpx, "post", lambda *a, **k: _FakeServicingResponse())


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "payment-service"}


def test_post_payment_success(fake_db):
    resp = client.post("/payments", json={
        "loan_id": 42, "pan": "4111111111111111", "cvv": "123", "amount": 250.0,
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["payment_id"] == 1
    assert body["loan_id"] == 42
    assert body["status"] == "captured"
    assert body["applied_amount"] == 250.0


def test_post_payment_quantizes_malformed_float_amount_to_cents(fake_db):
    # D12 fix: payment-service does no repeated arithmetic (no accumulation
    # loop like disclosure-service/servicing-service had), but it never
    # validated the incoming amount either -- a malformed float from a client
    # used to get stored and forwarded verbatim, uncorrected.
    resp = client.post("/payments", json={
        "loan_id": 42, "pan": "4111111111111111", "cvv": "123",
        "amount": 19.999999999999996,
    })

    assert resp.status_code == 200
    assert resp.json()["applied_amount"] == 20.0
    _, params = fake_db.calls[0]
    assert params[3] == 20.0  # the amount actually persisted to the payments row


def test_post_payment_log_line_redacts_pan_cvv_ssn(fake_db, caplog):
    # D5 fix: the log line used to write full PAN/CVV/SSN at INFO with zero
    # redaction (services/payment-service/app/payments.py). Storage in the
    # payments table is a separate, still-open half of D5 -- this only proves
    # the logging half is fixed.
    import logging
    caplog.set_level(logging.INFO, logger="payment")

    client.post("/payments", json={
        "loan_id": 42, "pan": "4111111111111111", "cvv": "123",
        "ssn": "412-55-9981", "amount": 10.0,
    })

    charge_lines = [r.message for r in caplog.records if "charge req=" in r.message]
    assert charge_lines, "expected a charge log line"
    logged = charge_lines[0]
    assert "4111111111111111" not in logged
    assert "123" not in logged
    assert "412-55-9981" not in logged
    # redact_dict() redacts by key name (pan/cvv/ssn), not the pattern-based
    # markers redact_str() uses on free text where the key isn't already known.
    assert logged.count("[REDACTED]") == 3


def test_post_payment_persists_full_pan_and_cvv_unmasked(fake_db):
    # Characterizes the documented PCI debt (D5/adr/0003): the stored row gets
    # the full PAN and CVV, not a masked/tokenized value. _mask_pan is
    # display-only and never touches what's actually persisted here.
    client.post("/payments", json={
        "loan_id": 42, "pan": "4111111111111111", "cvv": "999", "amount": 10.0,
    })

    assert len(fake_db.calls) == 1
    _, params = fake_db.calls[0]
    assert "4111111111111111" in params
    assert "999" in params


def test_post_payment_still_reports_captured_when_servicing_unreachable(fake_db, monkeypatch):
    def _boom(*a, **k):
        raise httpx_module.ConnectError("connection refused")

    monkeypatch.setattr(payments.httpx, "post", _boom)

    resp = client.post("/payments", json={
        "loan_id": 42, "pan": "4111111111111111", "cvv": "123", "amount": 100.0,
    })

    # Documented tradeoff (payments.py _apply_via_servicing docstring): the
    # card is already charged and the row already written by this point, so a
    # servicing-side failure still reports "captured" rather than erroring the
    # whole request.
    assert resp.status_code == 200
    assert resp.json()["status"] == "captured"


def test_retried_post_payment_double_charges(fake_db):
    # Characterizes debt D2: no idempotency key means an identical retried
    # POST inserts a SECOND payments row and reports a SECOND applied_amount,
    # rather than being recognized as a duplicate of the first.
    body = {"loan_id": 42, "pan": "4111111111111111", "cvv": "123", "amount": 500.0}

    first = client.post("/payments", json=body)
    second = client.post("/payments", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["payment_id"] != second.json()["payment_id"]
    assert len(fake_db.calls) == 2
    assert first.json()["applied_amount"] == second.json()["applied_amount"] == 500.0
