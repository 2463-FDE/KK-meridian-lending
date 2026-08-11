"""Characterization tests for the KYC service's API layer + run_cip() edge cases.

Pins CURRENT behavior before any Week 9+ change (sanctions/OFAC, UBO capture)
touches this service. test_cip.py already documents that the entity/LLC gap
(D11) is deliberately left unguarded -- these tests exist to make that gap
concrete and visible, not to close it: they prove today's actual behavior
(an LLC passing CIP with no real person verified), so Week 9 has a documented
"before" to diff its fix against.
"""
import pytest
from fastapi.testclient import TestClient

from app.kyc import run_cip
from app.main import app
from app.routers import kyc as kyc_router

from .conftest import AUTH_HEADERS

client = TestClient(app)


class _FakeDb:
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
    monkeypatch.setattr(kyc_router, "db", db)
    return db


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "kyc-service"}


# --- run_cip() edge cases not covered by test_cip.py ------------------------

def test_run_cip_missing_fields_are_not_verified():
    result = run_cip({"name": "Jane Borrower"})
    assert result["name_verified"] is True
    assert result["dob_verified"] is False
    assert result["address_verified"] is False
    assert result["ssn_verified"] is False


def test_run_cip_entity_applicant_has_no_dob_or_ssn_verified():
    # Entity/LLC applicants have no personal dob/ssn to check at all.
    entity = {"name": "Acme Holdings LLC", "address": "1 Corporate Way"}
    result = run_cip(entity)
    assert result["dob_verified"] is False
    assert result["ssn_verified"] is False


# --- POST /kyc/check ---------------------------------------------------------

def test_kyc_check_individual_applicant_passes(fake_db):
    resp = client.post("/kyc/check", json={
        "application_id": 1, "applicant_id": 1,
        "name": "Jane Borrower", "dob": "1990-04-12", "ssn": "123-45-6789",
        "address": "42 Main St, Springfield",
    }, headers=AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pass"
    assert body["cip_passed"] is True
    assert body["check_id"] == 1
    # Hardcoded false regardless of applicant type -- no sanctions/UBO checks exist.
    assert body["sanctions_screened"] is False
    assert body["ubo_captured"] is False


def test_kyc_check_entity_applicant_with_no_ssn_or_dob_still_passes(fake_db):
    # Characterizes debt D11: cip_passed only requires name_verified AND
    # address_verified -- an entity with blank dob/ssn still clears CIP with
    # zero individual identity verified, and there is no UBO capture at all.
    resp = client.post("/kyc/check", json={
        "application_id": 2, "applicant_id": 2,
        "name": "Acme Holdings LLC", "dob": "", "ssn": "",
        "address": "1 Corporate Way", "entity_type": "LLC",
    }, headers=AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pass"
    assert body["cip_passed"] is True
    assert body["ubo_captured"] is False


def test_kyc_check_failing_applicant_missing_name_and_address(fake_db):
    resp = client.post("/kyc/check", json={
        "application_id": 3, "applicant_id": 3,
        "name": "", "dob": "1990-04-12", "ssn": "123-45-6789", "address": "",
    }, headers=AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fail"
    assert body["cip_passed"] is False


def test_kyc_check_persists_all_four_cip_flags(fake_db):
    client.post("/kyc/check", json={
        "application_id": 4, "applicant_id": 4,
        "name": "Jane Borrower", "dob": "1990-04-12", "ssn": "123-45-6789",
        "address": "42 Main St, Springfield",
    }, headers=AUTH_HEADERS)

    assert len(fake_db.calls) == 1
    _, params = fake_db.calls[0]
    assert params == (4, True, True, True, True)


def test_kyc_check_survives_db_failure_and_returns_check_id_negative_one(fake_db, monkeypatch):
    # Characterizes the current swallow-and-continue behavior: a persistence
    # failure is logged, not raised -- the caller still gets a 200 with
    # check_id=-1 rather than an error surfaced back to them.
    def _boom(sql, params=None):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(kyc_router.db, "query", _boom)

    resp = client.post("/kyc/check", json={
        "application_id": 5, "applicant_id": 5,
        "name": "Jane Borrower", "dob": "1990-04-12", "ssn": "123-45-6789",
        "address": "42 Main St, Springfield",
    }, headers=AUTH_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["check_id"] == -1
