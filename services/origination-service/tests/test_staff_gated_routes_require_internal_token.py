"""Review finding: get_application_financials, get_loan_history, and
get_zip_disparate_impact_report all gated on X-User-Role alone -- the same
gap as /decision and /accept (see test_decision_and_accept_authz.py).
docker-compose.yml no longer publishes this service's host port, but that's
network topology, not an application-level check: a caller who reaches this
service any other way could set X-User-Role: admin itself with nothing to
verify the claim. X-Internal-Token (forwarded by the gateway on every /los/*
proxy, see gateway/app/main.py) is now required in addition to the role
claim -- these tests cover the "claims staff, no token" rejection for each
of the three routes _require_staff/_is_staff was added to.

PR #6 review finding: GET /applications/{app_id} (ApplicationDetail) had NO
auth check at all -- app_id is sequential/guessable, and the response
includes applicant PII (name/email/phone/address), loan amount/purpose,
decision outcome, offer terms, and manual-review rationale/reviewer
identity. It is now gated the same way as the routes above.

PR #6 self-review follow-up (A1): the COLLECTION endpoint GET /applications
was missed by that fix and is the strictly easier target -- it returns
applicant_name, amount, term_months, purpose and status for every
application at once, limit up to 200, with a server-side ?status= filter,
so no id-guessing was needed at all. Now gated identically. POST
/applications (borrower submission, same path, different operation) must
stay anonymous -- asserted below so a future tightening can't silently
break the /apply flow.

PR #6 self-review follow-up (A3): the legacy POST /board direct-boarding
endpoint had no authorization of any kind and was reachable anonymously
through the gateway's unrestricted /los/* passthrough. It has been removed
(zero callers repo-wide); accept_offer is the supported atomic boarding
path. Asserted below as "no longer routable".
"""
import contextlib
from unittest.mock import patch

from app import config, intake
from app.database import get_session
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_application_detail_anonymous_is_forbidden():
    resp = client.get("/applications/10")

    assert resp.status_code == 403


