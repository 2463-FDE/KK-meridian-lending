"""PR #6 review, Gap C -- application intake must not log applicant PII.

intake.create_application used to open with

    log.info("POST /applications intake req=%s", payload)  # full PII in the log

which put the applicant's SSN, date of birth, home address, phone number and
email address into an ordinary application log on every single submission.

The fix is deliberately NOT "redact the payload before logging it" -- not
logging the request body at all is the stronger guarantee, and there is no
operational need for it: the log line carries app_id and applicant_id, which is
what an operator actually correlates on. The business record still persists
every field it legitimately needs, which these tests also assert; ordinary
application logs and regulated audit records are different concerns and only
the former is being emptied here.

Canary approach: distinct, unmistakable values for each PII field, then assert
none of them appears anywhere in captured log output while the row itself is
written correctly.
"""
import contextlib
import logging

import pytest

from app import intake

# Distinct canaries -- no two share a substring, so a hit is unambiguous.
_CANARY = {
    "name": "Zephyrine Quillfeather",
    "ssn": "123456782",
    "dob": "1971-03-02",
    "email": "zq-canary@example-canary.test",
    "phone": "5550101999",
    "address": "9931 Canary Hollow Terrace, Nowhere, ZZ",
    "zip_code": "99301",
}


class _FakeDb:
    """Captures what intake writes without needing Postgres.

    Both inserts moved into `db.transaction()` when intake became idempotent
    (db/migrations/0036): the applicant and the application have to land or fail
    together, or a retry that loses the race leaves an orphan applicant. So this
    fake models the cursor as well as `query` -- stubbing only `query` let the
    real database be hit, which is why these tests started failing against live
    data rather than against the fake.
    """

    def __init__(self):
        self.applicant_params = None
        self.application_params = None
        self.resumed = None

    # --- the transactional path (both inserts) --------------------------------
    @contextlib.contextmanager
    def transaction(self):
        outer = self

        class _Cur:
            def execute(self, sql, params=None):
                if "INSERT INTO applicants" in sql:
                    outer.applicant_params = params
                    self._last = [{"id": 4242}]
                elif "INSERT INTO applications" in sql:
                    outer.application_params = params
                    self._last = [{"id": 8484}]
                else:
                    self._last = []

            def fetchall(self):
                return self._last

        yield _Cur()

    # --- the non-transactional reads (resume lookup, applicant_id) ------------
    def query(self, sql, params=None):
        if "FROM applications WHERE idempotency_key" in " ".join(sql.split()):
            self.resumed = params
            return []                       # unused key: a fresh application
        return []


@pytest.fixture
def fake_db(monkeypatch):
    fake = _FakeDb()
    monkeypatch.setattr(intake.db, "query", fake.query)
    # Both, since the inserts moved into a transaction. Patching `query` alone
    # left `transaction` pointing at the real database, so these tests ran
    # against live data and asserted a stubbed app_id against a real sequence.
    monkeypatch.setattr(intake.db, "transaction", fake.transaction)
    return fake


def test_intake_logs_no_applicant_pii(fake_db, caplog):
    caplog.set_level(logging.DEBUG)

    app_id, raw_token, _resume = intake.create_application(dict(_CANARY, amount=9000,
                                                       term_months=24, income=100000))

    assert app_id == 8484
    logged = caplog.text
    for field, value in _CANARY.items():
        assert value not in logged, f"{field} canary leaked into the log: {value!r}"
    # The freshly minted submission token is a bearer credential too (Gap B).
    assert raw_token not in logged, "the submission token must never be logged"


def test_intake_still_logs_useful_identifiers(fake_db, caplog):
    """Emptying the log line must not make it useless -- an operator still
    needs to correlate the request to a row."""
    caplog.set_level(logging.INFO)

    intake.create_application(dict(_CANARY, amount=9000, term_months=24, income=100000))

    assert "app_id=8484" in caplog.text
    assert "applicant_id=4242" in caplog.text


def test_intake_still_persists_every_business_field(fake_db):
    """The business record is a different concern from the application log --
    removing PII from logs must not remove it from the row."""
    intake.create_application(dict(_CANARY, amount=9000, term_months=24, income=100000))

    applicant = fake_db.applicant_params
    assert _CANARY["name"] in applicant
    assert _CANARY["ssn"] in applicant
    assert _CANARY["dob"] in applicant
    assert _CANARY["email"] in applicant
    assert _CANARY["phone"] in applicant
    assert _CANARY["address"] in applicant
    assert _CANARY["zip_code"] in applicant

    application = fake_db.application_params
    assert 9000 in application and 24 in application and 100000 in application


def test_intake_persists_only_the_token_hash_not_the_raw_value(fake_db):
    """Gap B cross-check at the same boundary."""
    from app import decision_state

    _, raw, _resume = intake.create_application(dict(_CANARY, amount=9000, term_months=24, income=100000))

    params = [str(p) for p in fake_db.application_params]
    assert raw not in params, "the raw submission token must never be persisted"
    assert decision_state.hash_access_token(raw) in params
