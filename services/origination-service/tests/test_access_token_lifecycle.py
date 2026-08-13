"""PR #6 review, Gap B -- the SUBMISSION token's security lifecycle.

The token minted at submission (ApplicationCreated.access_token) proves
ownership on the FIRST decision call, for a borrower who has no account yet.
It used to be stored in plain text, never expire, never be consumed, and be
compared with a plain `==`. Migration 0025 and decision_state's
issue/verify/consume helpers give it the same lifecycle the acceptance token
got in 0022.

Runs against real PostgreSQL: the point of most of these cases is what is (and
is not) in the applications row, which a mocked cursor cannot demonstrate.
"""
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from fastapi.testclient import TestClient

from app import clients, db, decision_state, disclosure_graph, intake
from app.main import app

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
SCHEMA = "origination_access_token_test"
client = TestClient(app)

_RAW = "borrower-submission-token-abc123"


@pytest.fixture
def real_db(monkeypatch):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(f"""
            SET search_path TO {SCHEMA};
            CREATE TABLE applicants (
                id SERIAL PRIMARY KEY, name TEXT NOT NULL, dob DATE, ssn TEXT,
                ein TEXT, is_entity BOOLEAN DEFAULT FALSE, email TEXT,
                phone TEXT, address TEXT, zip_code TEXT
            );
            CREATE TABLE applications (
                id SERIAL PRIMARY KEY,
                applicant_id INTEGER REFERENCES applicants(id),
                amount NUMERIC(14,2) NOT NULL, term_months INTEGER NOT NULL,
                purpose TEXT, income NUMERIC(14,2), employer TEXT,
                job_title TEXT, employment_years DOUBLE PRECISION,
                status TEXT DEFAULT 'submitted',
                access_token_hash TEXT,
                access_token_expires_at TIMESTAMPTZ,
                access_token_consumed_at TIMESTAMPTZ,
                accept_token_hash TEXT,
                accept_token_expires_at TIMESTAMPTZ,
                accept_token_consumed_at TIMESTAMPTZ,
                idempotency_key TEXT,
                resume_token_hash TEXT,
                resume_token_expires_at TIMESTAMPTZ,
                resume_token_consumed_at TIMESTAMPTZ,
                -- db/migrations/0038. This block is a hand-written copy of
                -- db/init/001_schema.sql and drifts every time that file gains
                -- a column: the write path then fails here with UndefinedColumn
                -- while the real schema is fine. Kept in step rather than
                -- rebuilt from db/init, which is a larger change than this PR
                -- should carry -- but the duplication is the reason this test
                -- broke, not the migration.
                request_fingerprint TEXT,
                -- db/migrations/0039. Same hand-written-copy problem as the
                -- line above: this block duplicates db/init/001_schema.sql and
                -- has to be kept in step by hand, which is why adding a column
                -- to the real schema breaks these tests rather than the code.
                prev_access_token_hash TEXT,
                prev_access_token_expires_at TIMESTAMPTZ
            );
            CREATE UNIQUE INDEX applications_idempotency_key_uniq
                ON applications (idempotency_key) WHERE idempotency_key IS NOT NULL;
            CREATE TABLE kyc_checks (
                id SERIAL PRIMARY KEY,
                applicant_id INTEGER REFERENCES applicants(id),
                application_id INTEGER REFERENCES applications(id),
                name_verified BOOLEAN, dob_verified BOOLEAN,
                address_verified BOOLEAN, ssn_verified BOOLEAN,
                cip_passed BOOLEAN,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE decisions (
                app_id INTEGER PRIMARY KEY REFERENCES applications(id), outcome TEXT NOT NULL
            );
            -- PR #8: an approval now reports DecisionOut.offer_ready, which
            -- means run_decision reads offers on the approve path.
            CREATE TABLE offers (
                id SERIAL PRIMARY KEY,
                app_id INTEGER REFERENCES applications(id) UNIQUE,
                decision_id INTEGER REFERENCES decisions(app_id) UNIQUE,
                fee_pct_used NUMERIC(5,4),
                -- note_rate_pct is canonical: _complete_offer_exists() queries it,
                -- so a fixture omitting it fails with UndefinedColumn.
                note_rate_pct NUMERIC(7,3),
                apr NUMERIC(7,3), finance_charge NUMERIC(14,2),
                monthly_payment NUMERIC(14,2), amount_financed NUMERIC(14,2),
                total_of_payments NUMERIC(14,2),
                -- Model B schedule facts (db/migrations/0030). Boarding requires
                -- these, so a fixture omitting them cannot board.
                regular_payment_count INTEGER,
                final_payment NUMERIC(14,2),
                term_months INTEGER,
                schedule_version TEXT,
            principal NUMERIC(14,2),
                accepted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE manual_reviews (
                id SERIAL PRIMARY KEY,
                app_id INTEGER NOT NULL REFERENCES applications(id) UNIQUE,
                reviewer_role TEXT NOT NULL, reviewer_name TEXT,
                outcome TEXT NOT NULL, reason TEXT NOT NULL,
                reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE decision_events (
                id SERIAL PRIMARY KEY,
                app_id INTEGER NOT NULL REFERENCES applications(id),
                requested_amount NUMERIC(14,2), term_months INTEGER,
                annual_income NUMERIC(14,2), bureau_score INTEGER,
                model_score DOUBLE PRECISION, model_version TEXT NOT NULL,
                top_features JSONB, decision TEXT NOT NULL,
                reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb, attempt_id INTEGER
            );
        """)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute((MIGRATIONS_DIR / "0023_decision_attempts.sql").read_text())
        cur.execute((MIGRATIONS_DIR / "0024_decision_attempt_bureau_key.sql").read_text())
    conn.commit()

    schema_url = DATABASE_URL + ("&" if "?" in DATABASE_URL else "?") + f"options=-csearch_path%3D{SCHEMA}"
    monkeypatch.setattr(db, "DATABASE_URL", schema_url)
    monkeypatch.setattr(db, "_conn", None)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.commit()
    conn.close()


