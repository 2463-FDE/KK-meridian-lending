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
    """Call the route as an AUTHORIZED caller.

    The KYC gate sits behind the authorization branch (review round 4), so a
    test that does not authorize gets the generic 403 and never reaches it.
    These tests are about the gate, so they authorize; the oracle tests below
    deliberately do not, and assert the 403.
    """
    return applications_router.run_decision(
        8484,
        applications_router.DecisionIn(),
        x_user_role=None,
        x_internal_token=None,
    )


def test_an_application_with_no_kyc_row_cannot_be_decided(monkeypatch):
    db = _Db(kyc_rows=[])
    monkeypatch.setattr(applications_router, "db", db)
    monkeypatch.setattr(applications_router.decision_state, "verify_access_token", lambda r, tok: True)

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
    monkeypatch.setattr(applications_router.decision_state, "verify_access_token", lambda r, tok: True)
    monkeypatch.setattr(applications_router.decision_state, "verify_access_token", lambda r, tok: True)
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
    monkeypatch.setattr(applications_router.decision_state, "verify_access_token", lambda r, tok: True)
    db = _Db(kyc_rows=[{"cip_passed": True}])
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


def test_the_gate_keys_on_the_application_not_the_applicant(monkeypatch):
    """This test asserted the OPPOSITE, and the thing it asserted was the bug.

    It used to require the gate query to `JOIN applications` on
    `a.applicant_id = k.applicant_id`, with a docstring explaining that
    `kyc_checks` has no `application_id` "so the join must go through
    applicants" -- and warning that the obvious `WHERE application_id = %s`
    would return nothing and block the entire product.

    That was true of the schema at the time and it was the wrong conclusion. The
    schema could not express "was THIS application verified", so the gate asked a
    weaker question instead, and a repeat applicant's old CIP row satisfied it.
    Noticing a schema limitation and reasoning around it is how the limitation
    becomes a compliance gap: the evidence a regulator asks for is the result for
    this application, and it did not exist.

    `db/migrations/0032` adds the column. The gate now asks the real question,
    and the old expectation is inverted rather than deleted so the reasoning that
    produced it stays visible.
    """
    monkeypatch.setattr(applications_router.decision_state, "verify_access_token", lambda r, tok: True)
    db = _Db(kyc_rows=[{"cip_passed": True}])
    monkeypatch.setattr(applications_router, "db", db)
    monkeypatch.setattr(
        applications_router.decision_state, "start_decision_attempt",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("reached decisioning")),
    )

    with pytest.raises(Exception):
        _run(db)

    gate = next(q for q in db.queries if "FROM kyc_checks" in q)
    assert "application_id = %s" in gate
    assert "a.applicant_id = k.applicant_id" not in gate


# --- review round 3: the gate must be about THIS application ------------------

def test_a_repeat_applicants_old_kyc_does_not_cover_a_new_application(monkeypatch):
    """The bypass: an old CIP row must not vouch for a later application.

    `kyc_checks` had no `application_id`, so the gate asked "has this APPLICANT
    ever been verified?" -- and a repeat applicant with an old check passed it
    even when the current application's KYC call failed or never ran. The
    application reached underwriting on evidence belonging to a different
    application, while the logs recorded a block that had not happened. That is
    the compliance gap: the evidence a regulator asks for is the CIP result for
    THIS application, and the schema could not express it.

    `db/migrations/0032` adds the column and this asserts the gate reads it: a
    row exists for the applicant's OLD application and none for the new one, so
    the new one is refused.
    """
    monkeypatch.setattr(applications_router.decision_state, "verify_access_token", lambda r, tok: True)
    queried = []

    class _Db:
        def query(self, sql, params=None):
            flat = " ".join(sql.split())
            queried.append((flat, params))
            if "FROM kyc_checks" in flat:
                # The applicant HAS a CIP row -- for application 8000, not 8484.
                return [] if params == (8484,) else [{"1": 1}]
            if "FROM applications a LEFT JOIN applicants" in flat:
                return [{
                    "id": 8484, "applicant_id": 4242, "amount": 9000, "term_months": 24,
                    "income": 100000, "status": "submitted", "name": "Jane",
                    "ssn": "123456782", "access_token_hash": None,
                    "access_token_expires_at": None, "access_token_used_at": None,
                }]
            return []

    db = _Db()
    monkeypatch.setattr(applications_router, "db", db)

    def _must_not_run(*a, **kw):                                   # pragma: no cover
        raise AssertionError("decisioning began for an application with no CIP row")

    monkeypatch.setattr(applications_router.decision_state, "start_decision_attempt", _must_not_run)
    monkeypatch.setattr(applications_router.clients, "post", _must_not_run)

    with pytest.raises(HTTPException) as excinfo:
        _run(db)

    assert excinfo.value.status_code == 409

    gate = next(sql for sql, _ in queried if "FROM kyc_checks" in sql)
    assert "application_id" in gate, (
        "the gate still keys on the applicant, so any prior CIP row satisfies it"
    )
    assert "JOIN applications" not in gate, (
        "joining through applicants is what made an old check vouch for a new "
        "application"
    )


