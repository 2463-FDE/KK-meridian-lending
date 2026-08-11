"""Tests for the payment-service charge flow.

Review finding: a timeout retry or a double-click on submit inserted a second
payments row and applied the balance twice via servicing-service -- there was
no idempotency key at all. These tests cover the fix: `idempotency_key` is now
required at the API boundary, and a repeated request with the SAME key
returns the ORIGINAL payment result without a second insert -- even the
ON CONFLICT DO NOTHING's atomicity (a duplicate is detected even if it races
the original).

Review finding (follow-up): a charge could still silently never reach the
loan balance -- a servicing-side failure was swallowed and charge() reported
"captured" regardless, with no record anything was left undone. These tests
also cover that fix: `applied_at` tracks confirmed-applied separately from
captured, an apply failure reports "pending" (not "captured"), and a same-key
retry retries the apply instead of repeating a false "captured".

ADR 0008 (Week 5 tokenization): this endpoint no longer accepts pan/cvv/ssn
at all -- see PaymentIn (schemas.py). test_post_payment_never_persists_
processor_token and test_post_payment_stores_last4_and_brand cover the new
contract: only an opaque processor_token (used transiently, never stored)
plus last4/brand for display.

Review finding: charge() used to treat receiving a processor_token as proof
the card was charged -- the token was only shape/length-checked, never sent
to a processor for real authorization. test_post_payment_with_a_made_up_
token_never_captures_or_touches_the_balance is the exact attack: an arbitrary
token gets declined, no balance-affecting call ever reaches servicing.
"""
import json
import logging
from decimal import Decimal

import httpx as httpx_module
import pytest
from fastapi.testclient import TestClient

from app import config, payments, processor
from app.main import app