def _seed(conn, app_id, raw=_RAW, expires="now() + interval '1 hour'", consumed=None):
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("INSERT INTO applicants (id, name, ssn) VALUES (%s, %s, %s)",
                  (app_id, "Jane Borrower", "123456782"))
        c.execute(
            f"INSERT INTO applications (id, applicant_id, amount, term_months, income, "
            f"access_token_hash, access_token_expires_at, access_token_consumed_at) "
            f"VALUES (%s, %s, 9000, 24, 100000, %s, {expires}, %s)",
            (app_id, app_id, decision_state.hash_access_token(raw) if raw else None, consumed),
        )
        # The decision gate refuses an application with no persisted KYC result
        # for THAT application (PR #18 + db/migrations/0032). These fixtures are
        # about the access token, not identity verification, so each seeds one --
        # after the applications row exists, since application_id is a real FK.
        c.execute(
            # cip_passed, not just the factors: the decision gate reads the
            # VERDICT now (db/migrations/0033), because a row that merely
            # existed used to satisfy it even when CIP had failed.
            "INSERT INTO kyc_checks (applicant_id, application_id, name_verified, "
            "cip_passed) VALUES (%s, %s, true, true)",
            (app_id, app_id),
        )
    conn.commit()


@pytest.fixture
def approve_stub(monkeypatch):
    monkeypatch.setattr(disclosure_graph, "auto_generate_offer", lambda app_id: None)

    def _fake_post(base_url, path, payload, headers=None):
        return {
            "outcome": "approve", "score": 700, "reason": None,
            "attempt_id": payload["attempt_id"],
            "bureau_score": 680, "bureau_reference_id": "stub-ref",
            "model_version": "v1-stub", "top_features": None, "reason_codes": [],
        }

    monkeypatch.setattr(clients, "post", _fake_post)


def _decide(app_id, token):
    return client.post(f"/applications/{app_id}/decision", json={"access_token": token})


# --- the six credential states ------------------------------------------------

def test_valid_token_is_accepted(real_db, approve_stub):
    _seed(real_db, 601)
    assert _decide(601, _RAW).status_code == 200


def test_wrong_token_is_rejected(real_db, approve_stub):
    _seed(real_db, 602)
    resp = _decide(602, "attacker-guessed-value")
    assert resp.status_code == 403


