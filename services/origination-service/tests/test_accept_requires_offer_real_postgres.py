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

# Internally consistent for principal 9000 at a 7.99% note rate over 24
# months: 407.00/mo, 3% prepaid fee, and an ACTUARIAL apr of 11.029 --
# deliberately different from the note rate, so a test that confuses the two
# fails instead of passing by coincidence.
_NOTE_RATE_PCT = 7.99
_COMPLETE_TERMS = {
    "apr": 11.029, "finance_charge": 768.11, "monthly_payment": 407.0,
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
             "INSERT INTO offers (app_id, decision_id, fee_pct_used, note_rate_pct, apr, "
             "finance_charge, monthly_payment, amount_financed, total_of_payments) "
             "VALUES (1, 1, 0.030, 7.990, %s, %s, %s, %s, %s)",
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
         "INSERT INTO offers (app_id, decision_id, fee_pct_used, note_rate_pct, apr, "
         "finance_charge, monthly_payment, amount_financed, total_of_payments) "
         "VALUES (1, 1, 0.030, 7.990, %s, %s, %s, %s, %s)",
         tuple(_COMPLETE_TERMS[k] for k in
               ("apr", "finance_charge", "monthly_payment", "amount_financed", "total_of_payments")))

    resp = _accept(token)
    assert resp.status_code == 200, resp.text

    loans = _sql(real_db, "SELECT id, apr FROM loans WHERE app_id = 1")
    assert len(loans) == 1
    # The boarded rate is the CONTRACTUAL note rate, not the disclosed APR --
    # servicing amortizes what it is given, so this is what the borrower is
    # billed at (PR #10 review). Asserting the disclosed APR here is precisely
    # the confusion that shipped a schedule 13.59/month above the disclosure.
    assert float(loans[0]["apr"]) == pytest.approx(_NOTE_RATE_PCT, abs=1e-3)
    assert float(loans[0]["apr"]) != pytest.approx(_COMPLETE_TERMS["apr"], abs=1e-3)
    assert _sql(real_db, "SELECT count(*)::int AS n FROM balances WHERE loan_id = %s",
                (loans[0]["id"],))[0]["n"] == 1


# --- PR #10 review: the boarded loan must bill what the disclosure promised ---

def _amortized_payment(principal: float, annual_rate_pct: float, term_months: int) -> float:
    """The payment servicing will bill, computed the way servicing computes it
    (`servicing-service/app/schedule.py` amortizes `loans.apr`). Written out
    here rather than imported, so this test does not pass just because two
    services share one helper."""
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return principal / term_months
    f = (1 + r) ** term_months
    return principal * r * f / (f - 1)


def test_the_boarded_loan_bills_the_disclosed_monthly_payment(real_db):
    """The defect PR #10's review caught, as an end-to-end assertion.

    `offers.apr` became the true actuarial APR, but accept still boarded that
    column as the servicing rate -- so servicing amortized 9.584% while the
    disclosure said 439.35/month at 7.99%. The borrower would have been billed
    452.94: 13.59 a month, 652 over the term, against them.

    This asserts the property that actually matters and would have caught it
    either way round: whatever rate reaches `loans`, amortizing it must
    reproduce the payment on the accepted disclosure.
    """
    token = _approved_application(real_db, with_offer=True)
    disclosed = _sql(real_db, "SELECT monthly_payment, apr, note_rate_pct FROM offers WHERE app_id = 1")[0]

    assert _accept(token).status_code == 200

    loan = _sql(real_db, "SELECT principal, apr, term_months FROM loans WHERE app_id = 1")[0]
    billed = _amortized_payment(float(loan["principal"]), float(loan["apr"]), loan["term_months"])

    assert billed == pytest.approx(float(disclosed["monthly_payment"]), abs=0.01), (
        f"boarded loan bills {billed:.2f}/month against a disclosure of "
        f"{float(disclosed['monthly_payment']):.2f} -- servicing is amortizing "
        f"{loan['apr']} where the contractual rate is {disclosed['note_rate_pct']}"
    )


