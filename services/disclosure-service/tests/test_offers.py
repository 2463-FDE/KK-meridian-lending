"""Tests for the offer creation/read endpoints (previously untested end-to-end --
nothing exercised the offer route against a DB at all before this).

Review finding (W4 PR): create_offer() used to trust a caller-supplied decision_id
directly -- the FK only proves SOME decision with that id exists, not that it
belongs to this application_id. A request with application_id=A, decision_id=B
(no real relation between them) would sail through, leaking applicant B's
decision into application A's audit trail. These tests cover the fix:
decision_id is now always derived server-side (via the INSERT's own SELECT ...
FROM decisions), a mismatched/malicious decision_id can never leak through, and
a request with no approved decision on record is rejected.

Also covers two later review findings on the same route:
- The approval check and the offer write used to be two separate statements (a
  SELECT, then an INSERT) -- a concurrent decision rerun could flip the
  outcome to 'deny' in the gap between them. Folding the check into the
  INSERT's own SELECT ... FROM decisions WHERE outcome = 'approve' makes the
  check and the write atomic.
- ON CONFLICT (decision_id) DO UPDATE used to recompute APR/finance charge/
  fee_pct_used from whatever the fee config happens to be right now on every
  retried/duplicated call -- a fee-rule change between the original request
  and a retry would silently change the borrower's canonical disclosure.
  DO NOTHING instead, falling back to the already-stored row, so a retry
  always gets back the ORIGINAL terms.

Also covers: get_offer() reading fee_pct_used from the stored row instead of
the live ORIGINATION_FEE_PCT constant -- the exact drift this column exists to
prevent.
"""
from decimal import Decimal

import psycopg2.errors
from app import config, db
from app import apr as apr_mod
from app import offer as offer_mod
from app.database import get_session
from app.main import app
import pytest
from fastapi.testclient import TestClient

