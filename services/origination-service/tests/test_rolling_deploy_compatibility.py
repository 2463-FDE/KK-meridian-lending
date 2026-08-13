"""During a rolling deploy the two services are different versions. Neither
combination may report an application as submitted with no CIP row behind it.

The hole: this version of origination sends only `application_id` and
`applicant_id`, because kyc-service grades the applicant row it reads for itself.
An OLD kyc-service still requires `name`, `dob`, `ssn` and `address` as strings,
so it answers **422**. That is neither a credential failure (401/403), nor
kyc-service's "I could not record this" (503), nor a connection error -- so it
fell through to the resilience branch and returned `200 submitted` with every CIP
flag false and no `kyc_checks` row. An application nobody could advance, reported
to the borrower as accepted.

**The fix is not to enumerate 422 as well.** Enumerating statuses is what produced
the hole: the next unlisted outcome does the same thing. Intake now VERIFIES that
a row landed for this application and treats any outcome that leaves none as the
same resumable failure.

**And it is not to send the identity fields again.** That would reintroduce a
second copy of the applicant's SSN on the wire to be ignored at the other end, and
undo a fix made for a real reason (review round 9: sending them made the CIP
verdict a function of the request rather than of stored state).

**Deployment order is therefore kyc-service first, then origination and the
frontend** -- documented in the runbook. New kyc-service accepts both payload
shapes; old kyc-service accepts only the fat one. Deploying origination first is
the combination this file proves is survivable but degraded: every application
becomes a resumable failure until kyc-service catches up.
"""
import httpx
import pytest

from app.routers import applications as router


class _Recorder:
    def __init__(self, kyc_row_persisted):
        self.kyc_row_persisted = kyc_row_persisted
        self.statements = []

    def query(self, sql, params=None):
        flat = " ".join(sql.split())
        self.statements.append((flat, params))
        if "SELECT applicant_id FROM applications" in flat:
            return [{"applicant_id": 4242}]
        if "FROM kyc_checks" in flat:
            return [{"cip_passed": True}] if self.kyc_row_persisted else []
        return []

    @property
    def status_updates(self):
        return [s for s, _ in self.statements if s.startswith("UPDATE applications SET status")]


@pytest.fixture
def intake(monkeypatch):
    def _install(kyc_row_persisted):
        db = _Recorder(kyc_row_persisted)
        monkeypatch.setattr(router, "db", db)
        monkeypatch.setattr(router.intake, "create_application",
                            lambda payload, resume_token=None: (8484, "acc-tok", "res-tok"))
        return db
    return _install


_BODY = {
    "name": "Robin Fictional", "dob": "1985-02-11", "ssn": "999-00-0042",
    "address": "1 Test Street", "zip_code": "99301",
    "amount": 9000, "term_months": 24, "income": 60000,
}


def _http_error(status):
    request = httpx.Request("POST", "http://kyc-service:8003/kyc/check")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# --- new origination + OLD kyc-service ---------------------------------------

def test_an_old_kyc_service_422_is_a_resumable_failure_not_a_submitted_application(
        intake, monkeypatch):
    """The exact rolling-deploy combination.

    Old kyc-service rejects the identity-free payload with 422. Before the fix
    this returned 200 submitted with all-false CIP and no row.
    """
    db = intake(kyc_row_persisted=False)
    monkeypatch.setattr(router.clients, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(_http_error(422)))

    with pytest.raises(router.HTTPException) as excinfo:
        router.submit_application(router.ApplicationIn(**_BODY))

    assert excinfo.value.status_code == 503
    detail = excinfo.value.detail
    assert detail["error"] == "identity_verification_unavailable"
    assert detail["app_id"] == 8484
    assert detail["resume_token"], "the failure carried no resume handle"


def test_that_failure_marks_the_application_rather_than_leaving_it_ambiguous(
        intake, monkeypatch):
    db = intake(kyc_row_persisted=False)
    monkeypatch.setattr(router.clients, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(_http_error(422)))

    with pytest.raises(router.HTTPException):
        router.submit_application(router.ApplicationIn(**_BODY))

    assert db.status_updates, (
        "the application was left at 'submitted' with no CIP row -- the state "
        "that looks accepted and cannot advance"
    )


@pytest.mark.parametrize("status", [422, 500, 502, 418])
def test_no_status_can_produce_a_submitted_application_without_a_row(
        intake, monkeypatch, status):
    """The general form. Enumerating statuses is what produced the hole; this
    asserts the property instead, over statuses nobody listed."""
    intake(kyc_row_persisted=False)
    monkeypatch.setattr(router.clients, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(_http_error(status)))

    with pytest.raises(router.HTTPException) as excinfo:
        router.submit_application(router.ApplicationIn(**_BODY))
    assert excinfo.value.status_code == 503


def test_a_connection_error_is_also_resumable(intake, monkeypatch):
    """kyc-service absent entirely -- the other rolling-deploy moment, when it is
    restarting."""
    intake(kyc_row_persisted=False)

    def _down(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(router.clients, "post", _down)

    with pytest.raises(router.HTTPException) as excinfo:
        router.submit_application(router.ApplicationIn(**_BODY))
    assert excinfo.value.status_code == 503


# --- NEW kyc-service + old origination ---------------------------------------

def test_new_kyc_service_still_accepts_a_payload_carrying_identity_fields():
    """The other direction: old origination sends the fat payload.

    New kyc-service must accept it -- and IGNORE the identity fields, grading the
    stored applicant instead. Accepting keeps the deploy survivable; ignoring is
    what stops a caller manufacturing evidence.
    """
    from app.schemas import ApplicationIn  # noqa: F401  (origination side)

    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve()
                           .parents[3] / "services" / "kyc-service"))
    try:
        from app.schemas import CipCheckIn as _KycIn
    except Exception:                                          # pragma: no cover
        pytest.skip("kyc-service package not importable from here")
    finally:
        sys.path.pop(0)

    fat = _KycIn(application_id=1, applicant_id=1, name="Robin", dob="1985-02-11",
                 ssn="999-00-0042", address="1 Test Street", entity_type=None)
    assert fat.application_id == 1
    thin = _KycIn(application_id=1, applicant_id=1)
    assert thin.application_id == 1, (
        "new kyc-service rejects the identity-free payload, so new origination "
        "cannot talk to it at all"
    )


# --- no PII regression --------------------------------------------------------

def test_the_kyc_payload_still_carries_no_identity_fields(intake, monkeypatch):
    """Fixing the rolling deploy must not reintroduce the SSN on the wire."""
    sent = {}
    intake(kyc_row_persisted=True)

    def _capture(base_url, path, payload, headers=None):
        sent.update(payload)
        return {"cip_passed": True, "name_verified": True, "dob_verified": True,
                "address_verified": True, "ssn_verified": True}

    monkeypatch.setattr(router.clients, "post", _capture)
    router.submit_application(router.ApplicationIn(**_BODY))

    for field in ("name", "dob", "ssn", "address", "entity_type"):
        assert field not in sent, (
            f"intake sent {field!r} to kyc-service -- the identity fields were "
            f"reintroduced for backward compatibility, which is the thing the "
            f"deployment order exists to avoid"
        )
    assert set(sent) == {"application_id", "applicant_id"}
