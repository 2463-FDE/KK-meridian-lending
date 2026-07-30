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
"""
from app import config
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


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