client = TestClient(app)
# Defaulted for every request in this file so the pre-existing tests below
# don't each need updating -- the X-Internal-Token rejection tests further
# down override/clear it per-call instead.
client.headers.update({"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})


class _FakeDb:
    """Simulates the offers table state machine for POST /offers.

    - the application lookup returns `application_rows`.
    - the atomic INSERT ... SELECT ... FROM decisions ... ON CONFLICT DO
      NOTHING RETURNING only succeeds once per decision_id, and only if
      `decision_approved` -- every call after the first (or any call when the
      decision isn't approved) returns no rows, exactly like a real
      ON CONFLICT DO NOTHING would.
    - the fallback SELECT ... FROM offers WHERE decision_id then returns
      whatever the first successful insert stored (or nothing, if the
      decision was never approved).
    """

    def __init__(self, application_rows=None, decision_approved=True, insert_id=101):
        self.application_rows = (
            application_rows if application_rows is not None
            else [{"amount": 12000, "term_months": 36}]
        )
        self.decision_approved = decision_approved
        self.insert_id = insert_id
        self.stored_offer = None
        self.calls = []

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        if "FROM applications" in sql:
            return self.application_rows
        if "INSERT INTO offers" in sql:
            if self.decision_approved and self.stored_offer is None:
                # PR #10 review: the offer records the CONTRACTUAL note rate
                # alongside the disclosed APR (boarding reads the former), and the
                # Model B payment schedule as stored fact -- regular payment count,
                # the adjusted final payment, the term, and the rounding-policy
                # version. 12 params, in INSERT column order.
                (fee_pct_used, note_rate_pct, apr, finance_charge, monthly_payment,
                 amount_financed, total_of_payments, regular_payment_count,
                 final_payment, term_months, schedule_version, application_id) = params
                self.stored_offer = {
                    "id": self.insert_id, "app_id": application_id, "decision_id": application_id,
                    "fee_pct_used": fee_pct_used, "note_rate_pct": note_rate_pct,
                    "apr": apr, "finance_charge": finance_charge,
                    "monthly_payment": monthly_payment, "amount_financed": amount_financed,
                    "total_of_payments": total_of_payments,
                    "regular_payment_count": regular_payment_count,
                    "final_payment": final_payment, "term_months": term_months,
                    "schedule_version": schedule_version, "accepted_at": None,
                }
                return [self.stored_offer]
            return []
        if "FROM offers WHERE decision_id" in sql:
            return [self.stored_offer] if self.stored_offer is not None else []
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
    fake_db = _FakeDb(decision_approved=False)
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post("/offers", json=_offer_payload(application_id=10))

    assert resp.status_code == 422


def test_create_offer_rejects_missing_internal_token(monkeypatch):
    """Defense in depth for POST /offers -- see docker-compose.yml (no host
    port for this service) and app/config.py."""
    fake_db = _FakeDb()
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post("/offers", json=_offer_payload(application_id=10), headers={"X-Internal-Token": ""})

    assert resp.status_code == 401


def test_create_offer_rejects_wrong_internal_token(monkeypatch):
    fake_db = _FakeDb()
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
    fake_db = _FakeDb()
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post("/offers", json=_offer_payload(application_id=10), headers={"X-Internal-Token": ""})

    assert resp.status_code == 401


def test_create_offer_ignores_mismatched_client_decision_id(monkeypatch):
    """The exact review scenario: application_id=10 with a completely unrelated
    decision_id=999 supplied by the caller. A real approved decision exists for
    application 10, so the request succeeds -- but the persisted decision_id
    must be the server-derived one (10), never the client-supplied 999.
    decision_id isn't even a bound parameter on the insert anymore (it's
    derived entirely inside the SQL via decisions.app_id), so 999 can never
    appear in any query this request issues."""
    fake_db = _FakeDb(insert_id=555)
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post("/offers", json=_offer_payload(application_id=10, decision_id=999))

    assert resp.status_code == 200
    assert resp.json()["decision_id"] == 10
    assert all(999 not in (params or ()) for _, params in fake_db.calls)


def test_create_offer_insert_atomically_checks_approval_and_upserts_by_decision(monkeypatch):
    """The approval check must be folded into the insert's own SELECT (not a
    separate prior SELECT) so a concurrent decision rerun can't flip the
    outcome in between, and the conflict path must be non-mutating (DO
    NOTHING, not DO UPDATE) so a retry can never rewrite accepted terms."""
    fake_db = _FakeDb()
    monkeypatch.setattr(db, "query", fake_db.query)

    client.post("/offers", json=_offer_payload(application_id=10))

    insert_call = next(c for c in fake_db.calls if "INSERT INTO offers" in c[0])
    assert "FROM decisions" in insert_call[0]
    assert "outcome = 'approve'" in insert_call[0]
    assert "ON CONFLICT (decision_id)" in insert_call[0]
    assert "DO NOTHING" in insert_call[0]
    assert "DO UPDATE" not in insert_call[0]


def test_create_offer_falls_back_to_read_when_insert_hits_the_app_id_constraint(monkeypatch):
    """Concurrency fix (borrower-workflow audit, found by a real-Postgres
    test -- see db/tests/test_offer_creation_concurrency.py): offers.
    decision_id and offers.app_id are two SEPARATE UNIQUE constraints, even
    though this INSERT always sets them equal. A genuinely concurrent
    insert can violate offers_app_id_key, which ON CONFLICT (decision_id)
    does not target -- this used to raise an unhandled 500. It must be
    caught and treated exactly like ON CONFLICT DO NOTHING firing: fall
    through to the read-back, report created=False, never leak a raw
    UniqueViolation to the caller."""
    fake_db = _FakeDb()
    fake_db.stored_offer = {
        "id": 55, "app_id": 10, "decision_id": 10, "fee_pct_used": 0.03,
        "apr": 7.99, "finance_charge": 500.0, "monthly_payment": 400.0,
        "amount_financed": 8700.0, "total_of_payments": 9600.0,
    }
    real_query = fake_db.query

    def _raise_on_insert(sql, params=None):
        if "INSERT INTO offers" in sql:
            fake_db.calls.append((sql, params))
            raise psycopg2.errors.UniqueViolation("duplicate key value violates offers_app_id_key")
        return real_query(sql, params)

    monkeypatch.setattr(db, "query", _raise_on_insert)

    resp = client.post("/offers", json=_offer_payload(application_id=10))

    assert resp.status_code == 200
    assert resp.json()["created"] is False
    assert resp.json()["offer_id"] == 55


def test_create_offer_ignores_client_supplied_principal_and_term(monkeypatch):
    """Security fix: a caller used to be able to supply any principal/term/rate
    for an approved application_id and have it persisted verbatim, overwriting
    the real TILA numbers. The application's own record (amount=12000,
    term_months=36 here) must win over the different values in the request body."""
    fake_db = _FakeDb(application_rows=[{"amount": 12000, "term_months": 36}])
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post(
        "/offers",
        json=_offer_payload(application_id=10, principal=49999) | {"term_months": 60, "annual_rate": 35},
    )

    assert resp.status_code == 200
    expected = offer_mod.build_offer(12000, 7.99, 36)
    # The point of this test is that caller-supplied principal/term/rate are
    # ignored in favour of the application's own record -- so compare against
    # what build_offer produces for THAT, not against the rate the caller sent.
    assert resp.json()["apr"] == expected["apr"]
    assert resp.json()["monthly_payment"] == expected["monthly_payment"]

    insert_call = next(c for c in fake_db.calls if "INSERT INTO offers" in c[0])
    # Positional params are (fee_pct_used, note_rate_pct, apr, ...) since PR #10
    # split the contractual rate out of the disclosed APR. Both are checked:
    # neither may be derived from the caller-supplied principal=49999/rate=35.
    assert insert_call[1][1] == pytest.approx(7.99)          # note rate, from fees.NOTE_RATE_PCT
    assert insert_call[1][2] == expected["apr"]              # disclosed APR
    assert insert_call[1][1] != insert_call[1][2], "note rate and APR must not be the same value"


def test_repeated_create_offer_with_different_body_values_leaves_offer_unchanged(monkeypatch):
    """Review's exact ask: create an offer, repeat POST /offers for the same
    application_id with a different principal/rate/term, assert the persisted
    offer is unchanged. ON CONFLICT DO NOTHING never rewrites the row on a
    repeat call -- the second response is the ORIGINAL stored terms, read
    back via the fallback SELECT, never recomputed."""
    fake_db = _FakeDb(application_rows=[{"amount": 12000, "term_months": 36}])
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

    # Only one INSERT actually stores a row -- the retry hits ON CONFLICT DO
    # NOTHING and falls back to reading it, never a second write.
    insert_calls = [c for c in fake_db.calls if "INSERT INTO offers" in c[0]]
    assert len(insert_calls) == 2
    assert fake_db.insert_id == 101  # the one row minted, never replaced


def test_repeated_create_offer_after_fee_rule_change_returns_original_terms(monkeypatch):
    """The review's core scenario: the fee rule changes between the original
    request and a retry. The retry must return the ORIGINAL fee_pct_used/APR/
    monthly_payment, never numbers recomputed from the new rule -- otherwise
    the borrower's canonical disclosure silently changes underneath them."""
    fake_db = _FakeDb(application_rows=[{"amount": 12000, "term_months": 36}])
    monkeypatch.setattr(db, "query", fake_db.query)

    monkeypatch.setattr(offer_mod, "ORIGINATION_FEE_PCT", Decimal("0.05"))
    first = client.post("/offers", json=_offer_payload(application_id=10))
    assert first.status_code == 200
    assert first.json()["fee_pct_used"] == 0.05

    monkeypatch.setattr(offer_mod, "ORIGINATION_FEE_PCT", Decimal("0.10"))
    second = client.post("/offers", json=_offer_payload(application_id=10))
    assert second.status_code == 200
    assert second.json()["fee_pct_used"] == 0.05  # original rule, not the new 0.10
    assert second.json()["apr"] == first.json()["apr"]
    assert second.json()["disclosure"]["amount_financed"] == first.json()["disclosure"]["amount_financed"]


def test_create_offer_rejects_when_no_application_on_record(monkeypatch):
    fake_db = _FakeDb(application_rows=[])
    monkeypatch.setattr(db, "query", fake_db.query)

    resp = client.post("/offers", json=_offer_payload(application_id=10))

    assert resp.status_code == 404


class _FakeOffer:
    """Stand-in for the ORM Offer row.

    Every column the read path touches is defaulted here, so a double that
    omits one behaves like a NULL column rather than raising AttributeError.
    `note_rate_pct` defaults to None deliberately: that is the legacy,
    pre-0030 shape, and the read path's recovery branch is what it exercises.
    """

    note_rate_pct = None
    # Legacy (pre-0030) shape by default: no stored payment schedule. Tests that
    # want a modern row pass these explicitly.
    regular_payment_count = None
    final_payment = None
    term_months = None
    schedule_version = None

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


def test_stored_note_rate_is_preferred_over_recovery(monkeypatch):
    """A normal, post-0030 offer must never reach note_rate_from_payment().

    Recovery infers a rate from an already-rounded payment. It exists only so
    pre-0030 rows still render; preferring it over a persisted value would mean
    the displayed rate drifts from the contractual one by a rounding artefact.
    Proven by making the recovery raise: if the read path calls it for a row that
    has note_rate_pct, this test fails loudly instead of silently agreeing.
    """
    def _must_not_be_called(*a, **kw):
        raise AssertionError(
            "note_rate_from_payment() was called for an offer that has a stored "
            "note_rate_pct -- recovery is legacy compatibility only"
        )

    monkeypatch.setattr(apr_mod, "note_rate_from_payment", _must_not_be_called)

    offer = _FakeOffer(
        id=43, app_id=11, decision_id=11, fee_pct_used=0.03,
        note_rate_pct=7.99, apr=10.072, finance_charge=2369.15,
        monthly_payment=469.98, amount_financed=14550.0, total_of_payments=16919.15,
    )
    def _fake_get_session():
        yield _FakeSession(offer)

    app.dependency_overrides[get_session] = _fake_get_session
    try:
        resp = client.get("/applications/11/offer")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    assert resp.json()["disclosure"]["note_rate_pct"] == pytest.approx(7.99, abs=1e-3)


def test_a_legacy_offer_without_a_stored_note_rate_recovers(monkeypatch):
    """The other half: a genuine pre-0030 row still renders a rate.

    Removing the recovery entirely would blank the rate on historical offers, so
    it has to stay -- reachable only when note_rate_pct is actually absent.
    """
    called = {}

    def _recovery(principal, payment, term):
        called["args"] = (principal, payment, term)
        return 7.99

    monkeypatch.setattr(apr_mod, "note_rate_from_payment", _recovery)

    offer = _FakeOffer(
        id=44, app_id=12, decision_id=12, fee_pct_used=0.03,
        note_rate_pct=None,                      # the legacy shape
        apr=10.072, finance_charge=2369.15,
        monthly_payment=469.98, amount_financed=14550.0, total_of_payments=16919.15,
    )
    def _fake_get_session():
        yield _FakeSession(offer)

    app.dependency_overrides[get_session] = _fake_get_session
    try:
        resp = client.get("/applications/12/offer")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    assert called, "recovery was not reached for a row with no stored note rate"
    assert resp.json()["disclosure"]["note_rate_pct"] == pytest.approx(7.99, abs=1e-3)


def test_a_mismatched_body_term_does_not_reach_the_stored_schedule(monkeypatch):
    """The stored contractual term must come from the application, not the caller.

    The schedule and every derived amount are built from the server-side term, so
    persisting body.term_months would store a term contradicting the schedule it
    describes. Client-supplied principal/term/rate have been ignored since the
    PR #6 security review; the stored schedule term follows the same rule.
    """
    fake_db = _FakeDb(application_rows=[{"amount": 15000.0, "term_months": 36}])
    monkeypatch.setattr(db, "query", fake_db.query)

    # Caller asks for 60 months and a 49,000 principal; the application says 36
    # months / 15,000. Both request values are inside the schema's own bounds, so
    # this exercises the trust boundary rather than input validation.
    resp = client.post("/offers", json={"application_id": 10, "principal": 49000,
                                        "term_months": 60, "annual_rate": 24.0})

    assert resp.status_code == 200, resp.text
    stored = fake_db.stored_offer
    assert int(stored["term_months"]) == 36, (
        f"stored term {stored['term_months']} came from the request body, "
        f"not the application row"
    )
    assert int(stored["regular_payment_count"]) + 1 == int(stored["term_months"])
    assert stored["schedule_version"] == "B1"
