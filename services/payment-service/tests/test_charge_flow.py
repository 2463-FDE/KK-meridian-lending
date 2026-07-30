"""Tests for the payment-service charge flow.

Review finding: a timeout retry or a double-click on submit inserted a second
payments row and applied the balance twice via servicing-service -- there was
no idempotency key at all. These tests cover the fix: `idempotency_key` is now
required at the API boundary, and a repeated request with the SAME key
returns the ORIGINAL payment result without a second insert -- even the
ON CONFLICT DO NOTHING's atomicity (a duplicate is detected even if it races
the original).

Review finding (D2 follow-up): a charge could still silently never reach the
loan balance -- a servicing-side failure was swallowed and charge() reported
"captured" regardless, with no record anything was left undone. These tests
also cover that fix: `applied_at` tracks confirmed-applied separately from
captured, an apply failure reports "pending" (not "captured"), and a same-key
retry retries the apply instead of repeating a false "captured".

test_pan_mask.py still documents the remaining, deliberately-untested debt:
full PAN/CVV persistence (D5/PCI).
"""
import httpx as httpx_module
import pytest
from fastapi.testclient import TestClient

from app import payments
from app.main import app

client = TestClient(app)


class _FakeDb:
    """Stands in for app.db.query -- simulates a payments table with a partial
    unique index on idempotency_key: the INSERT ... ON CONFLICT DO NOTHING
    only succeeds once per (non-null) key, and the fallback SELECT reads back
    whatever the first successful insert stored. Also tracks applied_at, set
    by the UPDATE charge() issues once servicing confirms the apply."""

    def __init__(self):
        self.calls = []
        self._next_id = 1
        self._by_key = {}
        self._by_id = {}

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        stmt = sql.strip()
        if stmt.startswith("INSERT"):
            loan_id, pan, cvv, amount, method, idempotency_key = params
            if idempotency_key is not None and idempotency_key in self._by_key:
                return []  # ON CONFLICT DO NOTHING -- a row already exists for this key
            row = {"id": self._next_id, "loan_id": loan_id, "amount": amount, "applied_at": None}
            self._next_id += 1
            if idempotency_key is not None:
                self._by_key[idempotency_key] = row
            self._by_id[row["id"]] = row
            return [row]
        if stmt.startswith("SELECT"):
            (idempotency_key,) = params
            return [self._by_key[idempotency_key]]
        if stmt.startswith("UPDATE"):
            (payment_id,) = params
            self._by_id[payment_id]["applied_at"] = "2026-07-29T00:00:00Z"
            return []
        raise AssertionError(f"unexpected query: {sql}")


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


def _payload(**overrides):
    body = {
        "loan_id": 42, "pan": "4111111111111111", "cvv": "123", "amount": 250.0,
        "idempotency_key": "idem-key-1",
    }
    body.update(overrides)
    return body


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "payment-service"}


def test_post_payment_success(fake_db):
    resp = client.post("/payments", json=_payload())

    assert resp.status_code == 200
    body = resp.json()
    assert body["payment_id"] == 1
    assert body["loan_id"] == 42
    assert body["status"] == "captured"
    assert body["applied_amount"] == 250.0


def test_post_payment_requires_idempotency_key():
    body = _payload()
    del body["idempotency_key"]

    resp = client.post("/payments", json=body)

    assert resp.status_code == 422


def test_post_payment_quantizes_malformed_float_amount_to_cents(fake_db):
    # D12 fix: payment-service does no repeated arithmetic (no accumulation
    # loop like disclosure-service/servicing-service had), but it never
    # validated the incoming amount either -- a malformed float from a client
    # used to get stored and forwarded verbatim, uncorrected.
    resp = client.post("/payments", json=_payload(amount=19.999999999999996))

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

    client.post("/payments", json=_payload(ssn="412-55-9981", amount=10.0))

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
    client.post("/payments", json=_payload(cvv="999", amount=10.0))

    insert_calls = [c for c in fake_db.calls if c[0].strip().startswith("INSERT")]
    assert len(insert_calls) == 1
    _, params = insert_calls[0]
    assert "4111111111111111" in params
    assert "999" in params


def test_post_payment_reports_pending_when_servicing_unreachable(fake_db, monkeypatch):
    def _boom(*a, **k):
        raise httpx_module.ConnectError("connection refused")

    monkeypatch.setattr(payments.httpx, "post", _boom)

    resp = client.post("/payments", json=_payload(amount=100.0))

    # Review fix: the card is already charged and the row already written by
    # this point, so the request still succeeds -- but the status must say
    # "pending", not "captured", since the balance was never confirmed
    # applied. applied_at stays NULL (no UPDATE call went out).
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    update_calls = [c for c in fake_db.calls if c[0].strip().startswith("UPDATE")]
    assert update_calls == []


