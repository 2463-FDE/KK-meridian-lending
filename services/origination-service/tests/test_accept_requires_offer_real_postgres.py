"""PR #8 review -- an approved application with no offer row must not be
acceptable, proven against real rows.

The reviewer's concern was that `run_decision` swallows an `auto_generate_offer`
failure but still mints an `accept_token`, and that the accept path used to fall
back to a hardcoded 7.99% APR when no offer existed -- funding a borrower with
no TILA disclosure on record. The APR fallback is gone (PR #6, Gap F), and this
file is the proof that nothing else re-opens the hole.

There is already a test asserting the 409, but it mocks `db.query` to return a
canned row and patches `intake.board_to_servicing` -- so it proves the
pre-check's message and nothing about whether a loan could still be created. The
tests here use real Postgres and assert on `loans`/`balances` directly, which is
the only thing that actually answers "was the borrower funded".

Note on the token: `accept_token` is deliberately still issued when disclosure
generation fails. It is the borrower's only credential, and `POST /los/offer`
authenticates with that same token, so withholding it would strand them with an
approved application they can never complete. The funding gate is the offer
check enforced below; `DecisionOut.offer_ready` is what tells the caller the
disclosure is missing instead of leaving them to discover it at accept time.
"""
import os

import psycopg2
import psycopg2.extras
import pytest
from fastapi.testclient import TestClient

from app import db, decision_state, intake
from app.main import app
from .test_decision_attempt_real_postgres import _full_schema_sql, SCHEMA

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

client = TestClient(app)

_COMPLETE_TERMS = {
    "apr": 5.946, "finance_charge": 768.11, "monthly_payment": 407.0,
    "amount_financed": 8730.0, "total_of_payments": 9768.11,
}


@pytest.fixture
def real_db(monkeypatch):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(_full_schema_sql())
    monkeypatch.setattr(db, "_conn", conn, raising=False)
    monkeypatch.setattr(
        db, "DATABASE_URL", f"{DATABASE_URL}?options=-csearch_path%3D{SCHEMA}", raising=False
    )
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def _sql(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _approved_application(conn, *, with_offer: bool):
    """An application that has been approved but whose disclosure may or may not
    have been generated -- exactly the state a failed auto_generate_offer
    leaves behind."""
    _sql(conn, "INSERT INTO applicants (id, name, ssn) VALUES (1, 'Robin Fictional', '999-00-0001')")
    _sql(conn, "INSERT INTO applications (id, applicant_id, amount, term_months, income, status) "
               "VALUES (1, 1, 9000, 24, 60000, 'approved')")
    _sql(conn, "INSERT INTO decisions (app_id, outcome) VALUES (1, 'approve')")

    raw_token = None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        raw_token = decision_state.issue_accept_token(cur, 1)

    if with_offer:
        _sql(conn,
             "INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, finance_charge, "
             "monthly_payment, amount_financed, total_of_payments) "
             "VALUES (1, 1, 0.030, %s, %s, %s, %s, %s)",
             tuple(_COMPLETE_TERMS[k] for k in
                   ("apr", "finance_charge", "monthly_payment", "amount_financed", "total_of_payments")))
    return raw_token


def _accept(token):
    return client.post("/applications/1/accept", headers={"X-Offer-Accept-Token": token})


def test_approved_application_with_no_offer_row_cannot_be_accepted(real_db, monkeypatch):
    """The reviewer's second named test. No offer -> no funding, and the proof
    is the absence of real loan and balance rows, not a stubbed spy."""
    token = _approved_application(real_db, with_offer=False)
    # Not patched: if boarding were somehow reached, it would really try to run
    # and the row assertions below would catch it.
    resp = _accept(token)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Create an offer before boarding this application."

    assert _sql(real_db, "SELECT count(*)::int AS n FROM loans")[0]["n"] == 0
    assert _sql(real_db, "SELECT count(*)::int AS n FROM balances")[0]["n"] == 0
    assert _sql(real_db, "SELECT status FROM applications WHERE id = 1")[0]["status"] != "funded"


@pytest.mark.parametrize("missing", list(_COMPLETE_TERMS))
def test_an_offer_missing_any_canonical_term_cannot_be_accepted(real_db, missing):
    """A partially written offers row is not a disclosure either. One case per
    term, so no single field can quietly regain a default."""
    token = _approved_application(real_db, with_offer=True)
    _sql(real_db, f"UPDATE offers SET {missing} = NULL WHERE app_id = 1")

    resp = _accept(token)

    assert resp.status_code == 409, f"a NULL {missing} was accepted"
    assert _sql(real_db, "SELECT count(*)::int AS n FROM loans")[0]["n"] == 0
    assert _sql(real_db, "SELECT count(*)::int AS n FROM balances")[0]["n"] == 0


def test_the_refusal_is_recoverable_once_the_offer_exists(real_db):
    """The refusal must be a "not yet", not a dead end -- the same token that was
    just refused works once the disclosure is generated. This is why the token is
    still issued when auto-generation fails."""
    token = _approved_application(real_db, with_offer=False)
    assert _accept(token).status_code == 409

    _sql(real_db,
         "INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, finance_charge, "
         "monthly_payment, amount_financed, total_of_payments) "
         "VALUES (1, 1, 0.030, %s, %s, %s, %s, %s)",
         tuple(_COMPLETE_TERMS[k] for k in
               ("apr", "finance_charge", "monthly_payment", "amount_financed", "total_of_payments")))

    resp = _accept(token)
    assert resp.status_code == 200, resp.text

    loans = _sql(real_db, "SELECT id, apr FROM loans WHERE app_id = 1")
    assert len(loans) == 1
    # Never the old hardcoded 7.99 -- the boarded rate is the disclosed rate.
    assert float(loans[0]["apr"]) == pytest.approx(_COMPLETE_TERMS["apr"], abs=1e-3)
    assert _sql(real_db, "SELECT count(*)::int AS n FROM balances WHERE loan_id = %s",
                (loans[0]["id"],))[0]["n"] == 1
