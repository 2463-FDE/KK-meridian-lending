"""Where an application got to, from the rows and nothing else.

The underwriting detail page could show most of this already, in pieces, with one
real hole: the boarded loan id lived in React state. Reload the page and an
already-boarded application rendered "This application has already been boarded"
with **no id and no link** -- while `loans.app_id` had held the answer the whole
time and nothing read it back.

Two properties are worth testing and neither is cosmetic.

**Unknown is a third state, not a falsy second one.** No KYC row means the check
never ran; a KYC row with a failed field means it ran and did not pass. Rendering
those alike would let "we never checked" read as "we checked and it is
outstanding", and they carry different obligations for whoever is looking. Same
for a decision that does not exist versus one recorded as `refer`.

**No step is inferred from another.** A boarded loan is not taken as proof KYC
passed; an accepted offer is not taken as proof of approval. Each row is read on
its own, so the screen shows the record rather than a story consistent with it --
which matters precisely when the data is odd, which is when somebody is looking.

Driven against a fake session, because what is under test is the derivation and
its gate rather than SQLAlchemy. The boarded step reads `loans` through
`db.query` (origination owns no ORM model for a table servicing owns), so that
is stubbed alongside.
"""
import datetime
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import get_session
from app.routers import applications as applications_router

STAFF = {
    "X-User-Role": "underwriter",
    "X-Internal-Token": os.environ["INTERNAL_SERVICE_TOKEN"],
}
WHEN = datetime.datetime(2026, 8, 28, 12, 0, tzinfo=datetime.timezone.utc)


class _App:
    def __init__(self, app_id=7307, created_at=WHEN):
        self.id = app_id
        self.created_at = created_at


class _Kyc:
    def __init__(self, name=True, dob=True, address=True, ssn=True):
        self.name_verified = name
        self.dob_verified = dob
        self.address_verified = address
        self.ssn_verified = ssn


class _Decision:
    def __init__(self, outcome="approve"):
        self.outcome = outcome


class _Offer:
    def __init__(self, accepted_at=None, created_at=WHEN):
        self.accepted_at = accepted_at
        self.created_at = created_at


class _FakeSession:
    def __init__(self, application=None, kyc=None, decision=None, offer=None):
        self.application = application
        self.kyc = kyc
        self.decision = decision
        self.offer = offer

    def get(self, model, key):
        name = getattr(model, "__name__", "")
        if name == "Application":
            return self.application
        if name == "Decision":
            return self.decision
        return None

    def scalar(self, statement):
        # The two ordered lookups in the derivation: the latest KYC check and
        # the latest offer. Told apart by the entity the statement selects.
        text = str(statement).lower()
        if "kyc" in text:
            return self.kyc
        if "offers" in text:
            return self.offer
        return None


def _get(app_id=7307, headers=STAFF, session=None, loans=(), manual_review=None,
         monkeypatch=None):
    session = session or _FakeSession(application=_App(app_id))

    def _fake_get_session():
        yield session

    monkeypatch.setattr(applications_router.db, "query",
                        lambda sql, params=None: [dict(id=i) for i in loans])
    monkeypatch.setattr(applications_router.decision_state, "get_manual_review",
                        lambda _app_id: manual_review)
    app.dependency_overrides[get_session] = _fake_get_session
    try:
        return TestClient(app).get(f"/applications/{app_id}/lifecycle", headers=headers)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _stages(response):
    return {s["key"]: s for s in response.json()["stages"]}


# --------------------------------------------------------------------- shape


def test_all_five_steps_are_returned_in_order(monkeypatch):
    body = _get(monkeypatch=monkeypatch).json()

    assert [s["key"] for s in body["stages"]] == [
        "submitted", "kyc", "decision", "offer", "boarded",
    ]
    assert body["app_id"] == 7307


# ------------------------------------------------------- unknown vs incomplete


def test_a_missing_kyc_row_is_unknown_not_failed(monkeypatch):
    """The distinction this endpoint exists to keep. No row means nobody
    checked, which is not the same as checking and not passing."""
    stage = _stages(_get(monkeypatch=monkeypatch))["kyc"]

    assert stage["state"] == "unknown"
    assert stage["label"] == "Not available"
    assert "no KYC check is recorded" in stage["detail"]


def test_a_failed_kyc_row_is_incomplete_and_names_what_failed(monkeypatch):
    """"Not verified" alone sends the reader to another panel to find out which
    of the four fields it was."""
    session = _FakeSession(application=_App(), kyc=_Kyc(dob=False, ssn=False))
    stage = _stages(_get(session=session, monkeypatch=monkeypatch))["kyc"]

    assert stage["state"] == "incomplete"
    assert "date of birth" in stage["detail"]
    assert "SSN" in stage["detail"]
    assert "name" not in stage["detail"]


def test_a_fully_verified_kyc_row_is_complete(monkeypatch):
    session = _FakeSession(application=_App(), kyc=_Kyc())
    stage = _stages(_get(session=session, monkeypatch=monkeypatch))["kyc"]

    assert stage["state"] == "complete"
    assert stage["label"] == "Verified"


def test_a_missing_decision_is_unknown(monkeypatch):
    stage = _stages(_get(monkeypatch=monkeypatch))["decision"]

    assert stage["state"] == "unknown"
    assert "no decision is recorded" in stage["detail"]