def test_repeated_post_payment_with_same_idempotency_key_is_not_double_charged(fake_db, monkeypatch):
    """The review's exact scenario: a timeout retry or a double-click resends
    the identical request, same idempotency_key. Must return the ORIGINAL
    payment_id/applied_amount, insert no second row, and call servicing-
    service (apply the balance) exactly once."""
    servicing_calls = []
    monkeypatch.setattr(
        payments.httpx, "post",
        lambda *a, **k: servicing_calls.append((a, k)) or _FakeServicingResponse(),
    )

    body = _payload(amount=500.0, idempotency_key="retry-key")

    first = client.post("/payments", json=body)
    second = client.post("/payments", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["payment_id"] == second.json()["payment_id"]
    assert first.json()["applied_amount"] == second.json()["applied_amount"] == 500.0
    assert first.json()["status"] == second.json()["status"] == "captured"

    insert_calls = [c for c in fake_db.calls if c[0].strip().startswith("INSERT")]
    assert len(insert_calls) == 2  # both requests attempt the insert...
    assert len(servicing_calls) == 1  # ...but only the first ever reaches servicing
    # applied_at was already set by the first call, so the retry never re-called
    # servicing -- it just read back the already-applied row.


def test_repeated_post_payment_reconciles_a_pending_apply(fake_db, monkeypatch):
    """Review fix (D2 follow-up): insert succeeds, servicing fails -> pending.
    A same-key retry must retry the apply, not just repeat "captured" -- and
    if servicing succeeds this time, the payment reconciles to "captured"."""
    servicing_calls = []

    def _boom(*a, **k):
        raise httpx_module.ConnectError("connection refused")

    monkeypatch.setattr(payments.httpx, "post", _boom)
    body = _payload(amount=300.0, idempotency_key="pending-then-retry")

    first = client.post("/payments", json=body)
    assert first.json()["status"] == "pending"

    def _ok(*a, **k):
        servicing_calls.append((a, k))
        return _FakeServicingResponse()

    monkeypatch.setattr(payments.httpx, "post", _ok)
    second = client.post("/payments", json=body)

    assert second.status_code == 200
    assert second.json()["payment_id"] == first.json()["payment_id"]
    assert second.json()["status"] == "captured"
    assert len(servicing_calls) == 1  # the retry is what actually reaches servicing


def test_repeated_post_payment_still_pending_if_servicing_fails_again(fake_db, monkeypatch):
    """Same scenario, but servicing fails again on retry -- must keep
    reporting "pending", never fall back to a false "captured"."""
    def _boom(*a, **k):
        raise httpx_module.ConnectError("connection refused")

    monkeypatch.setattr(payments.httpx, "post", _boom)
    body = _payload(amount=300.0, idempotency_key="still-pending")

    first = client.post("/payments", json=body)
    second = client.post("/payments", json=body)

    assert first.json()["status"] == second.json()["status"] == "pending"


def test_different_idempotency_keys_charge_separately(fake_db):
    # Two genuinely different payments (different keys) must both go through --
    # the fix must not accidentally collapse unrelated charges together.
    first = client.post("/payments", json=_payload(idempotency_key="key-a", amount=100.0))
    second = client.post("/payments", json=_payload(idempotency_key="key-b", amount=200.0))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["payment_id"] != second.json()["payment_id"]
    assert first.json()["applied_amount"] == 100.0
    assert second.json()["applied_amount"] == 200.0


@pytest.mark.parametrize("amount", [0, -500.0])
def test_post_payment_rejects_non_positive_amount(fake_db, amount):
    # Review fix: amount was an unconstrained float -- a negative value
    # credited the borrower's balance instead of charging them (servicing
    # computes new_balance = current - amount).
    resp = client.post("/payments", json=_payload(amount=amount))

    assert resp.status_code == 422
    assert fake_db.calls == []


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_post_payment_rejects_non_finite_amount(fake_db, literal):
    # httpx's own json= encoder refuses to put NaN/Infinity on the wire at all
    # (raises ValueError) -- build the request body by hand to prove the
    # server-side still rejects a client that sends one anyway.
    body = (
        '{"loan_id": 42, "pan": "4111111111111111", "cvv": "123", '
        '"idempotency_key": "nonfinite-key", "amount": %s}' % literal
    )

    resp = client.post(
        "/payments", content=body, headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 422
    assert fake_db.calls == []


def test_post_payment_rejects_amount_over_the_ceiling(fake_db):
    resp = client.post("/payments", json=_payload(amount=1_000_000.01))

    assert resp.status_code == 422
    assert fake_db.calls == []
