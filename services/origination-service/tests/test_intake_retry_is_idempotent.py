"""A KYC failure must not cost the applicant a second borrower record.

The defect: `submit_application` commits the applicant and application rows and
then calls kyc-service. On a 401/403/503 it raised a bare 503 -- no identifier, no
token -- and told the caller to retry. The only thing a client could do with that
is POST again, which created a SECOND applicant and a SECOND application and left
the first stranded as `kyc_unverified` forever. One person, two borrower records,
on a system whose whole job is to be able to say who applied.

Why the rows are kept rather than rolled back: an application is the record that
somebody applied, and Reg B requires retaining application records -- **including
incomplete ones** -- for about 25 months (policies/underwriting_guidelines.md,
Records retention). Deleting them to tidy up a failed KYC call destroys exactly
the evidence the regulation asks for. So the row stays, and the retry is made safe.

Against real PostgreSQL, because the guarantee is a partial unique index and a
transaction boundary. Neither exists in a mock: an ON CONFLICT that matches no
constraint and a rollback that never happens both look like success from Python.
"""
import os
import pathlib
import uuid

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[3]
SCHEMA = "intake_retry_test"
INIT = REPO / "db" / "init"
INIT_FILES = ("001_schema.sql",)


@pytest.fixture
def db(monkeypatch):
    from app import config, database
    from app import db as app_db

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    for name in INIT_FILES:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute((INIT / name).read_text(encoding="utf-8"))

    scoped = f"{DATABASE_URL}?options=-csearch_path%3D{SCHEMA}"
    monkeypatch.setattr(app_db, "DATABASE_URL", scoped, raising=False)
    monkeypatch.setattr(app_db, "_conn", None, raising=False)
    monkeypatch.setattr(config, "DATABASE_URL", scoped, raising=False)
    monkeypatch.setattr(database, "DATABASE_URL", scoped, raising=False)
    monkeypatch.setattr(database, "_engine", None, raising=False)
    monkeypatch.setattr(database, "_Session", None, raising=False)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()
    monkeypatch.setattr(app_db, "_conn", None, raising=False)


def _count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def _payload(key):
    return {
        "idempotency_key": key,
        "name": "Robin Fictional", "dob": "1985-02-11", "ssn": "999-00-0042",
        "address": "1 Test Street", "zip_code": "99301",
        "email": "robin@example.test", "phone": "5550101234",
        "amount": 9000, "term_months": 24, "income": 60000,
    }


# --- the acceptance criterion ------------------------------------------------

def test_a_retry_after_a_kyc_failure_creates_exactly_one_applicant_and_application(db):
    """The defect, and the fix, in one test.

    kyc-service is down for both attempts. The client retries with the same key,
    exactly as the 503 tells it to. Before this change that produced two of
    everything.
    """
    from app import intake
    from app.routers import applications as router

    key = f"retry-{uuid.uuid4()}"

    def _kyc_is_down(*a, **kw):
        raise RuntimeError("connection refused")

    import app.clients as clients
    original = clients.post
    clients.post = _kyc_is_down
    try:
        first = intake.create_application(_payload(key))
        second = intake.create_application(_payload(key))
    finally:
        clients.post = original

    assert _count(db, "applicants") == 1, (
        "the retry created a second applicant -- one person, two borrower records"
    )
    assert _count(db, "applications") == 1, "the retry created a second application"
    assert first[0] == second[0], "the retry did not resume the same application"


def test_the_retry_returns_a_usable_authorization_handle(db):
    """Not a bare app_id.

    `app_id` is a guessable sequential integer and proves nothing -- `run_decision`
    requires the token precisely because of that. A resume handle that is only an
    id is not a handle.
    """
    from app import decision_state, intake

    key = f"handle-{uuid.uuid4()}"
    app_id, first_token = intake.create_application(_payload(key))
    resumed_id, second_token = intake.create_application(_payload(key))

    assert resumed_id == app_id
    assert second_token and second_token != first_token, (
        "the resume returned no fresh token, or reused one whose raw value is "
        "unrecoverable from the hash"
    )

    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT access_token_hash FROM applications WHERE id = %s", (app_id,))
        stored = cur.fetchone()["access_token_hash"]

    assert stored == decision_state.hash_access_token(second_token), (
        "the handed-back token does not authorise the application it resumed"
    )
    assert stored != decision_state.hash_access_token(first_token), (
        "the first token still works -- a resumed intake should leave one live "
        "handle, not two"
    )


def test_a_request_without_a_key_still_creates_a_new_application(db):
    """Backwards compatible. The column is nullable and the index partial, so a
    client that has not been updated is unaffected -- requiring a key on the
    intake path would be a flag day in the worst possible place."""
    from app import intake

    payload = _payload(None)
    payload.pop("idempotency_key")
    intake.create_application(payload)
    intake.create_application(payload)

    assert _count(db, "applications") == 2
    assert _count(db, "applicants") == 2


def test_a_different_key_is_a_different_application(db):
    """Guards the guard: an implementation that deduplicated everything would
    pass the first test and be catastrophically wrong."""
    from app import intake

    intake.create_application(_payload(f"a-{uuid.uuid4()}"))
    intake.create_application(_payload(f"b-{uuid.uuid4()}"))

    assert _count(db, "applications") == 2
    assert _count(db, "applicants") == 2


def test_a_lost_race_leaves_no_orphan_applicant(db):
    """The subtle half.

    ON CONFLICT DO NOTHING does not raise -- it returns no row and the transaction
    would COMMIT, leaving an applicant whose application was refused. Detecting
    the conflict is not enough; the insert that preceded it has to be undone.

    Simulated by inserting the winning row first, so the next call's application
    insert conflicts exactly as the losing side of a race would.
    """
    from app import intake

    key = f"race-{uuid.uuid4()}"
    intake.create_application(_payload(key))
    applicants_after_first = _count(db, "applicants")

    # A second call whose resume lookup is forced to miss, so it reaches the
    # INSERT and loses on the unique index -- the race, deterministically.
    real_resume = intake.resume_application
    calls = {"n": 0}

    def _miss_once(k):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_resume(k)

    intake.resume_application = _miss_once
    try:
        app_id, token = intake.create_application(_payload(key))
    finally:
        intake.resume_application = real_resume

    assert _count(db, "applicants") == applicants_after_first, (
        "the losing side of the race committed an applicant with no application"
    )
    assert _count(db, "applications") == 1
    assert app_id and token, "the loser did not resume the winner's application"


# --- the incomplete application must not advance -----------------------------

def test_an_unverified_application_cannot_be_decided(db):
    """It has no passing CIP row, so the decision gate refuses it. Asserted here
    as well as in the gate's own tests, because the retry contract is what makes
    this application reachable at all."""
    from fastapi import HTTPException

    from app import intake
    from app.routers import applications as router

    app_id, _ = intake.create_application(_payload(f"undecidable-{uuid.uuid4()}"))

    with pytest.raises(HTTPException) as excinfo:
        router._require_persisted_kyc(app_id)

    assert excinfo.value.status_code == 409
    assert "identity verification" in str(excinfo.value.detail).lower()
