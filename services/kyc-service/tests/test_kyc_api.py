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
        # Review round 2: the handler now verifies the application/applicant
        # linkage before inserting, so this fake has to answer two different
        # questions. The linkage read returns a match; only the INSERT consumes
        # an id, which keeps the check_id assertions below meaningful.
        if "FROM applications" in sql:
            return [{"1": 1}]
        row = {"id": self._next_id}
        self._next_id += 1
        return [row]

    @property
    def inserts(self):
        return [(s, p) for s, p in self.calls if "INSERT INTO kyc_checks" in s]


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

    assert len(fake_db.inserts) == 1
    _, params = fake_db.inserts[0]
    # (applicant_id, application_id, then the four CIP booleans). application_id
    # was added by db/migrations/0032 so the decision gate can ask "was THIS
    # application verified" rather than "has this applicant ever been verified",
    # which a repeat applicant's old row used to satisfy.
    assert params == (4, 4, True, True, True, True)


def test_kyc_check_reports_a_persistence_failure_instead_of_faking_success(fake_db, monkeypatch):
    """Review round 2 changed this behaviour, and the test with it.

    This used to characterize swallow-and-continue: a persistence failure was
    logged, not raised, and the caller got 200 with check_id=-1. That is a
    "verified" answer with no compliance record behind it -- and once the
    decision gate began requiring a persisted kyc_checks row, it also produced an
    applicant who was told they were submitted and then blocked later with no
    explanation. The old expectation is kept here as history rather than deleted,
    because it was deliberate behaviour and its removal is the fix.
    """
    def _boom(sql, params=None):
        if "INSERT INTO kyc_checks" in sql:
            raise RuntimeError("db unavailable")
        return [{"1": 1}]

    monkeypatch.setattr(kyc_router.db, "query", _boom)

    resp = client.post("/kyc/check", json={
        "application_id": 5, "applicant_id": 5,
        "name": "Jane Borrower", "dob": "1990-04-12", "ssn": "123-45-6789",
        "address": "42 Main St, Springfield",
    }, headers=AUTH_HEADERS)

    assert resp.status_code == 503
    assert "record" in resp.json()["detail"].lower()


def test_an_entity_applicant_is_accepted_without_dob_or_ssn(fake_db):
    """An LLC has no DOB or SSN, and this API must not require them.

    Review finding: dob/ssn/address were required strings here while
    origination's ApplicationIn has all three Optional. So an entity applicant --
    which this service's own CIP logic explicitly clears on name and address
    alone -- produced a 422, no kyc_checks row was written, intake still reported
    "submitted", and the decision gate later refused the application. Every
    entity application, every time.
    """
    resp = client.post("/kyc/check", json={
        "application_id": 7, "applicant_id": 7,
        "name": "Northgate Holdings LLC", "address": "1 Corporate Way",
        "entity_type": "llc",
    }, headers=AUTH_HEADERS)

    assert resp.status_code == 200, f"entity applicant rejected: {resp.text[:200]}"
    body = resp.json()
    assert body["cip_passed"] is True
    assert body["check_id"] > 0, "no CIP row was persisted, so decisioning will refuse it"
    assert len(fake_db.inserts) == 1


def test_a_sparse_individual_application_still_persists_a_result(fake_db):
    """A missing field verifies as False -- it does not abort the check.

    `run_cip` already treats each field as possibly absent, so accepting None
    changes no verification behaviour; it only stops the request being rejected
    before that logic runs.
    """
    resp = client.post("/kyc/check", json={
        "application_id": 8, "applicant_id": 8, "name": "Jane Borrower",
    }, headers=AUTH_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["cip_passed"] is False, "no address, so CIP must not pass"
    assert resp.json()["check_id"] > 0, "a failed CIP is still a recorded result"