def test_application_detail_staff_role_without_internal_token_is_forbidden():
    resp = client.get("/applications/10", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 403


def test_application_detail_forbidden_regardless_of_whether_app_id_exists():
    # No existence oracle: a non-staff caller gets the identical 403 whether
    # app_id is a real application or one nowhere close to existing --
    # _require_staff must run before any database lookup.
    real = client.get("/applications/10")
    fake = client.get("/applications/999999999")

    assert real.status_code == fake.status_code == 403
    assert real.json() == fake.json()


# --- A1: the collection endpoint GET /applications ---------------------------

_STAFF_HEADERS = {"X-User-Role": "underwriter", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN}


def test_application_list_anonymous_is_forbidden():
    """The A1 blocker itself: an anonymous caller could page the whole
    portfolio -- applicant_name, amount, term_months, purpose, status."""
    resp = client.get("/applications")

    assert resp.status_code == 403


def test_application_list_staff_role_without_internal_token_is_forbidden():
    resp = client.get("/applications", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 403


def test_application_list_non_staff_role_with_valid_internal_token_is_forbidden():
    resp = client.get(
        "/applications",
        headers={"X-User-Role": "borrower", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    )

    assert resp.status_code == 403


class _EmptyResult:
    def all(self):
        return []


class _FakeSession:
    """Minimal stand-in for the SQLAlchemy session list_applications uses --
    the point of these two tests is that a staff caller gets PAST the gate
    and into the query, not what the query returns, so no live Postgres is
    needed (same dependency_overrides pattern as
    disclosure-service/tests/test_offers.py)."""

    def scalar(self, _stmt):
        return 0

    def execute(self, _stmt):
        return _EmptyResult()


@contextlib.contextmanager
def _staff_session():
    app.dependency_overrides[get_session] = lambda: _FakeSession()
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_application_list_succeeds_for_staff():
    with _staff_session():
        resp = client.get("/applications", headers=_STAFF_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and "total" in body


def test_application_list_status_filter_and_pagination_are_staff_only():
    """The filter/paging controls are what made this cheap to harvest --
    they must be unreachable anonymously, and still work for staff."""
    for qs in ("?status=approved", "?limit=200&offset=0", "?status=denied&limit=200"):
        assert client.get(f"/applications{qs}").status_code == 403, qs
    with _staff_session():
        for qs in ("?status=approved", "?limit=200&offset=0", "?status=denied&limit=200"):
            assert client.get(f"/applications{qs}", headers=_STAFF_HEADERS).status_code == 200, qs


def test_application_list_returns_no_applicant_pii_to_an_anonymous_caller():
    """Belt-and-braces on the actual leak: no applicant name, amount or
    status value may appear in the anonymous response body."""
    resp = client.get("/applications?limit=200")

    assert resp.status_code == 403
    for leaked in ("applicant_name", "amount", "term_months", "purpose", "items"):
        assert leaked not in resp.text


def test_application_submission_stays_anonymous():
    """Regression guard: POST on the SAME path is the borrower's own
    submission (frontend/app/apply/page.tsx) and must NOT be gated by the
    GET fix -- proven by reaching intake, not by a 403."""
    with patch.object(intake, "create_application", return_value=(4242, "tok")) as create:
        resp = client.post("/applications", json={
            "name": "Jane Borrower", "amount": 9000, "term_months": 24,
            "income": 40000, "purpose": "test",
        })

    assert resp.status_code != 403
    assert create.called, "borrower submission must still reach intake.create_application"


# --- A3: the legacy POST /board endpoint is gone -----------------------------

def test_legacy_board_endpoint_is_not_routable():
    """A3: unauthenticated direct boarding wrote loans+balances straight from
    the request body. The route is removed outright (zero callers repo-wide);
    accept_offer is the supported atomic path."""
    resp = client.post("/board", json={
        "app_id": 10, "applicant_name": "Attacker", "principal": 50000,
        "annual_rate_pct": 0.01, "term_months": 48,
    })

    assert resp.status_code in (404, 405)
    assert "loan_id" not in resp.text


def test_legacy_board_endpoint_cannot_be_reached_with_a_forged_staff_role():
    """A role claim (with or without a valid internal token) must not
    resurrect the removed route."""
    for headers in (
        {"X-User-Role": "admin"},
        {"X-User-Role": "admin", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    ):
        resp = client.post("/board", json={
            "app_id": 10, "applicant_name": "Attacker", "principal": 50000,
        }, headers=headers)
        assert resp.status_code in (404, 405), headers
        assert "loan_id" not in resp.text


def test_removed_board_route_writes_no_loan_or_balance():
    """The rejected call must not reach the boarding sink at all -- asserted
    against the real function, not a status code."""
    with patch.object(intake, "board_to_servicing") as sink:
        client.post("/board", json={
            "app_id": 10, "applicant_name": "Attacker", "principal": 50000,
        })

    assert not sink.called, "no loans/balances write may be attempted"


def test_accept_offer_remains_the_supported_boarding_path():
    """The transactional sink accept_offer uses must still exist -- removing
    the legacy route must not have taken the real path with it."""
    assert hasattr(intake, "board_to_servicing_tx")


def test_financials_staff_role_without_internal_token_is_forbidden():
    resp = client.get("/applications/10/financials", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 403


def test_financials_staff_role_with_wrong_internal_token_is_forbidden():
    resp = client.get(
        "/applications/10/financials",
        headers={"X-User-Role": "underwriter", "X-Internal-Token": "not-the-real-token"},
    )

    assert resp.status_code == 403


def test_loan_history_staff_role_without_internal_token_is_forbidden():
    resp = client.get("/applications/10/history", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 403


def test_zip_analysis_staff_role_without_internal_token_is_forbidden():
    resp = client.get("/applications/fair-lending/zip-analysis", headers={"X-User-Role": "admin"})

    assert resp.status_code == 403


def test_non_staff_role_is_forbidden_regardless_of_internal_token():
    # A correct internal token alone must not substitute for an actual staff
    # role claim -- both are required, not either/or.
    resp = client.get(
        "/applications/10/financials",
        headers={"X-User-Role": "borrower", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    )

    assert resp.status_code == 403
