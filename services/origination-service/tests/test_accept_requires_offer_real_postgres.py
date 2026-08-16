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

from sqlalchemy import select

from app import config, database, db, decision_state, intake, models
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
    scoped_url = f"{DATABASE_URL}?options=-csearch_path%3D{SCHEMA}"
    monkeypatch.setattr(db, "_conn", conn, raising=False)
    monkeypatch.setattr(db, "DATABASE_URL", scoped_url, raising=False)
    # The ORM read paths (application listing and detail) do NOT share the
    # psycopg2 connection patched above -- app/database.py builds its own
    # SQLAlchemy engine, lazily, from config.DATABASE_URL. Left unpatched it
    # connects to the public schema, where this fixture's rows do not exist,
    # and the detail endpoint answers "application not found" for a row that
    # is plainly in the test schema.
    #
    # Both globals are reset so the next request builds an engine bound to
    # SCHEMA; monkeypatch restores them on teardown, which also stops a later
    # test inheriting an engine pointed at a schema this fixture has dropped.
    monkeypatch.setattr(database, "DATABASE_URL", scoped_url, raising=False)
    monkeypatch.setattr(database, "_engine", None, raising=False)
    monkeypatch.setattr(database, "_Session", None, raising=False)
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
    # An application that reached 'approved' has a passing CIP result for itself
    # -- the decision gate refuses without one. Boarding re-checks it under its
    # own lock (round 10), because approval and funding are different moments and
    # an application approved before that gate existed still carries a valid
    # accept token. Seeded here so these tests exercise boarding rather than the
    # gate; the gate's refusals have their own file.
    _sql(conn, "INSERT INTO kyc_checks (applicant_id, application_id, name_verified, "
               "dob_verified, address_verified, ssn_verified, cip_passed) "
               "VALUES (1, 1, true, true, true, true, true)")
    _sql(conn, "INSERT INTO decisions (app_id, outcome) VALUES (1, 'approve')")

    raw_token = None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        raw_token = decision_state.issue_accept_token(cur, 1)

    if with_offer:
        _sql(conn,
             # Boarding requires the stored Model B schedule, so a fixture offer must
             # carry it -- an offer without one is a legacy row that cannot board.
             "INSERT INTO offers (app_id, decision_id, fee_pct_used, note_rate_pct, apr, "
             "finance_charge, monthly_payment, amount_financed, total_of_payments, "
             "regular_payment_count, final_payment, term_months, schedule_version, principal) "
             "VALUES (1, 1, 0.030, 7.990, %s, %s, %s, %s, %s, 23, 407.12, 24, 'B1', 9000.00)",
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
         "finance_charge, monthly_payment, amount_financed, total_of_payments, "
         "regular_payment_count, final_payment, term_months, schedule_version, principal) "
         "VALUES (1, 1, 0.030, 7.990, %s, %s, %s, %s, %s, 23, 407.12, 24, 'B1', 9000.00)",
         tuple(_COMPLETE_TERMS[k] for k in
               ("apr", "finance_charge", "monthly_payment", "amount_financed", "total_of_payments")))

    resp = _accept(token)
    assert resp.status_code == 200, resp.text

    loans = _sql(real_db, "SELECT id, note_rate_pct FROM loans WHERE app_id = 1")
    assert len(loans) == 1
    # The boarded rate is the CONTRACTUAL note rate, not the disclosed APR --
    # servicing amortizes what it is given, so this is what the borrower is
    # billed at (PR #10 review). Asserting the disclosed APR here is precisely
    # the confusion that shipped a schedule 13.59/month above the disclosure.
    assert float(loans[0]["note_rate_pct"]) == pytest.approx(_NOTE_RATE_PCT, abs=1e-3)
    assert float(loans[0]["note_rate_pct"]) != pytest.approx(_COMPLETE_TERMS["apr"], abs=1e-3)
    assert _sql(real_db, "SELECT count(*)::int AS n FROM balances WHERE loan_id = %s",
                (loans[0]["id"],))[0]["n"] == 1


# --- PR #10 review: the boarded loan must bill what the disclosure promised ---