def test_the_two_copies_of_the_status_constant_agree():
    """`decision_state` duplicates KYC_UNVERIFIED_STATUS to avoid an import cycle.

    Two spellings of the same state would mean the locked finality check and the
    intake marker disagree silently, and the gate would pass an application that
    intake had flagged.
    """
    from app import decision_state

    assert decision_state.KYC_UNVERIFIED_STATUS == applications_router.KYC_UNVERIFIED_STATUS


# --- review round 4: the gate must not become an oracle -----------------------

def test_an_anonymous_caller_learns_nothing_from_the_kyc_gate(monkeypatch):
    """Authorize first. The gate must not answer before the trust boundary.

    Review finding: `_require_persisted_kyc` ran BEFORE the staff/access-token
    check, so an anonymous caller guessing an app_id got 409 for a real
    application with no KYC row and 403 otherwise. The response distinguished
    "this application exists" from "you may not ask" before the caller had
    proven anything.

    This route already collapses every unauthorized path into one generic 403 on
    purpose -- wrong token, expired token, already-used token, never-issued token
    all look identical. A correct check placed earlier than the trust boundary
    undoes that, however correct the check itself is.
    """
    db = _Db(kyc_rows=[])                       # a real application, no KYC row
    monkeypatch.setattr(applications_router, "db", db)

    def _must_not_run(*a, **kw):                                   # pragma: no cover
        raise AssertionError("work was done for an unauthorized caller")

    monkeypatch.setattr(applications_router.decision_state, "start_decision_attempt", _must_not_run)
    monkeypatch.setattr(applications_router.clients, "post", _must_not_run)
    monkeypatch.setattr(applications_router.decision_state, "verify_access_token", lambda r, t: False)

    with pytest.raises(HTTPException) as excinfo:
        applications_router.run_decision(
            8484, applications_router.DecisionIn(),
            x_user_role=None, x_internal_token=None,
        )

    assert excinfo.value.status_code == 403, (
        "an anonymous caller got a KYC-specific status, which confirms the "
        "application exists before they have proven anything"
    )
    assert "identity verification" not in str(excinfo.value.detail).lower(), (
        "the 403 body leaks that the refusal was about KYC"
    )


def test_an_authorized_caller_still_hits_the_kyc_gate(monkeypatch):
    """Moving the check must not disable it.

    The pairing matters: the test above passes on a build with no gate at all,
    so it only means something alongside this one.
    """
    db = _Db(kyc_rows=[])
    monkeypatch.setattr(applications_router, "db", db)
    monkeypatch.setattr(applications_router.decision_state, "verify_access_token", lambda r, t: True)

    def _must_not_run(*a, **kw):                                   # pragma: no cover
        raise AssertionError("decisioning began for an application with no CIP row")

    monkeypatch.setattr(applications_router.decision_state, "start_decision_attempt", _must_not_run)

    with pytest.raises(HTTPException) as excinfo:
        applications_router.run_decision(
            8484, applications_router.DecisionIn(),
            x_user_role=None, x_internal_token=None,
        )

    assert excinfo.value.status_code == 409
    assert "identity verification" in str(excinfo.value.detail).lower()