def test_rv2_vector_boards_the_note_rate_and_bills_the_disclosed_payment(real_db):
    """RV-2 -- the second reported vector, end to end through accept and billing.

    Reported from the running UI on 2026-08-07: 15,000 at a 7.99% note rate over
    36 months disclosed an APR of 5.43% and a finance charge of 1,919.15, a box
    that did not foot (14,550.00 + 1,919.15 = 16,469.15, short of the stated
    16,919.15 by exactly the 450.00 fee).

    Correct disclosure, recomputed from the payment stream:
        note rate          7.99%          APR              10.072%
        amount financed   14,550.00       finance charge    2,369.15
        total of payments 16,919.15       monthly payment     469.98

    Acceptance criteria 7 and 8: accepting boards the 7.99% NOTE rate, and
    amortizing whatever reached `loans` reproduces the disclosed 469.98.
    """
    rv2 = {
        "note_rate_pct": 7.99, "apr": 10.072, "finance_charge": 2369.15,
        "monthly_payment": 469.98, "amount_financed": 14550.00,
        "total_of_payments": 16919.15,
    }
    token = _approved_application(real_db, with_offer=True)
    _sql(
        real_db,
        "UPDATE offers SET note_rate_pct = %s, apr = %s, finance_charge = %s, "
        "monthly_payment = %s, amount_financed = %s, total_of_payments = %s "
        "WHERE app_id = 1",
        tuple(rv2[k] for k in ("note_rate_pct", "apr", "finance_charge",
                               "monthly_payment", "amount_financed",
                               "total_of_payments")),
    )
    _sql(real_db, "UPDATE applications SET amount = 15000, term_months = 36 WHERE id = 1")

    # 6. the box foots -- the identity the reported disclosure failed
    assert rv2["amount_financed"] + rv2["finance_charge"] == pytest.approx(
        rv2["total_of_payments"], abs=0.01
    )

    assert _accept(token).status_code == 200

    loan = _sql(real_db, "SELECT principal, apr, term_months FROM loans WHERE app_id = 1")[0]

    # 7. the NOTE rate is what reached servicing, not the APR
    assert float(loan["apr"]) == pytest.approx(7.99, abs=1e-3), (
        f"boarded {loan['apr']} -- servicing must amortize the 7.99 note rate, "
        f"not the 10.072 APR"
    )
    assert float(loan["apr"]) != pytest.approx(10.072, abs=1e-3)

    # 8. servicing reproduces the disclosed payment
    billed = _amortized_payment(float(loan["principal"]), float(loan["apr"]), loan["term_months"])
    assert billed == pytest.approx(rv2["monthly_payment"], abs=0.01), (
        f"servicing would bill {billed:.2f} against a disclosed 469.98"
    )


def test_boarding_uses_the_note_rate_not_the_disclosed_apr(real_db):
    """Pins which column is boarded, so the two can never be swapped back. They
    are deliberately different values in this fixture -- if they were equal the
    test above would pass for the wrong reason."""
    token = _approved_application(real_db, with_offer=True)
    offer = _sql(real_db, "SELECT apr, note_rate_pct FROM offers WHERE app_id = 1")[0]
    assert float(offer["apr"]) != float(offer["note_rate_pct"]), "fixture must keep them distinct"

    assert _accept(token).status_code == 200

    loan = _sql(real_db, "SELECT apr FROM loans WHERE app_id = 1")[0]
    assert float(loan["apr"]) == pytest.approx(float(offer["note_rate_pct"]), abs=1e-3)
    assert float(loan["apr"]) != pytest.approx(float(offer["apr"]), abs=1e-3)


def test_an_offer_with_no_recorded_note_rate_is_refused_rather_than_guessed(real_db):
    """A pre-0030 row that escaped the back-fill has no contractual rate on
    record. Falling back to `apr` would put the borrower on terms nobody
    disclosed, so accept refuses instead."""
    token = _approved_application(real_db, with_offer=True)
    _sql(real_db, "UPDATE offers SET note_rate_pct = NULL WHERE app_id = 1")

    resp = _accept(token)

    assert resp.status_code == 409
    # note_rate_pct is canonical, so the incomplete-offer precheck now names it
    # by field before the boarding-time contractual-rate guard is reached. Either
    # message is a refusal to guess a rate; assert on the field name, which both
    # carry, rather than on which guard fired first.
    detail = resp.json()["detail"]
    assert "note_rate_pct" in detail or "contractual rate" in detail, detail
    assert _sql(real_db, "SELECT count(*)::int AS n FROM loans")[0]["n"] == 0


def test_offer_ready_is_false_when_only_the_note_rate_is_missing(real_db):
    """offer_ready must not promise what accept will refuse.

    note_rate_pct was absent from _CANONICAL_OFFER_FIELDS, so an offer with a
    complete TILA box but no contractual rate reported ready -- and then 409'd on
    accept, because accept will not infer a rate. The caller was told to proceed
    into a guaranteed failure. Both halves are asserted here: not ready, and
    cannot board.
    """
    token = _approved_application(real_db, with_offer=True)
    _sql(real_db, "UPDATE offers SET note_rate_pct = NULL WHERE app_id = 1")

    # every other canonical term is still present
    row = _sql(
        real_db,
        "SELECT apr, finance_charge, monthly_payment, amount_financed, "
        "total_of_payments FROM offers WHERE app_id = 1",
    )[0]
    assert all(v is not None for v in row.values()), "fixture must keep the rest complete"

    from app.routers import applications as app_router

    assert app_router._complete_offer_exists(1) is False, (
        "offer_ready reported a usable disclosure for an offer with no note rate"
    )

    # and the promise matches reality: accept refuses rather than guessing
    resp = _accept(token)
    assert resp.status_code == 409
    assert "note_rate_pct" in resp.json()["detail"], resp.json()["detail"]
    assert _sql(real_db, "SELECT count(*)::int AS n FROM loans")[0]["n"] == 0
