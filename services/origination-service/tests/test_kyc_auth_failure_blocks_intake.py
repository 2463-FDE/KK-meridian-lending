"""A KYC credential rejection must not become a successful application.

PR #18 review, high severity. Adding an `X-Internal-Token` check to kyc-service
created a failure mode that did not exist before it: `submit_application` wrapped
the call in one broad `except`, so the 401 that a skewed or unset token now
produces was handled as the transient hiccup the fallback was written for. Intake
returned `200 submitted` with all four CIP booleans false, no `kyc_checks` row was
written, and the flow looked entirely healthy.

That is worse than an outage. An outage is visible. This silently switched
identity verification off for every applicant while continuing to accept them, and
the only trace was one `WARNING` line per application.

Three properties are pinned here, because closing only the first leaves the hole
open through a different door:

  1. a 401/403 does not produce a successful intake response;
  2. the application row that was already committed is marked, durably, so it is
     not left indistinguishable from a verified one;
  3. decisioning refuses an application with no persisted KYC result -- checked
     separately in `test_decision_requires_persisted_kyc.py`, because
     `POST /applications/{id}/decision` is reachable directly and does not care
     what intake returned.

Both directions are asserted, because a test that only proved the 401 case would
pass just as well on a version that failed intake for every KYC error.

**What "the other direction" actually is, corrected.** Two tests here used to be
named `test_a_timeout_still_takes_the_application` and
`test_a_kyc_5xx_still_takes_the_application`, and their docstrings said a KYC
outage must not stop someone applying, "deliberately preserved". They passed --
but only because `_RecordingDb.kyc_row_persisted` defaults to True, so the
authoritative `FROM kyc_checks` lookup at the end of `submit_application` found a
row. They were describing a fallback the production code no longer has.

Production is fail-closed on the ROW, not on the exception type. A timeout or a
5xx takes the soft path through `except Exception`, and then the CIP-row check
runs: if no row landed, the application is marked `kyc_unverified` and intake
returns 503 whatever the exception was. The only reason a timeout can still end
in `200 submitted` is that kyc-service committed its row before the client gave
up -- a real and ordinary outcome, and a much narrower claim than "an outage does
not stop an application".

So the two tests are renamed for the condition they actually hold (a persisted
row), and the case they were mistaken for -- a first submission, timing out, with
no row -- is asserted separately and directly.
"""
import httpx
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


def _http_error(status: int) -> httpx.HTTPStatusError:
    """A real HTTPStatusError, as `clients.post`'s raise_for_status() produces."""
    request = httpx.Request("POST", "http://kyc-service:8003/kyc/check")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


class _RecordingDb:
    def __init__(self):
        self.statements = []

    #: Whether kyc-service is deemed to have persisted a CIP row for this
    #: application. Intake now VERIFIES that rather than inferring it from the
    #: exception type -- an old kyc-service answers the identity-free payload
    #: with 422, which is neither a credential failure nor a connection error,
    #: and used to fall through to "submitted" with no row behind it.
    kyc_row_persisted = True

    def query(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))
        if "SELECT applicant_id FROM applications" in sql:
            return [{"applicant_id": 4242}]
        if "FROM kyc_checks" in sql:
            return [{"cip_passed": True}] if self.kyc_row_persisted else []
        return []

    def status_updates(self):
        return [(s, p) for s, p in self.statements if s.startswith("UPDATE applications SET status")]


@pytest.fixture
def intake(monkeypatch):
    recorder = _RecordingDb()
    monkeypatch.setattr(applications_router, "db", recorder)
    monkeypatch.setattr(
        applications_router.intake, "create_application",
        lambda payload, resume_token=None: (8484, "raw-submission-token", "resume-tok"),
    )
    return recorder


def _kyc_raises(monkeypatch, exc):
    def _post(base_url, path, payload, headers=None):
        raise exc
    monkeypatch.setattr(applications_router.clients, "post", _post)


@pytest.mark.parametrize("status", [401, 403, 503])
def test_a_rejected_credential_does_not_produce_a_submitted_application(intake, monkeypatch, status):
    _kyc_raises(monkeypatch, _http_error(status))

    with pytest.raises(Exception) as excinfo:
        applications_router.submit_application(applications_router.ApplicationIn(**_BODY))

    detail = getattr(excinfo.value, "status_code", None)
    assert detail == 503, (
        f"a {status} from kyc-service returned a normal response instead of failing "
        "intake: an application nobody could identify was accepted as submitted"
    )


@pytest.mark.parametrize("status", [401, 403, 503])
def test_the_persisted_application_is_marked_not_left_ambiguous(intake, monkeypatch, status):
    """The rows are already committed when KYC is called, so failing loudly is
    not enough on its own -- the application must not look like a normal one."""
    _kyc_raises(monkeypatch, _http_error(status))

    with pytest.raises(Exception):
        applications_router.submit_application(applications_router.ApplicationIn(**_BODY))

    updates = intake.status_updates()
    assert updates, "the application was left in its default 'submitted' state"
    sql, params = updates[0]
    assert applications_router.KYC_UNVERIFIED_STATUS in params
    assert 8484 in params
    # Only from 'submitted' -- a later state must never be clobbered by a retry.
    assert "status = 'submitted'" in sql


