"""A human may override the model. Not the identification.

Review round 10. `run_decision` refuses an application with no passing CIP row
for that application id, and `review_application` did not. So a staff member
could resolve any pre-existing `refer` -- write `manual_reviews`, flip
`decisions.outcome` to approve, and be issued an accept token -- with identity
never proven. `accept_offer` then boarded it, because it trusted the two paths
upstream to have checked.

The reachable case is legacy rows, not a hypothetical. `db/migrations/0032` leaves
`application_id` NULL wherever the link could not be inferred, and those
applications have no CIP result for THIS application -- which is exactly the
population sitting in `refer` waiting for a human.

Both gates run inside the caller's existing row-locked transaction, on the
connection holding the lock, so a concurrent write cannot land a decision the
check would have refused.
"""
import contextlib

import pytest
from fastapi import HTTPException

from app import db
from app.routers import applications as applications_router


class _Cur:
    """The application is locked, referred, and its CIP state is the variable."""

    def __init__(self, kyc_rows, outcome="refer", status="submitted"):
        self.kyc_rows = kyc_rows
        self.outcome = outcome
        self.status = status
        self.executed = []
        self._last = []

    def execute(self, sql, params=None):
        stmt = " ".join(sql.split())
        self.executed.append(stmt)
        if stmt.startswith("SELECT cip_passed FROM kyc_checks"):
            self._last = self.kyc_rows
        elif stmt.startswith("SELECT status FROM applications"):
            self._last = [{"status": self.status}]
        elif stmt.startswith("SELECT outcome FROM decisions"):
            self._last = [{"outcome": self.outcome}]
        else:                                          # pragma: no cover
            raise AssertionError(f"unexpected statement after the gate: {stmt}")

    def fetchall(self):
        return self._last


def _run(monkeypatch, kyc_rows):
    cur = _Cur(kyc_rows)

    @contextlib.contextmanager
    def _tx():
        yield cur

    monkeypatch.setattr(db, "transaction", _tx)
    monkeypatch.setattr(applications_router, "_require_staff", lambda *a, **kw: None)
    monkeypatch.setattr(applications_router, "db", _StubDb(kyc_rows))
    return cur


class _StubDb:
    """The unlocked pre-checks `review_application` runs before its transaction."""

    def __init__(self, kyc_rows):
        self.kyc_rows = kyc_rows

    def query(self, sql, params=None):
        flat = " ".join(sql.split())
        if "FROM decisions" in flat:
            return [{"outcome": "refer"}]
        if "FROM kyc_checks" in flat:
            return self.kyc_rows
        return []


@pytest.mark.parametrize("kyc_rows, why", [
    ([], "no CIP row at all -- KYC never ran, or its result was never persisted"),
    ([{"cip_passed": None}], "a legacy row that records no verdict (pre-0033)"),
    ([{"cip_passed": False}], "a recorded CIP failure"),
])
def test_manual_review_is_refused_without_passing_identity_evidence(monkeypatch, kyc_rows, why):
    cur = _run(monkeypatch, kyc_rows)

    with pytest.raises(HTTPException) as excinfo:
        applications_router._require_persisted_kyc_locked(1, cur)

    assert excinfo.value.status_code == 409, why
    assert "identity verification" in str(excinfo.value.detail).lower()


def test_manual_review_proceeds_on_a_passing_row(monkeypatch):
    """The gate must not block the legitimate case, or it would be measured by
    referrals piling up rather than by anything being caught."""
    cur = _run(monkeypatch, [{"cip_passed": True}])

    applications_router._require_persisted_kyc_locked(1, cur)          # must not raise


def test_the_gate_reads_through_the_transactions_cursor(monkeypatch):
    """Not through db.query().

    `db.query()` runs on a different, autocommitted connection, so it can see a
    state the row lock was taken to exclude -- the check would be racing the very
    thing the lock exists to serialise.
    """
    cur = _run(monkeypatch, [{"cip_passed": True}])
    applications_router._require_persisted_kyc_locked(1, cur)

    assert any(s.startswith("SELECT cip_passed FROM kyc_checks") for s in cur.executed), (
        "the gate did not query through the locked cursor"
    )


def test_both_write_paths_call_the_locked_gate():
    """Derived from the source, because this is a list of protected things.

    Two paths can turn an unverified application into a funded loan: a staff
    manual review, and boarding. Both must call the locked gate, and a third path
    added later must fail here rather than quietly becoming the new hole.
    """
    import inspect
    import re

    for fn in (applications_router.review_application, applications_router.accept_offer):
        src = inspect.getsource(fn)
        assert re.search(r"_require_persisted_kyc_locked\(\s*app_id", src), (
            f"{fn.__name__} does not call the identity gate, so it can approve or "
            f"fund an application whose identity was never proven"
        )
