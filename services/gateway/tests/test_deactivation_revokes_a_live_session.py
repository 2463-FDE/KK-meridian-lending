"""G-02 -- deactivating an account revokes what it is already holding.

THE GAP, measured on a running stack before this change. The gateway's Redis
session is a snapshot taken at login and lives `SESSION_TTL_SECONDS` -- eight
hours by default. Nothing re-read the account behind it, so `is_active` was
consulted at login and never again. With the flag set false and the *same*
bearer token reused, `POST /auth/login` correctly refused with 401 while that
session went on to:

  * answer `GET /auth/me` with 200,
  * raise a balance-adjustment proposal (202),
  * raise a fee waiver (202),
  * APPROVE a pending movement (200) -- writing a real `ledger_entries` row,
  * assess a late fee (200) -- writing another,
  * record a reconciliation review disposition (200),
  * and deny an application (200), setting the `adverse_action_reason` the
    applicant is told.

Offboarding and compromise response were therefore ineffective for up to eight
hours on the highest-authority routes in the system.

WHY THE FIX IS IN `get_session` rather than at each route. That function is the
single funnel every path shares: `/auth/me` resolves through it, `_proxy` sets
the `X-User-Id`/`X-User-Role` pair every backend trusts from what it returns,
and the Ed25519 principal the gateway mints for servicing is built from the same
dict. One check covers all of them; eight call sites would have covered seven.

These cases are the boundary half. The database half -- what holds when a
deactivation commits *concurrently* with an approval, which no application check
can close -- is `db/tests/test_0048_resolver_authority_is_current.py`.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app


ACTIVE_ROW = [{"is_active": True}]
INACTIVE_ROW = [{"is_active": False}]

_STAFF = {"id": 7, "username": "underwriter", "role": "underwriter",
          "name": "Sam Okafor (Underwriting)", "applicant_id": None}
_BORROWER = {"id": 9, "username": "maria", "role": "borrower",
             "name": "Maria Gonzalez", "applicant_id": 1}


class _SessionRedis:
    """Just enough Redis to hold one session, plus the rate limiter's counters."""

    def __init__(self, session: dict | None):
        self._raw = json.dumps(session) if session is not None else None

    def get(self, key):
        return self._raw if key.startswith("session:") else None

    def incr(self, key):
        return 1

    def expire(self, key, seconds):
        pass


@pytest.fixture()
def stack(monkeypatch):
    """Wire a session into Redis and let each case decide what the DB says."""
    def _build(session, rows):
        monkeypatch.setattr(auth, "_client", lambda: _SessionRedis(session))
        seen = []

        def _query(sql, params=None):
            seen.append((sql, params))
            return rows

        monkeypatch.setattr(auth.db, "query", _query)
        return seen
    return _build


# ---------------------------------------------------------------------------
# 1. The unit: what get_session does with each answer
# ---------------------------------------------------------------------------

def test_an_active_account_resolves_normally(stack):
    """The control. A check that refused everybody would pass every case below."""
    stack(_STAFF, ACTIVE_ROW)
    assert auth.get_session("tok") == _STAFF


def test_a_deactivated_account_no_longer_resolves(stack):
    stack(_STAFF, INACTIVE_ROW)
    assert auth.get_session("tok") is None


def test_a_deleted_account_no_longer_resolves(stack):
    """Fail closed. An account that cannot be found is not a current one."""
    stack(_STAFF, [])
    assert auth.get_session("tok") is None


def test_the_check_reads_the_account_named_by_the_session(stack):
    """Not by anything the caller supplied.

    The id comes from the server-side session, so a caller cannot point the
    lookup at a different, still-active account.
    """
    seen = stack(_STAFF, ACTIVE_ROW)
    auth.get_session("tok")
    assert seen, "no database read was made -- the snapshot was trusted"
    sql, params = seen[-1]
    assert "is_active" in sql and "users" in sql
    assert params == (_STAFF["id"],)


