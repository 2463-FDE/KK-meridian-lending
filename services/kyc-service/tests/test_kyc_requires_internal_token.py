"""POST /kyc/check must refuse a caller that did not come through the gateway.

The defect this pins: kyc-service was the one backend service that was *both*
host-published (`docker-compose.yml` mapped 8003:8003) and tokenless. So

    curl -X POST localhost:8003/kyc/check -d '{"applicant_id": 7, ...}'

reached the handler with no authentication and wrote a `kyc_checks` row for
applicant 7 -- the CIP evidence a BSA/AML program relies on, forgeable by anyone
who could reach the host. The four sibling services were closed in PR #6.

Two independent guards, because they fail differently and either alone is thin:

  1. the network boundary -- no host port, asserted in
     `gateway/tests/test_decision_service_not_host_published.py`;
  2. this token check -- the fallback for the day someone re-publishes the port,
     runs the service outside compose, or reaches it from a compromised
     container on the same network. The boundary test cannot catch any of those.

The assertions below are about the *side effects*, not only the status code. A
401 that still ran `run_cip()`, still wrote the row, or still logged the payload
would satisfy a status-code-only test while leaving the defect intact -- and the
write is the part that matters here, since a fabricated `kyc_checks` row outlives
the request that made it.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import kyc as kyc_router

from .conftest import AUTH_HEADERS, INTERNAL_TOKEN

client = TestClient(app)

_BODY = {
    "application_id": 4242,
    "applicant_id": 99,
    "name": "Robin Fictional",
    "dob": "1985-02-11",
    "ssn": "123-45-6789",
    "address": "1 Test Street, Springfield",
}


class _RecordingDb:
    """Records every write so a rejected request can be proven to make none."""

    def __init__(self):
        self.calls = []

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        return [{"id": 1}]


@pytest.fixture
def db(monkeypatch):
    recorder = _RecordingDb()
    monkeypatch.setattr(kyc_router, "db", recorder)
    return recorder


@pytest.mark.parametrize(
    "headers, why",
    [
        ({}, "no token at all -- the original host-port bypass"),
        ({"X-Internal-Token": ""}, "empty token"),
        ({"X-Internal-Token": "wrong-token"}, "a guessed or stale token"),
        # The header name is what the gateway sets; a caller sending the value
        # under the identity header the gateway strips must not be accepted.
        ({"X-User-Role": "admin"}, "a spoofed role header instead of the token"),
    ],
)
def test_unauthorized_call_is_rejected_and_writes_nothing(db, headers, why):
    resp = client.post("/kyc/check", json=_BODY, headers=headers)

    assert resp.status_code == 401, why
    assert db.calls == [], (
        f"a rejected request ({why}) still wrote to the database: {db.calls}. "
        "The token check must run before run_cip() and before the INSERT -- a "
        "fabricated kyc_checks row outlives the request that created it."
    )


def test_unauthorized_call_does_not_log_the_applicant_identifiers(db, caplog):
    """A rejected request should leave no trace of its payload in the log.

    The handler's own log line is identifiers-only by design (PR #6, Gap C), but
    an unauthenticated caller should not be able to write even those -- it is a
    free channel into the log file for anyone who can reach the port.
    """
    with caplog.at_level("INFO"):
        client.post("/kyc/check", json=_BODY)

    assert "4242" not in caplog.text and "POST /kyc/check" not in caplog.text


def test_the_correct_token_still_reaches_the_handler(db):
    """The guard above is worthless if it also breaks the real intake path.

    origination-service calls this endpoint on every application submission, and
    the gateway proxies /kyc/* to it, so both now send the token.
    """
    resp = client.post("/kyc/check", json=_BODY, headers=AUTH_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["cip_passed"] is True
    assert len(db.calls) == 1, "the authorized call should persist exactly one row"
    assert "INSERT INTO kyc_checks" in db.calls[0][0]


def test_an_unset_server_token_fails_closed(db, monkeypatch):
    """A deploy that forgets INTERNAL_SERVICE_TOKEN must reject everything.

    The comparison is `not TOKEN or header != TOKEN`, so an empty server-side
    value cannot be matched -- not even by a caller sending an empty header,
    which a bare `header != TOKEN` would have let straight through. This is the
    same contract decision-service documents, asserted here rather than assumed.
    """
    monkeypatch.setattr(kyc_router.config, "INTERNAL_SERVICE_TOKEN", "")

    for headers in ({}, {"X-Internal-Token": ""}, {"X-Internal-Token": INTERNAL_TOKEN}):
        resp = client.post("/kyc/check", json=_BODY, headers=headers)
        assert resp.status_code == 401, headers

    assert db.calls == []