def test_a_timeout_whose_kyc_row_landed_anyway_takes_the_application(intake, monkeypatch):
    """The client gave up; kyc-service committed its row before it did.

    This is the ONLY shape in which a timeout still ends in `200 submitted`, and
    naming the precondition is the point: the fixture's
    `kyc_row_persisted = True` is not scaffolding here, it is the condition under
    test. Under the previous name this read as "a KYC outage never stops an
    application", which is not what the code does -- see
    `test_a_timeout_with_no_persisted_row_refuses_the_intake` below.

    The application is taken because the compliance evidence exists; the response
    reports all-false CIP because this process never saw the verdict.
    """
    intake.kyc_row_persisted = True
    _kyc_raises(monkeypatch, httpx.ConnectTimeout("kyc-service timed out"))

    result = applications_router.submit_application(applications_router.ApplicationIn(**_BODY))

    assert result["status"] == "submitted"
    assert result["kyc"].name_verified is False
    assert not intake.status_updates(), (
        "a transient failure that left a CIP row behind must not mark the application"
    )


def test_a_timeout_with_no_persisted_row_refuses_the_intake(intake, monkeypatch):
    """A first submission, kyc-service unreachable, no CIP row anywhere.

    The case the old timeout test was read as covering and never touched. There
    is no compliance evidence for this applicant, so intake fails closed: the
    already-committed application row is marked `kyc_unverified` and the caller
    gets a resumable 503 rather than a `submitted` it could never advance.

    Asserted on the response the caller receives, not on the log line, because
    the defect this guards against is precisely a failure that looks like
    success.
    """
    intake.kyc_row_persisted = False
    _kyc_raises(monkeypatch, httpx.ConnectTimeout("kyc-service timed out"))

    with pytest.raises(applications_router.HTTPException) as excinfo:
        applications_router.submit_application(applications_router.ApplicationIn(**_BODY))

    assert excinfo.value.status_code == 503
    detail = excinfo.value.detail
    assert detail["error"] == "identity_verification_unavailable"
    assert detail["app_id"] == 8484
    assert detail["resume_token"], "the failure carried no handle to retry with"

    updates = intake.status_updates()
    assert updates, "the application was left looking like a normal submission"
    assert applications_router.KYC_UNVERIFIED_STATUS in updates[0][1]


def test_a_kyc_5xx_whose_row_landed_is_taken_and_left_to_the_decision_gate(intake, monkeypatch):
    """A server-side error is kyc-service's problem, not a credential problem.

    500 is deliberately NOT grouped with 503. A 503 from kyc-service is its
    specific "I could not record this" signal, which tells us there is no
    compliance row; a 500 tells us nothing, so with a row on file the
    application is taken and the decision gate -- which reads the database rather
    than trusting a status code -- decides whether it may advance.

    With no row on file a 500 refuses like everything else, which
    `test_rolling_deploy_compatibility.py::test_no_status_can_produce_a_submitted_application_without_a_row`
    asserts over 422/500/502/418. The precondition is in the name here so the two
    are not read as contradicting each other.
    """
    intake.kyc_row_persisted = True
    _kyc_raises(monkeypatch, _http_error(500))

    result = applications_router.submit_application(applications_router.ApplicationIn(**_BODY))

    assert result["status"] == "submitted"
    assert not intake.status_updates()


def test_the_happy_path_is_unchanged(intake, monkeypatch):
    def _post(base_url, path, payload, headers=None):
        assert headers == {"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN}
        return {"cip_passed": True, "name_verified": True, "dob_verified": True,
                "address_verified": True, "ssn_verified": True}
    monkeypatch.setattr(applications_router.clients, "post", _post)

    result = applications_router.submit_application(applications_router.ApplicationIn(**_BODY))

    assert result["status"] == "submitted"
    assert result["kyc"].name_verified is True
    assert result["kyc"].ssn_verified is True
    assert not intake.status_updates()


def test_the_intake_response_cannot_report_more_than_kyc_recorded(intake, monkeypatch):
    """Review round 6 (medium): it could, and this test used to require it to.

    The four booleans were rebuilt from the single `cip_passed` flag --
    `ssn_verified = passed and not is_entity` -- so a passing individual was
    reported as having a verified SSN whether or not one was ever checked. The
    version of the happy-path test above literally asserted that: a stub
    returning `{"cip_passed": True}` and nothing else was expected to produce
    `ssn_verified is True`. The test encoded the defect, which is why the defect
    survived a review that read the tests.

    Here kyc-service reports a pass on name and address with no SSN verified --
    which is what an entity result looks like, and what a partially verified
    individual's would look like too. The response must say so.
    """
    def _post(base_url, path, payload, headers=None):
        return {"cip_passed": True, "name_verified": True, "dob_verified": False,
                "address_verified": True, "ssn_verified": False}
    monkeypatch.setattr(applications_router.clients, "post", _post)

    result = applications_router.submit_application(applications_router.ApplicationIn(**_BODY))

    assert result["kyc"].name_verified is True
    assert result["kyc"].address_verified is True
    assert result["kyc"].ssn_verified is False, (
        "the intake response reported a verified SSN that kyc-service never "
        "recorded"
    )
    assert result["kyc"].dob_verified is False
