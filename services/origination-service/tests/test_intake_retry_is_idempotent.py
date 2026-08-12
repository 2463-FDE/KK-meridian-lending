"""A retry must be safe, and a retry must be *authorised*. Those are two things.

**The first defect.** Intake commits the applicant and application rows and then
calls kyc-service. On a failure it returned a bare 503 -- no identifier, no token --
and said "please retry". A retry created a SECOND applicant and a SECOND
application. One person, two borrower records.

**The defect that fix introduced, which is worse.** The idempotency key became the
thing that recovered an application: present the key, receive a fresh access
token. A client-chosen key is not a secret -- it travels in request bodies, proxy
logs and client-side code, and it can be guessed. Anyone holding one could obtain
a live access token and from there request a decision, read the application and
trigger a credit pull. **Application takeover, through the path added to make
retries safe.**

The reasoning was written into the docstring: *"it is safe because the caller has
just proved it owns this application by presenting the key that created it."*
Presenting an identifier is not proof of ownership.

So the contract these tests pin:

- the **key** says *which* application a retry belongs to;
- the **resume token** -- server-generated, 32 bytes of `secrets`, stored only as
  a hash -- says the caller may recover it;
- **both** are required, and failure is indistinguishable across missing, wrong,
  expired and replayed;
- the token is **rotated** on success, so a captured one cannot be replayed.

Against real PostgreSQL: the guarantees are a partial unique index, a transaction
boundary and a constant-time hash comparison. None of that is observable in a mock.
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


@pytest.fixture
def db(monkeypatch):
    from app import config, database
    from app import db as app_db

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute((INIT / "001_schema.sql").read_text(encoding="utf-8"))

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


def _row(conn, app_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT * FROM applications WHERE id = %s", (app_id,))
        return cur.fetchone()


def _payload(key):
    return {
        "idempotency_key": key,
        "name": "Robin Fictional", "dob": "1985-02-11", "ssn": "999-00-0042",
        "address": "1 Test Street", "zip_code": "99301",
        "email": "robin@example.test", "phone": "5550101234",
        "amount": 9000, "term_months": 24, "income": 60000,
    }


# --- the original guarantee still holds --------------------------------------

def test_a_retry_with_the_token_creates_exactly_one_applicant_and_application(db):
    from app import intake

    key = f"retry-{uuid.uuid4()}"
    app_id, _, resume = intake.create_application(_payload(key))
    again_id, _, resume2 = intake.create_application(_payload(key), resume_token=resume)

    assert again_id == app_id
    assert _count(db, "applicants") == 1, "the retry created a second applicant"
    assert _count(db, "applications") == 1
    assert resume2 != resume, "the resume token was not rotated"


def test_a_request_without_a_key_still_creates_a_new_application(db):
    from app import intake

    payload = _payload(None)
    payload.pop("idempotency_key")
    intake.create_application(payload)
    intake.create_application(payload)

    assert _count(db, "applications") == 2


def test_a_different_key_is_a_different_application(db):
    """Guards the guard: deduplicating everything would pass the first test and
    be catastrophically wrong."""
    from app import intake

    intake.create_application(_payload(f"a-{uuid.uuid4()}"))
    intake.create_application(_payload(f"b-{uuid.uuid4()}"))

    assert _count(db, "applications") == 2


# --- the key alone must not authorise anything -------------------------------

def test_the_key_alone_cannot_recover_an_application(db):
    """The takeover, refused.

    This is the exact call that used to hand back a live access token to anyone
    who knew the key.
    """
    from app import intake

    key = f"takeover-{uuid.uuid4()}"
    intake.create_application(_payload(key))

    with pytest.raises(intake.ResumeNotAuthorized):
        intake.create_application(_payload(key))          # no resume token


@pytest.mark.parametrize("bad, why", [
    (None, "missing"),
    ("", "empty"),
    ("guessed-token-value", "guessed"),
    ("x" * 43, "right shape, wrong value"),
])
def test_a_wrong_or_missing_token_is_refused(db, bad, why):
    from app import intake

    key = f"wrong-{uuid.uuid4()}"
    intake.create_application(_payload(key))

    with pytest.raises(intake.ResumeNotAuthorized):
        intake.create_application(_payload(key), resume_token=bad)


def test_a_replayed_token_is_refused(db):
    """Rotation makes a captured token single-use.

    A resume token that leaked into a proxy log must stop working the moment the
    legitimate client uses it.
    """
    from app import intake

    key = f"replay-{uuid.uuid4()}"
    _, _, first = intake.create_application(_payload(key))
    intake.create_application(_payload(key), resume_token=first)     # legitimate

    with pytest.raises(intake.ResumeNotAuthorized):
        intake.create_application(_payload(key), resume_token=first)  # replay


def test_an_expired_token_is_refused(db):
    from app import intake

    key = f"expired-{uuid.uuid4()}"
    app_id, _, resume = intake.create_application(_payload(key))
    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("UPDATE applications SET resume_token_expires_at = now() - "
                    "interval '1 second' WHERE id = %s", (app_id,))

    with pytest.raises(intake.ResumeNotAuthorized):
        intake.create_application(_payload(key), resume_token=resume)


def test_a_refused_recovery_mints_no_access_token_and_changes_nothing(db):
    """The consequences the contract forbids: no access token, no data, no KYC,
    no decisioning. Asserted on the stored row, because that is what a later
    request would authenticate against."""
    from app import intake

    key = f"nochange-{uuid.uuid4()}"
    app_id, _, _ = intake.create_application(_payload(key))
    before = _row(db, app_id)

    with pytest.raises(intake.ResumeNotAuthorized):
        intake.create_application(_payload(key), resume_token="not-the-token")

    after = _row(db, app_id)
    assert after["access_token_hash"] == before["access_token_hash"], (
        "a refused recovery rotated the access token, so a wrong guess "
        "invalidates the real client's handle -- and a repeated guess is a "
        "denial of service on the applicant"
    )
    assert after["resume_token_hash"] == before["resume_token_hash"]
    assert after["status"] == before["status"]


def test_only_the_hash_of_the_resume_token_is_stored(db):
    from app import intake

    key = f"hash-{uuid.uuid4()}"
    app_id, _, resume = intake.create_application(_payload(key))
    row = _row(db, app_id)

    assert row["resume_token_hash"] and row["resume_token_hash"] != resume, (
        "the raw resume token is in the database"
    )
    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) FROM applications WHERE "
                    "resume_token_hash = %s", (resume,))
        assert cur.fetchone()[0] == 0


def test_the_failure_does_not_distinguish_its_reasons(db):
    """Missing, wrong, expired and replayed must be indistinguishable.

    Telling them apart tells an attacker which one they achieved -- and
    "expired" in particular confirms the application exists.
    """
    from app import intake

    key = f"opaque-{uuid.uuid4()}"
    app_id, _, resume = intake.create_application(_payload(key))

    errors = []
    for bad in (None, "wrong-value"):
        try:
            intake.create_application(_payload(key), resume_token=bad)
        except intake.ResumeNotAuthorized as e:
            errors.append(repr(e))

    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("UPDATE applications SET resume_token_expires_at = now() - "
                    "interval '1 second' WHERE id = %s", (app_id,))
    try:
        intake.create_application(_payload(key), resume_token=resume)
    except intake.ResumeNotAuthorized as e:
        errors.append(repr(e))

    assert len(set(errors)) == 1, f"the failures are distinguishable: {set(errors)}"


# --- concurrency --------------------------------------------------------------

def test_a_lost_race_leaves_no_orphan_applicant(db):
    """ON CONFLICT DO NOTHING does not raise -- it returns no row and the
    transaction would COMMIT, leaving an applicant whose application was refused.

    The loser cannot resume without the winner's token, so it raises
    ResumeNotAuthorized -- which is correct: two concurrent first attempts with
    the same key are indistinguishable from an attacker racing a real client, and
    the safe answer to both is "prove it".
    """
    from app import intake

    key = f"race-{uuid.uuid4()}"
    intake.create_application(_payload(key))
    applicants_after_first = _count(db, "applicants")

    real_resume = intake.resume_application
    calls = {"n": 0}

    def _miss_once(k, token=None):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_resume(k, token)

    intake.resume_application = _miss_once
    try:
        with pytest.raises(intake.ResumeNotAuthorized):
            intake.create_application(_payload(key))
    finally:
        intake.resume_application = real_resume

    assert _count(db, "applicants") == applicants_after_first, (
        "the losing side of the race committed an applicant with no application"
    )
    assert _count(db, "applications") == 1


# --- the incomplete application still cannot advance -------------------------

def test_an_unverified_application_cannot_be_decided(db):
    from fastapi import HTTPException

    from app import intake
    from app.routers import applications as router

    app_id, _, _ = intake.create_application(_payload(f"undecidable-{uuid.uuid4()}"))

    with pytest.raises(HTTPException) as excinfo:
        router._require_persisted_kyc(app_id)

    assert excinfo.value.status_code == 409
    assert "identity verification" in str(excinfo.value.detail).lower()
