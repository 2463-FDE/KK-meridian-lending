"""Intake must present X-Internal-Token when it calls kyc-service.

kyc-service now requires that header (its `routers/kyc.py`), closing the last
service that was reachable around the gateway: it was host-published on 8003 and
checked nothing, so an unauthenticated `POST localhost:8003/kyc/check` wrote a
`kyc_checks` row for any applicant_id.

That check is only safe to add because the two legitimate callers send the token.
This pins origination's half. The gateway's half is its `/kyc/{path}` route,
covered by `gateway/tests/test_auth_and_routes.py`'s proxy assertions.

Why this is worth its own test rather than trusting the intake flow to fail
loudly: `submit_application` deliberately swallows a kyc-service error and falls
back to all-CIP-false so a hiccup cannot 500 an application submission (the
resilience the handler documents). A 401 from a missing token lands in exactly
that except branch -- so dropping the header would not break intake, it would
silently stop verifying identity on every application while still returning 200.
A regression here is invisible unless something asserts on the header itself.
"""
import pytest

from app import config
from app.routers import applications as applications_router


_BODY = {
    "name": "Jane Borrower",
    "ssn": "123456782",
    "dob": "1990-04-12",
    "email": "jane@example.test",
    "phone": "5550101999",
    "address": "42 Main St, Springfield",
    "zip_code": "99301",
    "amount": 9000,
    "term_months": 24,
    "income": 100000,
}


class _RecordingClients:
    """Stands in for app.clients, capturing what intake sends to kyc-service."""

    def __init__(self, kyc_url):
        self.KYC_URL = kyc_url
        self.calls = []

    def post(self, base_url, path, payload, headers=None):
        self.calls.append({"base_url": base_url, "path": path,
                           "payload": payload, "headers": headers})
        return {"cip_passed": True}


@pytest.fixture
def recording_clients(monkeypatch):
    fake = _RecordingClients(config.KYC_URL)
    monkeypatch.setattr(applications_router, "clients", fake)
    # intake and the applicant_id lookup are not what this test is about.
    monkeypatch.setattr(
        applications_router.intake, "create_application",
        lambda payload: (8484, "raw-submission-token"),
    )
    monkeypatch.setattr(
        applications_router.db, "query",
        lambda sql, params=None: [{"applicant_id": 4242}],
    )
    return fake


def test_the_kyc_call_carries_the_internal_token(recording_clients):
    applications_router.submit_application(
        applications_router.ApplicationIn(**_BODY)
    )

    assert len(recording_clients.calls) == 1
    call = recording_clients.calls[0]
    assert call["path"] == "/kyc/check"
    assert call["headers"] == {"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN}
    assert config.INTERNAL_SERVICE_TOKEN, (
        "the test environment must set INTERNAL_SERVICE_TOKEN -- asserting "
        "equality against an empty value would pass on a handler that forwards "
        "nothing at all"
    )


def test_the_token_never_travels_in_the_kyc_request_body(recording_clients):
    """It is a header, not a field. A body copy would land in kyc-service's
    request logs and in any error surfaced back to a caller."""
    applications_router.submit_application(
        applications_router.ApplicationIn(**_BODY)
    )

    payload = recording_clients.calls[0]["payload"]
    assert config.INTERNAL_SERVICE_TOKEN not in str(payload)
