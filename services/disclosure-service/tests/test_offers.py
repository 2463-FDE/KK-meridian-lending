"""Tests for the offer creation/read endpoints (previously untested end-to-end --
nothing exercised the offer route against a DB at all before this).

Review finding (W4 PR): create_offer() used to trust a caller-supplied decision_id
directly -- the FK only proves SOME decision with that id exists, not that it
belongs to this application_id. A request with application_id=A, decision_id=B
(no real relation between them) would sail through, leaking applicant B's
decision into application A's audit trail. These tests cover the fix:
decision_id is now always derived server-side from application_id, a
mismatched/malicious decision_id can never leak through, a request with no
approved decision on record is rejected, and the insert is idempotent per
decision (a retried/duplicated call updates the one canonical offer instead of
minting a second one).

Also covers: get_offer() reading fee_pct_used from the stored row instead of
the live ORIGINATION_FEE_PCT constant -- the exact drift this column exists to
prevent.
"""
from app import config, db
from app import offer as offer_mod
from app.database import get_session
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)
# Defaulted for every request in this file so the pre-existing tests below
# don't each need updating -- the X-Internal-Token rejection tests further
# down override/clear it per-call instead.
client.headers.update({"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})


class _FakeDb:
    """Records every query() call; scripted to return the decision-lookup rows,
    the application-lookup row, and the insert's RETURNING row."""

    def __init__(self, decision_rows, insert_id=101, application_rows=None):
        self.decision_rows = decision_rows
        self.insert_id = insert_id
        self.application_rows = (
            application_rows if application_rows is not None
            else [{"amount": 12000, "term_months": 36}]
        )
        self.calls = []

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        if "FROM decisions" in sql:
            return self.decision_rows
        if "FROM applications" in sql:
            return self.application_rows
        if "INSERT INTO offers" in sql:
            return [{"id": self.insert_id}]
        raise AssertionError(f"unexpected query: {sql}")


def _offer_payload(application_id=10, decision_id=None, principal=12000):
    body = {
        "application_id": application_id,
        "principal": principal,
        "term_months": 36,
        "annual_rate": 7.99,
    }
    if decision_id is not None:
        body["decision_id"] = decision_id
    return body


def test_create_offer_rejects_when_no_approved_decision(monkeypatch):
    fake_db = _FakeDb(decision_rows=[])  # no approve decision on record
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post("/offers", json=_offer_payload(application_id=10))

    assert resp.status_code == 422


def test_create_offer_rejects_missing_internal_token(monkeypatch):
    """Defense in depth for POST /offers -- see docker-compose.yml (no host
    port for this service) and app/config.py."""
    fake_db = _FakeDb(decision_rows=[{"app_id": 10}])
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post("/offers", json=_offer_payload(application_id=10), headers={"X-Internal-Token": ""})

    assert resp.status_code == 401


def test_create_offer_rejects_wrong_internal_token(monkeypatch):
    fake_db = _FakeDb(decision_rows=[{"app_id": 10}])
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post(
        "/offers", json=_offer_payload(application_id=10),
        headers={"X-Internal-Token": "attacker-guessed-token"},
    )

    assert resp.status_code == 401


def test_create_offer_rejects_everything_when_config_token_unset(monkeypatch):
    """A deploy that forgets to set INTERNAL_SERVICE_TOKEN must fail closed --
    no caller (not even one that sends the empty string) should ever match."""
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "")
    fake_db = _FakeDb(decision_rows=[{"app_id": 10}])
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post("/offers", json=_offer_payload(application_id=10), headers={"X-Internal-Token": ""})

    assert resp.status_code == 401


def test_create_offer_ignores_mismatched_client_decision_id(monkeypatch):
    """The exact review scenario: application_id=10 with a completely unrelated
    decision_id=999 supplied by the caller. A real approved decision exists for
    application 10, so the request succeeds -- but the persisted decision_id
    must be the server-derived one (10), never the client-supplied 999."""
    fake_db = _FakeDb(decision_rows=[{"app_id": 10}], insert_id=555)
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post("/offers", json=_offer_payload(application_id=10, decision_id=999))

    assert resp.status_code == 200
    assert resp.json()["decision_id"] == 10

    insert_call = next(c for c in fake_db.calls if "INSERT INTO offers" in c[0])
    assert insert_call[1][1] == 10  # decision_id positional param -- not 999


def test_create_offer_insert_is_idempotent_per_decision(monkeypatch):
    """The insert must upsert on decision_id, not blindly insert every call --
    otherwise a retried/duplicated request mints a second offer for the same
    decision, and ORDER BY id DESC silently prefers whichever landed last."""
    fake_db = _FakeDb(decision_rows=[{"app_id": 10}])
    monkeypatch.setattr(db, "query", fake_db.query)

    client.post("/offers", json=_offer_payload(application_id=10))

    insert_call = next(c for c in fake_db.calls if "INSERT INTO offers" in c[0])
    assert "ON CONFLICT (decision_id)" in insert_call[0]
    assert "DO UPDATE" in insert_call[0]


def test_create_offer_ignores_client_supplied_principal_and_term(monkeypatch):
    """Security fix: a caller used to be able to supply any principal/term/rate
    for an approved application_id and have it persisted verbatim, overwriting
    the real TILA numbers. The application's own record (amount=12000,
    term_months=36 here) must win over the different values in the request body."""
    fake_db = _FakeDb(
        decision_rows=[{"app_id": 10}],
        application_rows=[{"amount": 12000, "term_months": 36}],
    )
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post(
        "/offers",
        json=_offer_payload(application_id=10, principal=49999) | {"term_months": 60, "annual_rate": 35},
    )

    assert resp.status_code == 200
    expected = offer_mod.build_offer(12000, 7.99, 36)
    assert resp.json()["apr"] == expected["apr"]
    assert resp.json()["monthly_payment"] == expected["monthly_payment"]

    insert_call = next(c for c in fake_db.calls if "INSERT INTO offers" in c[0])
    assert insert_call[1][3] == expected["apr"]  # apr positional param -- not derived from principal=49999


def test_repeated_create_offer_with_different_body_values_leaves_offer_unchanged(monkeypatch):
    """Review's exact ask: create an offer, repeat POST /offers for the same
    application_id with a different principal/rate/term, assert the persisted
    offer is unchanged. The ON CONFLICT (decision_id) DO UPDATE only ever writes
    server-derived values now, so a repeat call recomputes the identical numbers
    regardless of what the caller sends -- it can never drift."""
    fake_db = _FakeDb(
        decision_rows=[{"app_id": 10}],
        application_rows=[{"amount": 12000, "term_months": 36}],
    )
    monkeypatch.setattr(db, "query", fake_db.query)

    first = client.post("/offers", json=_offer_payload(application_id=10, principal=12000))
    second = client.post(
        "/offers",
        json=_offer_payload(application_id=10, principal=49999) | {"term_months": 60, "annual_rate": 35},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["apr"] == second.json()["apr"]
    assert first.json()["monthly_payment"] == second.json()["monthly_payment"]
    assert first.json()["disclosure"]["amount_financed"] == second.json()["disclosure"]["amount_financed"]


def test_create_offer_rejects_when_no_application_on_record(monkeypatch):
    fake_db = _FakeDb(decision_rows=[{"app_id": 10}], application_rows=[])
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post("/offers", json=_offer_payload(application_id=10))

    assert resp.status_code == 404


class _FakeOffer:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeSession:
    def __init__(self, offer):
        self._offer = offer

    def scalar(self, stmt):
        return self._offer


def test_get_offer_uses_stored_fee_pct_not_live_constant(monkeypatch):
    """Review finding: get_offer() used to recompute principal from the LIVE
    ORIGINATION_FEE_PCT constant instead of the fee_pct_used actually
    snapshotted on the row -- so a later fee-schedule change silently changed
    the recovered principal (and the redisplayed schedule) for every existing
    offer. Snapshots the row at 5%, changes the live constant to 10%, and
    confirms the stored 5% wins."""
    offer = _FakeOffer(
        id=42, app_id=10, decision_id=10, fee_pct_used=0.05,
        apr=7.99, finance_charge=500.0, monthly_payment=300.0,
        amount_financed=9500.0, total_of_payments=10800.0,
    )

    def _fake_get_session():
        yield _FakeSession(offer)

    app.dependency_overrides[get_session] = _fake_get_session
    monkeypatch.setattr(offer_mod, "ORIGINATION_FEE_PCT", 0.10)
    try:
        resp = client.get("/applications/10/offer")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["fee_pct_used"] == 0.05
    assert body["decision_id"] == 10
