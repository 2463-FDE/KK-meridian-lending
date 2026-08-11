"""Decisioning must refuse an application with no persisted KYC result.

The companion to `test_kyc_auth_failure_blocks_intake.py`, and the reason that
one is not sufficient on its own.

Failing intake stops the *response* being a lie. It does not stop the row: the
applicant and application are committed before kyc-service is ever called, so a
credential rejection leaves a real application in the database. And
`POST /applications/{id}/decision` is directly reachable -- the gateway proxies
`/los/*` anonymously on purpose, because a freshly-submitted applicant has no
account yet -- so a caller holding an app_id can ask for a decision without ever
having seen what intake returned.

Without this gate, "intake failed" degrades to "intake failed, and then the
application was underwritten anyway".

**What counts as a KYC result is a persisted row, not a passing one.** A failed
CIP is a real recorded outcome the deny path is entitled to act on; blocking on
it would deny every applicant who fails verification any decision at all, which
is a different product and would break the denied-workflow E2E. The case being
refused is the one where KYC never ran or its result never persisted, which is
indistinguishable from an unverified applicant. That distinction is asserted
below rather than left implicit, because it is the one a future reader is most
likely to get wrong.
"""
import pytest
from fastapi import HTTPException

from app.routers import applications as applications_router


class _Db:
    """Answers the KYC-gate query and nothing else.

    `run_decision` runs a good deal before and after the gate; this stub returns
    the application row it needs and controls only whether a kyc_checks row
    exists, so the assertions are about the gate and not about decisioning.
    """

    def __init__(self, kyc_rows):
        self._kyc_rows = kyc_rows
        self.queries = []

    def query(self, sql, params=None):
        flat = " ".join(sql.split())
        self.queries.append(flat)
        if "FROM kyc_checks" in flat:
            return self._kyc_rows
        if "FROM applications a LEFT JOIN applicants" in flat:
            return [{
                "id": 8484, "applicant_id": 4242, "amount": 9000, "term_months": 24,
                "income": 100000, "status": "submitted", "name": "Jane", "ssn": "123456782",
                "access_token_hash": None, "access_token_expires_at": None,
                "access_token_used_at": None,
            }]
        return []


def _run(db):
    return applications_router.run_decision(
        8484,
        applications_router.DecisionIn(),
        x_user_role=None,
        x_internal_token=None,
    )


def test_an_application_with_no_kyc_row_cannot_be_decided(monkeypatch):
    db = _Db(kyc_rows=[])
    monkeypatch.setattr(applications_router, "db", db)

    with pytest.raises(HTTPException) as excinfo:
        _run(db)

    assert excinfo.value.status_code == 409
    assert "identity verification" in str(excinfo.value.detail).lower()


def test_the_gate_runs_before_any_bureau_call_or_attempt_row(monkeypatch):
    """A refused application must cost nothing and leave nothing behind.

    If the gate ran after the attempt lease, a rejected request would still
    burn a decision_attempts row; after the credit pull, it would bill a real
    hard inquiry for an applicant we declined to identify.
    """
    db = _Db(kyc_rows=[])
    monkeypatch.setattr(applications_router, "db", db)

    def _must_not_run(*a, **kw):                                   # pragma: no cover
        raise AssertionError("work was done before the KYC gate rejected the request")

    monkeypatch.setattr(applications_router.decision_state, "start_decision_attempt", _must_not_run)
    monkeypatch.setattr(applications_router.clients, "post", _must_not_run)

    with pytest.raises(HTTPException):
        _run(db)

    assert not any("decision_attempts" in q for q in db.queries)


def test_a_recorded_but_FAILED_cip_is_still_decidable(monkeypatch):
    """The gate is "did KYC run", not "did KYC pass".

    A denied applicant has a real, persisted CIP result; refusing to decide them
    would mean an applicant who fails verification can never be told no. The
    denied-workflow E2E depends on exactly this.
    """
    db = _Db(kyc_rows=[{"1": 1}])
    monkeypatch.setattr(applications_router, "db", db)

    # Past the gate, decisioning proceeds -- proven by it reaching the next step
    # rather than raising the gate's own 409.
    def _stop_after_gate(*a, **kw):
        raise RuntimeError("reached decisioning")

    monkeypatch.setattr(applications_router.decision_state, "start_decision_attempt", _stop_after_gate)

    with pytest.raises(Exception) as excinfo:
        _run(db)

    assert not isinstance(excinfo.value, HTTPException) or excinfo.value.status_code != 409, (
        "an application with a recorded (failed) CIP result was blocked by the KYC gate"
    )


def test_the_gate_keys_on_the_applications_applicant(monkeypatch):
    """kyc_checks has no application_id, so the join must go through applicants.

    Recorded because the obvious query -- `WHERE application_id = %s` -- would
    silently return nothing for every application and block the entire product.
    """
    db = _Db(kyc_rows=[{"1": 1}])
    monkeypatch.setattr(applications_router, "db", db)
    monkeypatch.setattr(
        applications_router.decision_state, "start_decision_attempt",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("reached decisioning")),
    )

    with pytest.raises(Exception):
        _run(db)

    gate = next(q for q in db.queries if "FROM kyc_checks" in q)
    assert "JOIN applications" in gate and "a.applicant_id = k.applicant_id" in gate