def test_borrowers_are_revoked_too(stack):
    """The gap was found on staff routes; the control is not staff-specific.

    A deactivated borrower keeping eight hours of access to their own loan and
    payment routes is the same defect with a smaller blast radius.
    """
    stack(_BORROWER, INACTIVE_ROW)
    assert auth.get_session("tok") is None


def test_no_token_short_circuits_before_any_database_read(stack):
    """An anonymous caller must not cost a query.

    `/los/*` resolves a session on every request including anonymous ones, so a
    lookup here would put a database read on the borrower application path for
    callers who have no session at all.
    """
    seen = stack(_STAFF, ACTIVE_ROW)
    assert auth.get_session("") is None
    assert seen == []


def test_an_absent_session_short_circuits_before_any_database_read(stack):
    """Expired or logged out: Redis says no, and that settles it."""
    seen = stack(None, ACTIVE_ROW)
    assert auth.get_session("tok") is None
    assert seen == []


def test_the_session_is_not_destroyed_by_a_refusal(stack):
    """Refused is not the same as logged out, and the difference matters.

    A deactivation can be reversed. Deleting the session on a failed check would
    also mean a brief database problem silently ending every session in
    progress -- failing closed in the wrong direction: unavailable rather than
    merely refused.
    """
    deleted = []
    stack(_STAFF, INACTIVE_ROW)
    monkey = getattr(auth, "delete_session")
    try:
        auth.delete_session = lambda token: deleted.append(token)
        assert auth.get_session("tok") is None
    finally:
        auth.delete_session = monkey
    assert deleted == [], "a refused check must not destroy the session"


# ---------------------------------------------------------------------------
# 2. Through the app: the routes the gap was measured on
# ---------------------------------------------------------------------------

def test_auth_me_refuses_a_deactivated_account(stack):
    """The first symptom that was measured: 200 while login said 401."""
    stack(_STAFF, INACTIVE_ROW)
    with TestClient(app) as client:
        resp = client.get("/auth/me", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 401


def test_auth_me_still_answers_an_active_account(stack):
    stack(_STAFF, ACTIVE_ROW)
    with TestClient(app) as client:
        resp = client.get("/auth/me", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "underwriter"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/lss/accounts/4471/adjust-balance",
         {"component": "fees", "amount": 1.0, "reason": "g02"}),
        ("post", "/lss/accounts/4471/waive-fee", {"amount": -1.0, "reason": "g02"}),
        ("post", "/lss/movements/1/resolve", {"resolution": "approved"}),
        ("post", "/lss/accounts/4471/late-fee", {}),
        ("post", "/lss/reconciliation/review-queue/1/disposition",
         {"disposition": "legitimate_distinct_payment", "note": "g02"}),
    ],
)
def test_every_money_and_state_route_refuses_a_deactivated_account(stack, method, path, body):
    """The five servicing paths the gap was demonstrated on.

    Parametrised from the measured list rather than written out, so a sixth
    route added later is a visible omission here rather than a silent one.

    401 and not 403: the session no longer identifies anyone the gateway will
    act for, which is an authentication answer. Nothing is proxied, so servicing
    is never asked, and no principal is minted -- the assertion that would have
    carried this identity is never signed.
    """
    stack(_STAFF, INACTIVE_ROW)
    with TestClient(app) as client:
        resp = getattr(client, method)(
            path, json=body, headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 401, resp.text


def test_no_principal_is_minted_for_a_deactivated_account(stack, monkeypatch):
    """Not merely refused downstream -- never signed in the first place.

    A minted assertion is a bearer credential valid for its whole TTL. Refusing
    after minting would leave a signed statement of authority in existence for
    an account that no longer has any.
    """
    from app import principal

    minted = []
    real_mint = principal.mint
    monkeypatch.setattr(principal, "mint",
                        lambda user: minted.append(user) or real_mint(user))
    stack(_STAFF, INACTIVE_ROW)
    with TestClient(app) as client:
        client.post("/lss/accounts/4471/late-fee", json={},
                    headers={"Authorization": "Bearer tok"})
    assert minted == [], "a principal was signed for a deactivated account"