def test_expired_token_is_rejected(real_db, approve_stub):
    """Expiry is Postgres's own clock, not the app host's."""
    _seed(real_db, 603, expires="now() - interval '1 second'")
    assert _decide(603, _RAW).status_code == 403


def test_consumed_token_is_rejected(real_db, approve_stub):
    _seed(real_db, 604, consumed="2026-01-01T00:00:00+00:00")
    assert _decide(604, _RAW).status_code == 403


def test_revoked_token_is_rejected(real_db, approve_stub):
    """A NULL hash (never issued, or cleared) can never match."""
    _seed(real_db, 605, raw=None)
    assert _decide(605, _RAW).status_code == 403


def test_a_token_from_another_application_is_rejected(real_db, approve_stub):
    """Cross-application replay: a real, live token for app 606 must not
    authorise a decision on app 607."""
    _seed(real_db, 606, raw=_RAW)
    _seed(real_db, 607, raw="a-completely-different-applications-token")
    assert _decide(607, _RAW).status_code == 403


# --- single use ---------------------------------------------------------------

def test_token_is_consumed_by_the_decision_it_authorises(real_db, approve_stub):
    """Concurrent/repeated use succeeds exactly once."""
    _seed(real_db, 608)
    assert _decide(608, _RAW).status_code == 200

    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT access_token_consumed_at FROM applications WHERE id = 608")
        assert cur.fetchone()["access_token_consumed_at"] is not None

    # Second use of the same raw token: rejected. (A staff rerun is a separate,
    # staff-authenticated path -- this is the borrower credential only.)
    assert _decide(608, _RAW).status_code == 403


def test_token_is_not_consumed_when_the_decision_does_not_persist(real_db, monkeypatch):
    """Consumption happens inside TXN B, so a decision that never commits
    leaves the token usable -- otherwise the Gap A ambiguous-timeout retry
    would be locked out of a decision the borrower never received."""
    import httpx
    _seed(real_db, 609)
    monkeypatch.setattr(disclosure_graph, "auto_generate_offer", lambda app_id: None)

    def _timeout(base_url, path, payload, headers=None):
        raise httpx.ReadTimeout("simulated ambiguous timeout")

    monkeypatch.setattr(clients, "post", _timeout)
    assert _decide(609, _RAW).status_code == 502

    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT access_token_consumed_at FROM applications WHERE id = 609")
        assert cur.fetchone()["access_token_consumed_at"] is None, (
            "a decision that never persisted must not burn the borrower's token"
        )


# --- the plaintext value must not exist anywhere ------------------------------

def test_plaintext_token_is_never_stored_in_the_database(real_db, monkeypatch):
    """intake mints the token; only its hash may reach the row."""
    monkeypatch.setattr(db, "query", db.query)  # real db, schema-scoped by the fixture
    with real_db.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("INSERT INTO applicants (id, name) VALUES (900, 'Seed')")
    real_db.commit()

    app_id, raw, _resume = intake.create_application({
        "name": "Jane Borrower", "ssn": "123456782", "dob": "1990-01-01",
        "email": "j@example.com", "phone": "5551234567", "address": "1 Main St",
        "amount": 9000, "term_months": 24, "income": 100000,
    })

    assert raw, "the raw token must be returned to the caller exactly once"
    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT * FROM applications WHERE id = %s", (app_id,))
        row = dict(cur.fetchone())

    assert "access_token" not in row, "the plaintext column must no longer exist"
    assert row["access_token_hash"] == decision_state.hash_access_token(raw)
    assert row["access_token_expires_at"] is not None
    assert row["access_token_consumed_at"] is None
    # The raw value must not appear in ANY column of the row.
    assert raw not in " ".join(str(v) for v in row.values())


def test_plaintext_token_is_absent_from_logs_and_error_responses(real_db, approve_stub, caplog):
    import logging
    _seed(real_db, 610)
    caplog.set_level(logging.DEBUG)

    ok = _decide(610, _RAW)
    bad = _decide(610, _RAW)          # now consumed -> rejected

    assert ok.status_code == 200
    assert bad.status_code == 403
    assert _RAW not in ok.text, "the submission token must not be echoed back"
    assert _RAW not in bad.text, "the rejected token must not be echoed in the error"
    assert _RAW not in caplog.text, "the submission token must never be logged"