def _amortized_payment(principal: float, annual_rate_pct: float, term_months: int) -> float:
    """The payment servicing will bill, computed the way servicing computes it
    (`servicing-service/app/schedule.py` amortizes `loans.note_rate_pct`). Written out
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

    loan = _sql(real_db, "SELECT principal, note_rate_pct, term_months FROM loans WHERE app_id = 1")[0]
    billed = _amortized_payment(float(loan["principal"]), float(loan["note_rate_pct"]), loan["term_months"])

    assert billed == pytest.approx(float(disclosed["monthly_payment"]), abs=0.01), (
        f"boarded loan bills {billed:.2f}/month against a disclosure of "
        f"{float(disclosed['monthly_payment']):.2f} -- servicing is amortizing "
        f"{loan['note_rate_pct']} where the contractual rate is {disclosed['note_rate_pct']}"
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
        "monthly_payment = %s, amount_financed = %s, total_of_payments = %s, "
        "regular_payment_count = 35, final_payment = 469.87, term_months = 36, "
        # RV2's own principal, not the fixture default. Boarding now opens the
        # loan at the offer's stored principal, so a fixture that left the
        # default here would bill RV2's 469.98 against a 9,000 loan -- which is
        # precisely the mismatch this change exists to prevent, and which this
        # test caught when the value was wrong.
        "schedule_version = 'B1', principal = 15000.00 "
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

    loan = _sql(real_db, "SELECT principal, note_rate_pct, term_months FROM loans WHERE app_id = 1")[0]

    # 7. the NOTE rate is what reached servicing, not the APR
    assert float(loan["note_rate_pct"]) == pytest.approx(7.99, abs=1e-3), (
        f"boarded {loan['note_rate_pct']} -- servicing must amortize the 7.99 note rate, "
        f"not the 10.072 APR"
    )
    assert float(loan["note_rate_pct"]) != pytest.approx(10.072, abs=1e-3)

    # 8. servicing reproduces the disclosed payment
    billed = _amortized_payment(float(loan["principal"]), float(loan["note_rate_pct"]), loan["term_months"])
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

    loan = _sql(real_db, "SELECT note_rate_pct FROM loans WHERE app_id = 1")[0]
    assert float(loan["note_rate_pct"]) == pytest.approx(float(offer["note_rate_pct"]), abs=1e-3)
    assert float(loan["note_rate_pct"]) != pytest.approx(float(offer["apr"]), abs=1e-3)


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


def test_a_legacy_offer_displays_but_cannot_board(real_db):
    """The BOARDING_REQUIRED_FIELDS / TILA_MONETARY_FIELDS split, end to end.

    A legacy offer has all four box amounts and a note rate, but its contractual
    schedule was never recorded (0030 deliberately does not back-fill: the exact
    terms of an already-accepted disclosure are unknown, and generating them today
    would persist invented terms as the agreed ones).

    Such a row must stay READABLE -- those amounts are what was disclosed, and
    withholding a real disclosure over a bookkeeping gap would be its own defect
    -- while being unboardable, because servicing cannot bill a schedule nobody
    stored.
    """
    from app.routers import applications as app_router

    token = _approved_application(real_db, with_offer=True)
    _sql(real_db,
         "UPDATE offers SET regular_payment_count = NULL, final_payment = NULL, "
         "term_months = NULL, schedule_version = NULL WHERE app_id = 1")

    row = _sql(real_db,
               "SELECT apr, finance_charge, monthly_payment, amount_financed, "
               "total_of_payments, note_rate_pct FROM offers WHERE app_id = 1")[0]

    # 1. still a readable historical disclosure -- every four-box amount present
    for field in app_router.TILA_MONETARY_FIELDS:
        assert row[field] is not None, f"legacy disclosure lost {field}"

    # 1b. and rendered as a disclosure when read the way the staff screen reads
    #     it -- THROUGH THE ORM MAPPING, not by inspecting the table.
    #
    # This assertion is why the e2e failure reached CI. The loop above passes on
    # a row that the detail endpoint renders as no offer at all, because it
    # queries SQL directly and so exercises neither the ORM mapping nor the
    # display gate -- the two things that were actually wrong.
    #
    # Read through a real Session rather than a constructed object on purpose:
    # every unit test in this service builds offer rows as plain objects that
    # carry whatever attributes the test sets, which is exactly why 648 of them
    # stayed green while this was broken. Only a mapped read can catch a column
    # the model forgot to declare.
    database._init()
    with database._Session() as session:
        orm_offer = session.scalar(
            select(models.Offer).where(models.Offer.app_id == 1)
        )
    assert orm_offer is not None

    disclosure = app_router._offer_disclosure_or_none(orm_offer, 1)
    assert disclosure is not None, (
        "a legacy offer rendered as no disclosure at all, though every "
        "disclosed amount is present in SQL -- this is what disabled Accept & "
        "board for offers that were perfectly complete"
    )
    for field in app_router.TILA_MONETARY_FIELDS:
        assert getattr(disclosure, field) == pytest.approx(float(row[field]))

    # 2. reports NOT ready for boarding. The disclosure above renders and this
    #    is still False: displaying what was disclosed and permitting funding
    #    are separate questions, and the UI disables the button on this one.
    assert app_router._complete_offer_exists(1) is False

    # 3. acceptance refuses, naming the missing schedule
    resp = _accept(token)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "final_payment" in detail or "schedule" in detail.lower(), detail

    # 4. no loan and no balance were created
    assert _sql(real_db, "SELECT count(*)::int AS n FROM loans")[0]["n"] == 0
    assert _sql(real_db, "SELECT count(*)::int AS n FROM balances")[0]["n"] == 0

    # 5. and it becomes boardable only once the schedule is explicitly recorded --
    #    the audited regeneration path, not an inference at accept time
    _sql(real_db,
         "UPDATE offers SET regular_payment_count = 23, final_payment = 407.12, "
         "term_months = 24, schedule_version = 'B1', principal = 9000.00 WHERE app_id = 1")
    assert app_router._complete_offer_exists(1) is True
    assert _accept(token).status_code == 200


def test_boarding_required_fields_is_a_strict_superset_of_the_tila_amounts():
    """The two sets must not drift into one another.

    If BOARDING_REQUIRED_FIELDS ever stopped containing the monetary fields, an
    offer could board without a complete disclosure. If they became equal, the
    schedule facts would stop being required and the legacy-boarding hole would
    reopen.
    """
    from app.routers import applications as app_router

    tila = set(app_router.TILA_MONETARY_FIELDS)
    boarding = set(app_router.BOARDING_REQUIRED_FIELDS)
    assert tila < boarding, "boarding must require strictly more than the four-box amounts"
    assert {"note_rate_pct", "regular_payment_count", "final_payment",
            "term_months", "schedule_version"} <= boarding
    assert app_router._CANONICAL_OFFER_FIELDS == app_router.BOARDING_REQUIRED_FIELDS, (
        "offer_ready and accept must enforce the same set, or a caller is told "
        "'ready' and then gets a 409"
    )


def test_boarding_copies_the_contractual_schedule_onto_the_loan(real_db):
    """The offer's Model B terms must land on `loans`, not be recomputed later.

    Servicing used to regenerate the schedule from principal/rate/term with
    whatever generator was deployed at read time, so a later rounding-policy
    change silently altered the contractual terms of a signed loan. Under
    Model B it is worse than drift: the final payment absorbs the cent residue
    and cannot be recovered from any other stored figure, so a recomputation
    cannot reproduce it at all.

    Asserted field by field against the offer row rather than against literals,
    so the test proves a COPY happened and not merely that some plausible
    numbers were written.
    """
    token = _approved_application(real_db, with_offer=True)
    resp = _accept(token)
    assert resp.status_code == 200, resp.text

    offer = _sql(real_db,
                 "SELECT monthly_payment, regular_payment_count, final_payment, "
                 "term_months, schedule_version, note_rate_pct FROM offers WHERE app_id = 1")[0]
    loan = _sql(real_db,
                "SELECT note_rate_pct, term_months, regular_payment, regular_payment_count, "
                "final_payment, schedule_version FROM loans WHERE app_id = 1")[0]

    assert loan["regular_payment"] == offer["monthly_payment"]
    assert loan["regular_payment_count"] == offer["regular_payment_count"]
    assert loan["final_payment"] == offer["final_payment"]
    assert loan["schedule_version"] == offer["schedule_version"]
    assert loan["term_months"] == offer["term_months"]
    # And the rate boarded is still the contractual note rate, not the
    # disclosed APR -- the two are deliberately different in this fixture, so
    # a confusion between them fails here rather than passing by coincidence.
    assert float(loan["note_rate_pct"]) == _NOTE_RATE_PCT
    assert float(loan["note_rate_pct"]) != _COMPLETE_TERMS["apr"]

    # The copy satisfies the schedule constraints, which is not automatic: the
    # count/term identity is checked against the LOAN's term, so a boarding bug
    # that copied the wrong term could not have committed this row at all.
    assert loan["regular_payment_count"] + 1 == loan["term_months"]


def test_the_boarded_term_is_the_offers_term_not_the_applications(real_db):
    """Boarding reads the term the schedule was solved for.

    These agree today -- the offer is built from the application's term and the
    stored value is server-derived -- so the call site used the application row
    and nothing failed. It is still the wrong source: only the offer's term is
    the one its payment count belongs to, and loans_schedule_term_agrees checks
    the count against whatever boarding writes. A counteroffer at a different
    term would board a schedule filed under a term it does not describe.

    Made observable by giving the two rows different terms, which is only
    reachable through direct SQL -- exactly how a counteroffer path would
    eventually write it.
    """
    token = _approved_application(real_db, with_offer=True)
    # Offer says 24 months / 23 regular payments (as inserted); application is
    # rewritten to 36 so the two disagree.
    _sql(real_db, "UPDATE applications SET term_months = 36 WHERE id = 1")

    assert _accept(token).status_code == 200

    loan = _sql(real_db, "SELECT term_months, regular_payment_count FROM loans WHERE app_id = 1")[0]
    assert loan["term_months"] == 24, (
        "boarded the application's requested term instead of the offer's "
        "contractual term"
    )
    assert loan["regular_payment_count"] + 1 == loan["term_months"]


def test_a_boarding_failure_leaves_no_partly_recorded_loan(real_db, monkeypatch):
    """The schedule copy is in the accept transaction, not after it.

    A loan committed without the terms it is billed on would be a funded
    contract whose payment amounts exist nowhere -- and accept has no second
    chance to write them, because the same transaction marks the offer
    accepted and the offer is thereafter immutable.

    Forced by making the balances insert fail after the loan insert has already
    succeeded, so the failure lands strictly between the two writes.
    """
    token = _approved_application(real_db, with_offer=True)

    real_board = intake.board_to_servicing_tx

    def _fail_after_loan_insert(cur, *a, **k):
        real_board(cur, *a, **k)
        raise RuntimeError("simulated failure after the loan row was inserted")

    monkeypatch.setattr(intake, "board_to_servicing_tx", _fail_after_loan_insert)

    with pytest.raises(RuntimeError):
        _accept(token)

    assert _sql(real_db, "SELECT count(*)::int AS n FROM loans")[0]["n"] == 0
    assert _sql(real_db, "SELECT count(*)::int AS n FROM balances")[0]["n"] == 0
    # And the offer was NOT marked accepted, so the borrower can still complete.
    assert _sql(real_db, "SELECT accepted_at FROM offers WHERE app_id = 1")[0]["accepted_at"] is None
    assert _sql(real_db, "SELECT status FROM applications WHERE id = 1")[0]["status"] == "approved"


def test_boarding_opens_the_loan_at_the_offers_principal_not_the_applications(real_db):
    """The loan must amortize the schedule it is billing.

    Boarding copies the offer's stored payments and term. It used to take the
    principal from `applications.amount` instead of from the offer, so if the
    requested amount were corrected after the offer was written -- or a future
    counteroffer carried a different principal -- the loan would open at one
    number while billing a schedule solved for another, leaving a residue that
    never amortizes to zero. Review finding on PR #10.

    The two are made deliberately different here, which is the only way to tell
    which one was used: in normal operation they agree.
    """
    token = _approved_application(real_db, with_offer=True)
    # The offer is the contract: 9,000 over 24 months, as seeded. The
    # application's requested amount is then corrected to something else.
    _sql(real_db, "UPDATE applications SET amount = 12345.00 WHERE id = 1")

    resp = _accept(token)
    assert resp.status_code == 200, resp.text

    loan = _sql(real_db, "SELECT principal, regular_payment, term_months FROM loans WHERE app_id = 1")[0]
    offer = _sql(real_db, "SELECT principal, monthly_payment FROM offers WHERE app_id = 1")[0]

    assert float(loan["principal"]) == pytest.approx(float(offer["principal"]), abs=0.005), (
        "the loan boarded the application's requested amount, so it is billing a "
        "schedule that was solved for a different principal"
    )
    assert float(loan["principal"]) != pytest.approx(12345.00, abs=0.005)
    # And the schedule it bills is still the offer's.
    assert float(loan["regular_payment"]) == pytest.approx(float(offer["monthly_payment"]), abs=0.005)
