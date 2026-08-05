"""PR #6 review, Gap C -- the CIP check must not log applicant PII.

kyc-service sits inside the application-intake flow: origination forwards the
same name/DOB/SSN/address to POST /kyc/check on every submission. That handler
used to open with

    log.info("POST /kyc/check req=%s", payload)  # full PII in the log (D5)

so fixing origination alone would have left the identical data in a second
service's logs. Identifiers only now.

The CIP verification RESULT (four booleans in kyc_checks) is a legitimate audit
record and is unaffected -- it contains no identity data.
"""
import logging

from app import db
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_CANARY = {
    "name": "Zephyrine Quillfeather",
    "dob": "1971-03-02",
    "ssn": "123456782",
    "address": "9931 Canary Hollow Terrace, Nowhere, ZZ",
}


def test_kyc_check_logs_no_applicant_pii(monkeypatch, caplog):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"id": 77}])
    caplog.set_level(logging.DEBUG)

    resp = client.post("/kyc/check", json={
        "application_id": 4242, "applicant_id": 99, **_CANARY,
    })

    assert resp.status_code == 200
    for field, value in _CANARY.items():
        assert value not in caplog.text, f"{field} canary leaked into the kyc log: {value!r}"


def test_kyc_check_still_logs_correlating_identifiers(monkeypatch, caplog):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"id": 77}])
    caplog.set_level(logging.INFO)

    client.post("/kyc/check", json={"application_id": 4242, "applicant_id": 99, **_CANARY})

    assert "application_id=4242" in caplog.text
    assert "applicant_id=99" in caplog.text


def test_kyc_check_still_returns_and_persists_the_verification_result(monkeypatch):
    """The audit record is a different concern from the application log."""
    captured = {}

    def _fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [{"id": 77}]

    monkeypatch.setattr(db, "query", _fake_query)

    resp = client.post("/kyc/check", json={
        "application_id": 4242, "applicant_id": 99, **_CANARY,
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["check_id"] == 77
    assert body["application_id"] == 4242
    assert "INSERT INTO kyc_checks" in captured["sql"]
    # Only the applicant id and four booleans are persisted -- no identity data.
    assert captured["params"][0] == 99
    assert all(isinstance(p, bool) for p in captured["params"][1:])