def test_a_refer_decision_is_incomplete_rather_than_unknown(monkeypatch):
    """`refer` is a real recorded answer: the system decided that a human must
    look. Reporting it as unknown would lose the decision that was made."""
    session = _FakeSession(application=_App(), decision=_Decision("refer"))
    stage = _stages(_get(session=session, monkeypatch=monkeypatch))["decision"]

    assert stage["state"] == "incomplete"
    assert stage["label"] == "REFER"


def test_an_approved_decision_is_complete(monkeypatch):
    session = _FakeSession(application=_App(), decision=_Decision("approve"))
    stage = _stages(_get(session=session, monkeypatch=monkeypatch))["decision"]

    assert stage["state"] == "complete"
    assert stage["label"] == "APPROVE"


def test_a_staff_resolved_decision_says_it_is_final(monkeypatch):
    session = _FakeSession(application=_App(), decision=_Decision("approve"))
    stage = _stages(_get(session=session, monkeypatch=monkeypatch,
                         manual_review={"reason": "verified income"}))["decision"]

    assert stage["state"] == "complete"
    assert "final" in (stage["detail"] or "")


def test_a_missing_offer_is_unknown(monkeypatch):
    stage = _stages(_get(monkeypatch=monkeypatch))["offer"]

    assert stage["state"] == "unknown"
    assert "no offer has been created" in stage["detail"]


def test_an_unaccepted_offer_is_incomplete(monkeypatch):
    """Created and accepted are two stored facts, and neither is inferred from
    the other."""
    session = _FakeSession(application=_App(), offer=_Offer(accepted_at=None))
    stage = _stages(_get(session=session, monkeypatch=monkeypatch))["offer"]

    assert stage["state"] == "incomplete"
    assert stage["label"] == "Issued, not accepted"


def test_an_accepted_offer_is_complete_and_dated(monkeypatch):
    session = _FakeSession(application=_App(), offer=_Offer(accepted_at=WHEN))
    stage = _stages(_get(session=session, monkeypatch=monkeypatch))["offer"]

    assert stage["state"] == "complete"
    assert stage["label"] == "Accepted"
    assert stage["detail"].startswith("2026-08-28")


# ------------------------------------------------------------------- boarded


def test_the_boarded_loan_id_comes_from_the_database(monkeypatch):
    """The hole this closes. The page kept the id in React state, so a reload
    lost it and an already-boarded application showed no id and no link -- while
    `loans.app_id` had held the answer the whole time."""
    stage = _stages(_get(loans=(7307,), monkeypatch=monkeypatch))["boarded"]

    assert stage["state"] == "complete"
    assert stage["loan_id"] == 7307
    assert stage["label"] == "Loan #7307"


def test_an_unboarded_application_says_so_and_carries_no_loan_id(monkeypatch):
    stage = _stages(_get(loans=(), monkeypatch=monkeypatch))["boarded"]

    assert stage["state"] == "incomplete"
    assert stage["loan_id"] is None
    assert "no serviced loan references this application" in stage["detail"]


# ------------------------------------------------------- no step infers another


def test_a_boarded_loan_does_not_imply_kyc_or_a_decision(monkeypatch):
    """The property that makes this a record rather than a narrative.

    An application with a loan but no KYC row and no decision row is a strange
    state, and a screen that quietly filled the gaps would hide exactly the case
    worth seeing.
    """
    stages = _stages(_get(loans=(7307,), monkeypatch=monkeypatch))

    assert stages["boarded"]["state"] == "complete"
    assert stages["kyc"]["state"] == "unknown"
    assert stages["decision"]["state"] == "unknown"


def test_an_accepted_offer_does_not_imply_an_approval(monkeypatch):
    session = _FakeSession(application=_App(), offer=_Offer(accepted_at=WHEN))
    stages = _stages(_get(session=session, monkeypatch=monkeypatch))

    assert stages["offer"]["state"] == "complete"
    assert stages["decision"]["state"] == "unknown"


# ----------------------------------------------------------------------- gate


def test_a_borrower_cannot_read_the_lifecycle(monkeypatch):
    """Staff only, and not decoration: `GET /applications/{id}` is reachable
    anonymously through the gateway, and this response carries the boarded LOAN
    ID. On the detail response it would hand a loan id, and the fact of funding,
    to anyone who guessed an application id."""
    response = _get(headers=dict(STAFF, **{"X-User-Role": "borrower"}),
                    loans=(7307,), monkeypatch=monkeypatch)

    assert response.status_code == 403


def test_a_claimed_staff_role_without_the_internal_token_is_refused(monkeypatch):
    response = _get(headers={"X-User-Role": "underwriter"}, loans=(7307,),
                    monkeypatch=monkeypatch)

    assert response.status_code == 403


def test_an_anonymous_caller_is_refused(monkeypatch):
    response = _get(headers={}, loans=(7307,), monkeypatch=monkeypatch)

    assert response.status_code == 403


def test_an_application_that_does_not_exist_is_a_404(monkeypatch):
    session = _FakeSession(application=None)
    response = _get(session=session, monkeypatch=monkeypatch)

    assert response.status_code == 404