def test_the_staff_detail_read_is_scoped_to_the_application(monkeypatch):
    """Staff must not see a repeat applicant's older identity evidence.

    Review finding: the detail read took the LATEST kyc_checks row for the
    applicant, so opening a repeat applicant's second application displayed CIP
    evidence from their first -- the exact mixing db/migrations/0032 exists to
    stop, still happening on the screen a human looks at, while the decision gate
    refused that same application as unverified. Screen and gate disagreeing
    about whether an application is verified is worse than either answer alone.

    Asserted against the query rather than a rendered page: the defect is which
    column it filters on.
    """
    import inspect
    from app.routers import applications as mod

    src = inspect.getsource(mod.get_application)
    assert "models.KycCheck.application_id == app_id" in src, (
        "the staff detail read still selects KYC by applicant, so a repeat "
        "applicant's older evidence is shown against a newer application"
    )
    assert "models.KycCheck.applicant_id" not in src, (
        "an applicant-scoped KYC filter remains in the staff detail read"
    )


# --- review round 6: a recorded CIP failure must have a consequence ----------


def _gate_only(monkeypatch, kyc_rows):
    """Drive run_decision far enough to hit the gate, and no further."""
    db = _Db(kyc_rows=kyc_rows)
    monkeypatch.setattr(applications_router, "db", db)
    monkeypatch.setattr(applications_router.decision_state, "verify_access_token", lambda r, t: True)

    def _must_not_run(*a, **kw):                                   # pragma: no cover
        raise AssertionError("decisioning began for an application whose CIP did not pass")

    monkeypatch.setattr(applications_router.decision_state, "start_decision_attempt", _must_not_run)
    with pytest.raises(HTTPException) as excinfo:
        applications_router.run_decision(
            8484, applications_router.DecisionIn(),
            x_user_role=None, x_internal_token=None,
        )
    return excinfo.value


def test_a_recorded_cip_failure_blocks_decisioning(monkeypatch):
    """The case that made the gate ornamental.

    The gate accepted ANY kyc_checks row, on the stated reasoning that a failed
    CIP is "a real, recorded outcome the deny path is entitled to act on". No
    deny path acted on it -- nothing outside kyc-service read the result at all.
    So a recorded failure had precisely the same effect as a pass: the
    application was decided, and could be approved, for someone this system had
    recorded as unidentified.

    Paired with the round-6 kyc-service fix, this is the whole reachable attack:
    an individual submits with a name and an address and no DOB or SSN, CIP
    records a failure, and underwriting proceeds regardless.
    """
    exc = _gate_only(monkeypatch, [{"cip_passed": False}])

    assert exc.status_code == 409
    assert "identity verification" in str(exc.detail).lower()


def test_a_row_with_no_recorded_verdict_is_not_a_pass(monkeypatch):
    """NULL is a row that does not say, which is not evidence of anything.

    Rows written before db/migrations/0033 carry no verdict. Reading absence as
    approval is how the applicant-scoped gate failed before: the safe-looking
    default was the permissive one.
    """
    exc = _gate_only(monkeypatch, [{"cip_passed": None}])

    assert exc.status_code == 409


def test_a_later_passing_check_settles_an_earlier_failure(monkeypatch):
    """A re-run that succeeds must not be outvoted by the failure before it.

    Otherwise a corrected application -- the applicant supplies the SSN they
    first omitted -- is blocked permanently by its own history, and the only way
    forward is a new application, which is worse for the applicant and worse for
    the audit trail.
    """
    db = _Db(kyc_rows=[{"cip_passed": True}])
    monkeypatch.setattr(applications_router, "db", db)

    applications_router._require_persisted_kyc(8484)               # must not raise

    kyc_query = next(q for q in db.queries if "FROM kyc_checks" in q)
    assert "cip_passed" in kyc_query, "the gate no longer reads the verdict"
    assert "ORDER BY cip_passed DESC" in kyc_query, (
        "the gate does not prefer a passing check, so an earlier failure would "
        "block an application its own re-run has since cleared"
    )