client = TestClient(app)
# Defaulted for every request in this file so the pre-existing tests below
# don't each need updating -- the X-Internal-Token rejection tests further
# down override/clear it per-call instead.
client.headers.update({"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})


@pytest.fixture(autouse=True)
def _reset_stub_processor_authorizations():
    """processor._stub_authorizations mimics a real processor's own
    idempotency-key store, which is process-lifetime state by design (see
    processor.py). Several tests in this file reuse the same default
    idempotency_key ("idem-key-1") for genuinely unrelated attempts, so it
    must not leak an authorization minted by an earlier test into a later
    one expecting a fresh decline/approval."""
    processor._stub_authorizations.clear()
    yield
    processor._stub_authorizations.clear()


class _FakeDb:
    """Stands in for app.db.query -- simulates a payments table with a partial
    unique index on idempotency_key: the INSERT ... ON CONFLICT DO NOTHING
    only succeeds once per (non-null) key, and the fallback SELECT reads back
    whatever the first successful insert stored. Also tracks applied_at (set
    once servicing confirms the balance apply) and auth_status (set once the
    processor confirms or declines the authorization) -- each via its own
    UPDATE, distinguished by which column the real SQL text is setting."""

    def __init__(self):
        self.calls = []
        self._next_id = 1
        self._by_key = {}
        self._by_id = {}

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        stmt = sql.strip()
        if stmt.startswith("INSERT"):
            loan_id, last4, brand, amount, method, idempotency_key = params
            if idempotency_key is not None and idempotency_key in self._by_key:
                return []  # ON CONFLICT DO NOTHING -- a row already exists for this key
            # Real Postgres hands a NUMERIC column back as Decimal regardless
            # of what type was inserted -- mirror that here so a same-key
            # retry's `row["amount"] != amount` comparison (Decimal vs. the
            # request's float) is exercised the same way it is in production.
            row = {
                "id": self._next_id, "loan_id": loan_id, "amount": Decimal(str(amount)),
                "last4": last4, "brand": brand, "applied_at": None,
                "auth_status": "pending", "authorization_id": None,
            }
            self._next_id += 1
            if idempotency_key is not None:
                self._by_key[idempotency_key] = row
            self._by_id[row["id"]] = row
            return [row]
        if stmt.startswith("SELECT"):
            (idempotency_key,) = params
            return [self._by_key[idempotency_key]]
        if stmt.startswith("UPDATE"):
            if "auth_status = 'captured'" in stmt:
                # Review fix: auth_status and authorization_id are now written
                # in the same UPDATE -- params carries both.
                auth_id, payment_id = params
                self._by_id[payment_id]["auth_status"] = "captured"
                self._by_id[payment_id]["authorization_id"] = auth_id
            elif "auth_status = 'failed'" in stmt:
                (payment_id,) = params
                self._by_id[payment_id]["auth_status"] = "failed"
            elif "applied_at" in stmt:
                (payment_id,) = params
                self._by_id[payment_id]["applied_at"] = "2026-07-29T00:00:00Z"
            elif "apply_last_error" in stmt:
                # PR #8: a failed apply records the exception TYPE for triage
                # (never the message) and leaves applied_at NULL, which is what
                # enqueues the row for app/reconcile.py.
                error_code, payment_id = params
                self._by_id[payment_id]["apply_last_error"] = error_code
            else:
                raise AssertionError(f"unexpected UPDATE: {sql}")
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


# Shaped exactly like frontend/lib/tokenize.ts's own mock output
# (`tok_mock_<uuid>`) -- app.processor._stub_authorize() only approves a
# token matching this shape, same as a real processor only ever approves a
# token it actually issued.
_VALID_MOCK_TOKEN = "tok_mock_550e8400-e29b-41d4-a716-446655440000"


def _payload(**overrides):
    body = {
        "loan_id": 42, "processor_token": _VALID_MOCK_TOKEN, "last4": "1111",
        "brand": "visa", "amount": 250.0, "idempotency_key": "idem-key-1",
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


def test_post_payment_log_line_redacts_processor_token(fake_db, caplog):
    # ADR 0008: pan/cvv/ssn no longer exist on this endpoint at all (D5's
    # storage half is closed by not receiving them in the first place). The
    # processor_token is opaque but still sensitive -- a vaulted token found
    # in a log is itself a real credential leak -- so it's still redacted.
    import logging
    caplog.set_level(logging.INFO, logger="payment")

    client.post("/payments", json=_payload(processor_token="tok_mock_secret999", amount=10.0))

    charge_lines = [r.message for r in caplog.records if "charge req=" in r.message]
    assert charge_lines, "expected a charge log line"
    logged = charge_lines[0]
    assert "tok_mock_secret999" not in logged
    assert "[REDACTED]" in logged


def test_post_payment_never_persists_processor_token(fake_db):
    # ADR 0008 (Week 5 tokenization): the token is used only to (mock-)charge
    # the processor -- it must never reach the payments row. Only last4/brand
    # persist, for display.
    client.post("/payments", json=_payload(processor_token="tok_mock_should_not_be_stored", amount=10.0))

    insert_calls = [c for c in fake_db.calls if c[0].strip().startswith("INSERT")]
    assert len(insert_calls) == 1
    _, params = insert_calls[0]
    assert "tok_mock_should_not_be_stored" not in params


def test_post_payment_stores_last4_and_brand(fake_db):
    resp = client.post("/payments", json=_payload(last4="4242", brand="mastercard", amount=10.0))

    assert resp.status_code == 200
    insert_calls = [c for c in fake_db.calls if c[0].strip().startswith("INSERT")]
    _, params = insert_calls[0]
    assert "4242" in params
    assert "mastercard" in params


@pytest.mark.parametrize("field,value", [("pan", "4111111111111111"), ("cvv", "123"), ("ssn", "412-55-9981")])
def test_post_payment_rejects_pan_cvv_ssn_outright(fake_db, field, value):
    # ADR 0008: this endpoint used to accept these directly. `extra="forbid"`
    # on PaymentIn makes the new contract a real rejection (422), not a silent
    # drop of a field the client may still be sending out of habit.
    resp = client.post("/payments", json=_payload(**{field: value}))

    assert resp.status_code == 422
    assert fake_db.calls == []


def test_post_payment_rejects_missing_processor_token(fake_db):
    body = _payload()
    del body["processor_token"]

    resp = client.post("/payments", json=body)

    assert resp.status_code == 422


@pytest.mark.parametrize("last4", ["123", "12345", "abcd", ""])
def test_post_payment_rejects_malformed_last4(fake_db, last4):
    resp = client.post("/payments", json=_payload(last4=last4))

    assert resp.status_code == 422


def test_post_payment_reports_pending_when_servicing_unreachable(fake_db, monkeypatch):
    def _boom(*a, **k):
        raise httpx_module.ConnectError("connection refused")

    monkeypatch.setattr(payments.httpx, "post", _boom)

    resp = client.post("/payments", json=_payload(amount=100.0))

    # Review fix: the card is already authorized (auth_status -> 'captured')
    # and the row already written by this point, so the request still
    # succeeds -- but the response status must say "pending", not "captured",
    # since the balance was never confirmed applied. applied_at stays NULL
    # (no applied_at-setting UPDATE went out), even though the authorization
    # UPDATE did.
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    applied_at_updates = [
        c for c in fake_db.calls if c[0].strip().startswith("UPDATE") and "applied_at" in c[0]
    ]
    assert applied_at_updates == []


def test_post_payment_rejects_missing_internal_token(fake_db):
    """Defense in depth for POST /payments -- see docker-compose.yml (no host
    port for this service) and app/config.py."""
    resp = client.post(
        "/payments", json=_payload(), headers={"X-Internal-Token": ""},
    )

    assert resp.status_code == 401


def test_post_payment_rejects_wrong_internal_token(fake_db):
    resp = client.post(
        "/payments", json=_payload(),
        headers={"X-Internal-Token": "attacker-guessed-token"},
    )

    assert resp.status_code == 401


def test_post_payment_rejects_everything_when_config_token_unset(fake_db, monkeypatch):
    """A deploy that forgets to set INTERNAL_SERVICE_TOKEN must fail closed --
    no caller (not even one that sends the empty string) should ever match."""
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "")

    resp = client.post(
        "/payments", json=_payload(), headers={"X-Internal-Token": ""},
    )

    assert resp.status_code == 401


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


def test_repeated_post_payment_with_a_cents_amount_is_not_misjudged_as_conflict(fake_db, monkeypatch):
    """Review fix: row["amount"] reads back from Postgres as Decimal while the
    incoming amount is a float -- Decimal('10.99') != 10.99 under naive
    comparison, so an identical retry with a cents-precision amount used to
    409 instead of returning the original result, leaving the payment stuck
    with applied_at NULL. Must behave exactly like any other same-key retry."""
    servicing_calls = []
    monkeypatch.setattr(
        payments.httpx, "post",
        lambda *a, **k: servicing_calls.append((a, k)) or _FakeServicingResponse(),
    )

    body = _payload(amount=10.99, idempotency_key="cents-retry-key")

    first = client.post("/payments", json=body)
    second = client.post("/payments", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["payment_id"] == second.json()["payment_id"]
    assert first.json()["applied_amount"] == second.json()["applied_amount"] == 10.99
    assert first.json()["status"] == second.json()["status"] == "captured"
    assert len(servicing_calls) == 1  # the retry never re-applied anything


def test_repeated_post_payment_reconciles_a_pending_apply(fake_db, monkeypatch):
    """Review fix (follow-up): insert succeeds, servicing fails -> pending. A
    same-key retry must retry the apply, not just repeat "captured" -- and
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


def test_reusing_a_key_with_a_different_loan_id_is_a_409_not_a_misapply(fake_db, monkeypatch):
    # Review fix: a retry reusing an idempotency_key but claiming a DIFFERENT
    # loan_id must never be honored against either the request's loan_id (the
    # original bug) or silently against the stored one -- surfaced as a 409
    # so the caller knows this key collision is not a safe retry, and never
    # reaches servicing-service for the mismatched request at all.
    servicing_calls = []
    monkeypatch.setattr(
        payments.httpx, "post",
        lambda *a, **k: servicing_calls.append((a, k)) or _FakeServicingResponse(),
    )

    first = client.post("/payments", json=_payload(idempotency_key="reused-key", loan_id=42))
    second = client.post("/payments", json=_payload(idempotency_key="reused-key", loan_id=999))

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(servicing_calls) == 1  # the mismatched retry never re-applied anything


def test_reusing_a_key_with_a_different_amount_is_a_409(fake_db):
    first = client.post("/payments", json=_payload(idempotency_key="reused-key-2", amount=100.0))
    second = client.post("/payments", json=_payload(idempotency_key="reused-key-2", amount=999.0))

    assert first.status_code == 200
    assert second.status_code == 409


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
        '{"loan_id": 42, "processor_token": "tok_mock_abc123", "last4": "1111", '
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


# --- review finding: charge() used to trust processor_token with no real ---
# --- authorization call at all -----------------------------------------

def test_post_payment_with_a_made_up_token_never_captures_or_touches_the_balance(fake_db, monkeypatch):
    """The exact attack the review flagged: a borrower POSTs an arbitrary,
    never-issued processor_token. Must be declined -- no captured payment,
    and servicing (the loan balance) is never called at all."""
    servicing_calls = []
    monkeypatch.setattr(
        payments.httpx, "post",
        lambda *a, **k: servicing_calls.append((a, k)) or _FakeServicingResponse(),
    )

    resp = client.post("/payments", json=_payload(processor_token="i-just-made-this-up", amount=250.0))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert not servicing_calls  # the loan balance was never touched

    # The attempt is on record (an honest audit trail), but explicitly as
    # declined -- never as captured.
    payment_id = body["payment_id"]
    assert fake_db._by_id[payment_id]["auth_status"] == "failed"


def test_reused_key_stays_declined_after_a_failed_authorization(fake_db, monkeypatch):
    """A retry of the SAME declined attempt (same idempotency_key) must not
    somehow succeed on a second try -- a declined charge stays declined; a
    genuine retry needs a new idempotency_key (a new attempt), not a replay."""
    servicing_calls = []
    monkeypatch.setattr(
        payments.httpx, "post",
        lambda *a, **k: servicing_calls.append((a, k)) or _FakeServicingResponse(),
    )
    body = _payload(processor_token="still-made-up", idempotency_key="declined-key")

    first = client.post("/payments", json=body)
    second = client.post("/payments", json=body)

    assert first.json()["status"] == second.json()["status"] == "failed"
    assert first.json()["payment_id"] == second.json()["payment_id"]
    assert not servicing_calls


def test_post_payment_declines_the_fixed_test_amount(fake_db):
    """Mirrors a real processor's own published test-card convention: a
    fixed amount always declines, so the decline path is exercisable without
    a live processor -- proves the amount, not just the token, is checked."""
    resp = client.post("/payments", json=_payload(amount=0.02))

    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"


# --- review finding: authorization-retry could double-charge the processor ---

def test_retry_after_crash_before_auth_status_persists_reuses_existing_authorization(fake_db, monkeypatch):
    """The review's exact scenario: the processor approves the charge, but
    the process dies before auth_status/authorization_id are persisted
    (still 'pending' on record). A same-key retry must NOT call
    authorize_charge() again -- it must ask the processor for its own record
    of the idempotency_key (get_authorization()) and reuse that."""
    authorize_calls = []

    def _fake_authorize(token, amount, idempotency_key=None):
        authorize_calls.append((token, amount, idempotency_key))
        return "auth_abc123"

    def _fake_get_authorization(idempotency_key):
        # Mirrors a real processor's own idempotency-key store: it remembers
        # the authorization from the first (crashed) attempt.
        return "auth_abc123" if authorize_calls else None

    monkeypatch.setattr(payments.processor, "authorize_charge", _fake_authorize)
    monkeypatch.setattr(payments.processor, "get_authorization", _fake_get_authorization)

    real_query = fake_db.query

    def _crash_before_auth_status_update(sql, params=None):
        if sql.strip().startswith("UPDATE") and "auth_status = 'captured'" in sql:
            raise RuntimeError("simulated crash before auth_status/authorization_id persisted")
        return real_query(sql, params)

    monkeypatch.setattr(fake_db, "query", _crash_before_auth_status_update)

    with pytest.raises(RuntimeError):
        payments.charge(
            loan_id=42, processor_token=_VALID_MOCK_TOKEN, last4="1111", brand="visa",
            amount=250.0, idempotency_key="crash-before-persist",
        )

    assert fake_db._by_key["crash-before-persist"]["auth_status"] == "pending"
    assert len(authorize_calls) == 1

    monkeypatch.setattr(fake_db, "query", real_query)  # crash resolved -- app restarted

    result = payments.charge(
        loan_id=42, processor_token=_VALID_MOCK_TOKEN, last4="1111", brand="visa",
        amount=250.0, idempotency_key="crash-before-persist",
    )

    assert result["status"] == "captured"
    assert len(authorize_calls) == 1  # never re-issued a charge on retry
    assert fake_db._by_key["crash-before-persist"]["authorization_id"] == "auth_abc123"


def test_authorize_charge_fails_closed_when_no_processor_is_configured(monkeypatch):
    """Outside development/test, a missing processor must refuse to
    authorize rather than silently approve against a fake authority --
    same fail-closed contract as decision-service's bureau/AI-scorer calls.

    Exercised at the unit level (not via TestClient) matching
    decision-service's test_decision.py convention: TestClient re-raises an
    unhandled exception instead of returning the 500 response the app's own
    exception handler would produce, so pytest.raises against the function
    itself is the correct way to assert the fail-closed contract here.
    """
    from app import processor as processor_module

    monkeypatch.setattr(processor_module, "ALLOW_PAYMENT_STUB", False)

    with pytest.raises(processor_module.ProcessorUnavailableError):
        processor_module.authorize_charge(_VALID_MOCK_TOKEN, 250.0)


# --- caller-controlled values in the RETRY logs ------------------------------
#
# Reviewed on PR #16. The first charge log goes through redact_dict, but the
# duplicate-retry branches interpolated `idempotency_key` directly -- so a
# caller using a PAN or an SSN as their key had it masked on the initial
# request and written in the clear on the retry, which is the one request that
# is guaranteed to happen twice.

@pytest.mark.parametrize("secret", ["4111111111111111", "412-55-9981"])
def test_the_api_refuses_a_key_carrying_card_or_personal_data(fake_db, caplog, secret):
    """The boundary, which is the fix that actually removes the exposure.

    Redaction protects the log; it does nothing about the copy PostgreSQL keeps,
    and `idempotency_key` is persisted on the payments row. So a key carrying a
    PAN or an SSN is refused outright -- 422, nothing inserted, and the value
    never reaches a log line either.

    422 and not 500 is part of the assertion. Pydantic puts the raised
    ValueError object into the error's `ctx`, which this service's own 422
    handler could not serialise, so the first version of this validator turned a
    rejected request into "internal error" -- a boundary check that reports the
    server is broken is not a boundary check.
    """
    with caplog.at_level(logging.INFO, logger=payments.log.name):
        resp = client.post("/payments", json=_payload(idempotency_key=secret))

    assert resp.status_code == 422, resp.text
    assert "idempotency_key" in resp.text
    inserts = [c for c in fake_db.calls if c[0].strip().startswith("INSERT")]
    assert not inserts, "a rejected request still wrote a payments row"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in logged, f"the rejected value was logged: {logged!r}"


@pytest.mark.parametrize("secret,marker", [
    ("4111111111111111", "[PAN-REDACTED]"),
    ("412-55-9981", "[SSN-REDACTED]"),
])
def test_the_duplicate_branch_still_redacts_a_key_that_bypassed_the_api(
    fake_db, monkeypatch, caplog, secret, marker
):
    """Defence in depth, exercised through the REAL duplicate branch.

    The validator above stops such a key arriving over HTTP, so this calls
    `charge()` directly -- which is not a contrivance: `app/reconcile.py` drives
    the same function from stored rows, and a key predating the validator is
    exactly what it would replay.

    The previous version of this test called `log.info` itself with an
    already-redacted string, which asserted that logging a redacted value logs a
    redacted value -- true of any implementation, including one that formats the
    raw key three lines further down. Review of PR #16 asked for the actual
    duplicate flow, and that is the right ask: the redaction has to happen inside
    `charge()`, on the retry path, where the defect was. So this charges twice and
    reads `caplog`; the second call takes the duplicate branch.
    """
    monkeypatch.setattr(
        payments.httpx, "post", lambda *a, **k: _FakeServicingResponse()
    )
    kwargs = dict(loan_id=42, processor_token=_VALID_MOCK_TOKEN, last4="1111",
                  brand="visa", amount=125.0, method="card", idempotency_key=secret)

    with caplog.at_level(logging.INFO, logger=payments.log.name):
        first = payments.charge(**kwargs)
        second = payments.charge(**kwargs)

    # The precondition that makes this meaningful: the second call really did
    # take the duplicate path rather than charging again.
    assert first["payment_id"] == second["payment_id"]

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "duplicate POST /payments" in logged, (
        "the duplicate branch never logged, so this test proves nothing about it"
    )
    assert secret not in logged, (
        f"the retry log wrote the caller's key verbatim: {logged!r}"
    )
    assert marker in logged


def test_the_conflict_message_does_not_echo_a_raw_key(fake_db, monkeypatch):
    """The 409 body is caller-visible, and it was formatting the key verbatim.

    Asserted on the RAISED ERROR, not on `inspect.getsource`. Matching source
    text passes whenever the string happens to be spelled that way and fails on a
    refactor that is still correct -- it tests the file, not the behaviour. The
    key here is one the API would now reject, so `charge()` is called directly for
    the same reason as the test above.
    """
    monkeypatch.setattr(
        payments.httpx, "post", lambda *a, **k: _FakeServicingResponse()
    )
    secret = "4111111111111111"
    base = dict(processor_token=_VALID_MOCK_TOKEN, last4="1111", brand="visa",
                amount=250.0, method="card", idempotency_key=secret)

    payments.charge(loan_id=42, **base)
    # Same key, different loan: the conflict the 409 exists for. `charge()` raises
    # the domain error; the router is what turns it into a 409, so this asserts on
    # the message that becomes the response body.
    with pytest.raises(payments.IdempotencyKeyConflict) as exc:
        payments.charge(loan_id=43, **base)

    message = str(exc.value)
    assert secret not in message, f"the conflict echoed the caller's key: {message!r}"
    assert "[PAN-REDACTED]" in message


def test_brand_cannot_carry_a_card_number_into_the_payments_row():
    """`last4` was constrained and `brand` was not, though both are persisted."""
    from app import schemas

    with pytest.raises(Exception) as exc:
        schemas.PaymentIn(loan_id=1, processor_token="tok_test_placeholder",
                          last4="1111", brand="4111111111111111", amount=10.0,
                          idempotency_key="k")
    assert "brand" in str(exc.value)

    ok = schemas.PaymentIn(loan_id=1, processor_token="tok_test_placeholder",
                           last4="1111", brand="visa", amount=10.0,
                           idempotency_key="k")
    assert ok.brand == "visa"


# --- the 422 contract ---------------------------------------------------------
#
# Reviewed on PR #16. Every Pydantic error carries `loc` as a tuple, and
# JSONResponse renders a tuple as a JSON array. A blanket str() fallback in
# `_sanitize_non_finite` -- added to stop an exception object in `ctx` crashing
# the response -- caught those tuples too and turned `("body",
# "idempotency_key")` into the string "('body', 'idempotency_key')" on EVERY 422.
# The status code was unchanged, so nothing failed loudly; a client reading `loc`
# as an array to attach an error to a form field simply stopped working.

def test_a_422_reports_loc_as_an_array_not_a_stringified_tuple():
    """The response SHAPE, asserted on a plain missing-field error.

    Deliberately not the interesting validator: this is the shape every client
    depends on, so it is asserted on the most ordinary rejection there is.
    """
    body = _payload()
    del body["idempotency_key"]

    resp = client.post("/payments", json=body)

    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]
    assert isinstance(errors, list) and errors, resp.text
    locs = [e["loc"] for e in errors]
    assert ["body", "idempotency_key"] in locs, (
        f"loc is not the standard array: {locs!r}"
    )
    for loc in locs:
        assert isinstance(loc, list), f"loc must be a JSON array, got {type(loc).__name__}: {loc!r}"
        assert all(isinstance(part, (str, int)) for part in loc), loc


def test_a_custom_validator_error_keeps_the_array_and_still_returns_422():
    """Both halves at once, because fixing either alone regressed the other.

    `idempotency_key` raises ValueError, so Pydantic puts the exception object in
    `ctx` -- unserializable, and a 500 before it was handled. Handling it with a
    blanket str() then flattened `loc`. This asserts the status, the array, and
    that the exception was rendered as text rather than crashing the response.
    """
    resp = client.post("/payments", json=_payload(idempotency_key="4111111111111111"))

    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]
    assert ["body", "idempotency_key"] in [e["loc"] for e in errors]
    ctxs = [e.get("ctx", {}).get("error") for e in errors if e.get("ctx")]
    for value in ctxs:
        assert isinstance(value, str), f"ctx.error must be rendered as text, got {value!r}"


def test_a_nonfinite_amount_is_still_reported_rather_than_crashing_the_response():
    """The case the sanitizer was written for, kept under test.

    Starlette renders JSON with allow_nan=False, so an Infinity echoed back in
    `input` would raise inside the error response itself.
    """
    resp = client.post("/payments", data=json.dumps(_payload(amount=float("inf"))),
                       headers={"Content-Type": "application/json"})

    assert resp.status_code == 422, resp.text
    assert "amount" in resp.text
    for e in resp.json()["detail"]:
        assert isinstance(e["loc"], list)


# --- review round 2: never capture money we cannot credit ---------------------

def test_a_charge_is_refused_when_servicing_will_not_accept_our_token(monkeypatch):
    """Token skew must stop the charge, not strand it.

    Review round 2 (high): the token reaches servicing only AFTER the processor
    has authorized. If payment-service and servicing-service hold different
    values -- a rotation applied to one and not the other -- the borrower is
    charged and every apply is rejected, so the money sits captured and
    uncredited until a human reconciles it.

    An uncharged customer retries. A charged customer with no credit has to be
    found first.
    """
    from app import payments as payments_mod

    authorized = []

    class _Db:
        """Just enough to reach the preflight: the insert returns a payments row."""
        def query(self, sql, params=None):
            if sql.strip().upper().startswith("INSERT INTO PAYMENTS"):
                return [{"id": 1, "loan_id": 42, "amount": 10.0}]
            return []

    monkeypatch.setattr(payments_mod, "db", _Db())
    monkeypatch.setattr(payments_mod, "_servicing_auth_ok", lambda: False)
    monkeypatch.setattr(payments_mod.processor, "authorize_charge",
                        lambda *a, **k: authorized.append(a) or "auth_x")

    with pytest.raises(payments_mod.ServicingAuthUnavailable):
        payments_mod.charge(42, "tok_x", "1111", 10.0, "preflight-key-1")

    assert not authorized, (
        "the card was authorized even though servicing had already rejected our "
        "credentials -- that is the capture-without-credit this check prevents"
    )


def test_a_transient_preflight_failure_does_not_block_payments(monkeypatch):
    """Unknown is not known-bad.

    Making every servicing blip refuse payments would trade a rare accounting
    error for a common outage, so only an explicit 401/403 blocks the charge.
    """
    from app import payments as payments_mod

    def _boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(payments_mod.httpx, "get", _boom)
    assert payments_mod._servicing_auth_ok() is True


@pytest.mark.parametrize("status", [401, 403])
def test_the_preflight_reports_an_auth_rejection(monkeypatch, status):
    from app import payments as payments_mod

    class _Resp:
        status_code = status

    monkeypatch.setattr(payments_mod.httpx, "get", lambda *a, **k: _Resp())
    assert payments_mod._servicing_auth_ok() is False
