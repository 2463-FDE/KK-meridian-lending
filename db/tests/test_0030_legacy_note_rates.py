"""0030 must not overwrite a legacy offer's actual note rate.

Two review findings on PR #10, both about what this migration does to data that
already exists:

  1. it back-filled `note_rate_pct = 7.99` on EVERY offer, on the reasoning that
     create_offer had only ever written that literal. The seeds disagree --
     003_seed_bulk.sql generates `7.99 + (id % 16)`, i.e. up to 22.99, and
     002_seed.sql shipped 9.99% and 11.25% loans -- so on an upgraded database
     the constant would have replaced real rates with a rate the borrower was
     never given, and the new UI presents this column as a stored contractual
     fact;
  2. it left `accepted_at` NULL on offers boarded before migration 0021 (which
     added the column without back-filling), and 0030's own schedule columns are
     NULL by design. That combination is exactly what the widened repair path
     now accepts, so an authorised POST /offers retry could rewrite the terms of
     an already-funded offer.

Both are asserted here against a real Postgres, on a pre-0030 shaped database
built by hand, because both are properties of the SQL rather than of any
application code.
"""
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
SCHEMA = "migration_test_0030"


@pytest.fixture
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = False
    cur = connection.cursor()
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    cur.execute(f"CREATE SCHEMA {SCHEMA}")
    cur.execute(f"SET search_path TO {SCHEMA}")
    connection.commit()
    yield connection
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.commit()
    connection.close()


def _legacy_database(conn):
    """The pre-0030 shape: offers with no note rate, loans carrying the rate.

    Deliberately minimal -- only the columns 0030 reads or writes. Building the
    whole schema here would couple this test to every unrelated change to it.
    """
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("""
            -- Present in every real database since 001_schema.sql. Added to this
            -- legacy simulator because 0030 records its partial-contract
            -- demotions here before clearing them, and a migration that audits
            -- what it changed must not be made silent to keep a fixture small.
            CREATE TABLE audit_logs (
                id SERIAL PRIMARY KEY,
                actor TEXT,
                action TEXT,
                detail TEXT,
                at TIMESTAMPTZ DEFAULT now()
            );
            -- Also present in every real database since 001. 0030 stamps a
            -- pre-0011 offer's decision_id by joining to this table, which it
            -- must do BEFORE installing the NOT VALID check -- afterwards
            -- PostgreSQL rejects any UPDATE of a violating row, even of one
            -- unrelated column. Reviewed on PR #10.
            CREATE TABLE decisions (
                app_id INTEGER PRIMARY KEY,
                outcome TEXT NOT NULL
            );
            CREATE TABLE offers (
                id SERIAL PRIMARY KEY,
                app_id INTEGER UNIQUE,
                decision_id INTEGER UNIQUE,
                fee_pct_used NUMERIC(5,4),
                apr NUMERIC(7,3),
                finance_charge NUMERIC(14,2),
                monthly_payment NUMERIC(14,2),
                amount_financed NUMERIC(14,2),
                total_of_payments NUMERIC(14,2),
                accepted_at TIMESTAMPTZ
            );
            CREATE TABLE loans (
                id SERIAL PRIMARY KEY,
                app_id INTEGER UNIQUE,
                applicant_name TEXT,
                principal NUMERIC(14,2) NOT NULL,
                apr NUMERIC(7,3) NOT NULL,
                term_months INTEGER NOT NULL,
                status TEXT DEFAULT 'current',
                opened_at TIMESTAMPTZ DEFAULT now()
            );

            -- Four offers with four different histories. The monthly payments
            -- are the real amortized figures for each (principal, rate, term),
            -- because the backfill now PROVES the rate by reproducing them.
            --   1 seeded-style: loans.apr IS the note rate (11.25%)
            --   2 seeded-style: loans.apr IS the note rate (22.99%)
            --   3 never boarded: no loan, so nothing to recover
            --   4 boarded by the PRE-CHANGE path: loans.apr holds the DISCLOSED
            --     APR (5.196) while the payments were calculated at 7.99% --
            --     reading it as the note rate is the reviewed defect
            INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, finance_charge,
                                monthly_payment, amount_financed, total_of_payments)
            VALUES (1, 1, 0.03, 13.10, 1828.52, 328.57, 9700.00, 11828.52),
                   (2, 2, 0.03, 25.40, 11364.16, 640.92, 19400.00, 30764.16),
                   (3, 3, 0.03, 10.07, 2369.15, 469.98, 14550.00, 16919.15),
                   (4, 4, 0.03, 5.196, 3628.70, 439.35, 17460.00, 21088.70);

            -- One approved decision per application, matching what a real
            -- database has. Without these the new decision_id stamp has nothing
            -- to join to and the test would pass for the wrong reason.
            INSERT INTO decisions (app_id, outcome)
            VALUES (1, 'approve'), (2, 'approve'), (3, 'approve'), (4, 'approve');

            INSERT INTO loans (app_id, applicant_name, principal, apr, term_months, opened_at)
            VALUES (1, 'Boarded Eleven', 10000.00, 11.250, 36, '2026-01-02T00:00:00Z'),
                   (2, 'Boarded TwentyTwo', 20000.00, 22.990, 48, '2026-02-03T00:00:00Z'),
                   (4, 'Boarded ByApr', 18000.00, 5.196, 48, '2026-03-04T00:00:00Z');
        """)
    conn.commit()


def _apply_0030(conn):
    sql = (MIGRATIONS_DIR / "0030_offers_note_rate.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql)
    conn.commit()


def _offers(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT app_id, note_rate_pct, accepted_at FROM offers ORDER BY app_id")
        return {r["app_id"]: r for r in cur.fetchall()}


def _offers_full(conn):
    """Every column the newer tests assert on. `_offers` projects three, which
    reads as a KeyError rather than as a missing column."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT * FROM offers ORDER BY app_id")
        return {r["app_id"]: r for r in cur.fetchall()}


def test_a_boarded_offer_keeps_the_rate_it_was_written_at(conn):
    """The headline: 11.25% and 22.99% survive the migration.

    Under the constant back-fill both became 7.99%, and the borrower's own
    disclosure would then have shown a rate they were never quoted.
    """
    _legacy_database(conn)
    _apply_0030(conn)
    rows = _offers(conn)

    assert float(rows[1]["note_rate_pct"]) == pytest.approx(11.250, abs=1e-3)
    assert float(rows[2]["note_rate_pct"]) == pytest.approx(22.990, abs=1e-3)
    # And specifically NOT the constant.
    assert float(rows[1]["note_rate_pct"]) != pytest.approx(7.99, abs=1e-3)


def test_an_unboardable_legacy_offer_is_left_null_rather_than_guessed(conn):
    """No loan means no second record of the rate, and NULL says so.

    Downstream reads this correctly: accept refuses to board a row without a
    stored note rate, and the offer endpoint reports it absent rather than
    inventing one. A guessed value would be indistinguishable from a real one.
    """
    _legacy_database(conn)
    _apply_0030(conn)
    assert _offers(conn)[3]["note_rate_pct"] is None


def test_an_offer_boarded_before_0021_is_marked_accepted(conn):
    """The repair guard reads accepted_at, so a boarded offer must carry one.

    0021 added the column without back-filling. Left NULL, an offer with a loan
    behind it looks unaccepted to every guard that asks -- including the repair
    path, which would then rewrite terms somebody has already been funded
    against. The timestamp comes from the loan rather than now(), so the column
    does not claim the acceptance happened during the migration.
    """
    _legacy_database(conn)
    _apply_0030(conn)
    rows = _offers(conn)

    assert rows[1]["accepted_at"] is not None
    assert rows[1]["accepted_at"].year == 2026 and rows[1]["accepted_at"].month == 1
    assert rows[2]["accepted_at"] is not None
    # The unboarded one is untouched: it has not been accepted.
    assert rows[3]["accepted_at"] is None


def test_the_migration_is_idempotent_over_the_backfill(conn):
    """Replay must not turn a recovered rate into something else."""
    _legacy_database(conn)
    _apply_0030(conn)
    first = _offers(conn)
    _apply_0030(conn)
    second = _offers(conn)

    assert {k: v["note_rate_pct"] for k, v in first.items()} == {
        k: v["note_rate_pct"] for k, v in second.items()
    }


def test_a_disclosed_apr_in_loans_apr_is_not_taken_as_the_note_rate(conn):
    """`loans.apr` has held two different things, and only one of them is a rate.

    The pre-change acceptance path copied `offers.apr` -- the DISCLOSED APR --
    into that column, so an $18,000/48-month offer written at a contractual
    7.99% boarded `loans.apr = 5.196`. Backfilling from it indiscriminately
    would record 5.196% as the contractual fact the UI now displays: precisely
    the APR/note-rate conflation this migration exists to end. Review finding on
    PR #10.

    The two histories are told apart arithmetically: the value is accepted only
    if amortizing the loan's principal at that rate reproduces the offer's
    stored monthly payment. 5.196% over 18,000/48 gives 416.13, not the stored
    439.35, so it is refused and the row is left NULL for regeneration.
    """
    _legacy_database(conn)
    _apply_0030(conn)
    rows = _offers(conn)

    assert rows[4]["note_rate_pct"] is None, (
        "the disclosed APR was recorded as the contractual note rate"
    )
    # And the rows whose stored rate DOES reproduce their payment still recover.
    assert float(rows[1]["note_rate_pct"]) == pytest.approx(11.250, abs=1e-3)
    assert float(rows[2]["note_rate_pct"]) == pytest.approx(22.990, abs=1e-3)


def test_a_small_dollar_loan_does_not_falsely_certify_its_apr(conn):
    """A fixed $0.02 window admits a false positive on a small-dollar loan.

    A $100 12-month loan priced at 7.99% stores an $8.70 payment. Amortizing at
    its OLD disclosed APR of 7.609% gives $8.681 -- inside $0.02, so the APR
    would have been certified as the contractual note rate and shown to the
    borrower as one. A genuine note rate reproduces its own stored payment to
    the cent, so half a cent admits every true case and excludes this one.
    Reviewed on PR #10.
    """
    _legacy_database(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, "
                    "finance_charge, monthly_payment, amount_financed, total_of_payments) "
                    "VALUES (5, 5, 0.03, 7.609, 4.40, 8.70, 97.00, 104.40)")
        cur.execute("INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
                    "VALUES (5, 'Small Dollar', 100.00, 7.609, 12)")
    _apply_0030(conn)

    assert _offers(conn)[5]["note_rate_pct"] is None, (
        "a disclosed APR was certified as the contractual note rate on a "
        "small-dollar loan"
    )


def test_a_tiny_long_term_loan_is_left_null_because_its_cent_proves_nothing(conn):
    """Tightening the window does not survive scaling the principal down.

    The half-cent window fixes the $100/12mo case above by being narrower. It
    does not fix the underlying problem, because the payment gap between an APR
    and a note rate shrinks with the payment: a $5 loan over 84 months stores
    $0.08, and its disclosed APR of 8.925% reproduces $0.0803 -- a gap of
    $0.0003, inside half a cent. Agreement is not evidence here; this row's
    stored cent is compatible with a wide band of rates, so 8.925% would be
    certified as contractual fact on nothing at all.

    0030 therefore also requires SEPARABILITY: moving the rate by 0.125pp must
    move the computed payment by more than half a cent. On this row it moves it
    by $0.0003, so the row stays NULL. Reviewed on PR #10.
    """
    _legacy_database(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, "
                    "finance_charge, monthly_payment, amount_financed, total_of_payments) "
                    "VALUES (6, 6, 0.03, 8.925, 1.87, 0.08, 4.85, 6.72)")
        cur.execute("INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
                    "VALUES (6, 'Tiny Long Term', 5.00, 8.925, 84)")
    _apply_0030(conn)

    assert _offers(conn)[6]["note_rate_pct"] is None, (
        "a rate was certified on a row whose stored cent cannot distinguish it "
        "from a materially different rate"
    )


def test_separability_does_not_cost_ordinary_loans_their_recovered_rate(conn):
    """The guard above must not be a blanket refusal.

    A rule that left every row NULL would pass the test above and destroy the
    migration's purpose, so this asserts the other side: ordinary loans, whose
    payments do resolve a 0.125pp change, still recover their note rate. Without
    this pair, "safe" and "useless" are indistinguishable.
    """
    _legacy_database(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, "
                    "finance_charge, monthly_payment, amount_financed, total_of_payments) "
                    "VALUES (7, 7, 0.03, 7.99, 2369.17, 469.98, 14550.00, 16919.17)")
        cur.execute("INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
                    "VALUES (7, 'Ordinary', 15000.00, 7.99, 36)")
    _apply_0030(conn)

    assert float(_offers(conn)[7]["note_rate_pct"]) == pytest.approx(7.99, abs=1e-3), (
        "an ordinary loan lost its recoverable note rate to the separability guard"
    )


def test_a_pre_0011_offer_gets_its_decision_id_before_the_constraint_exists(conn):
    """The interaction the accepted-orphan test could not see.

    Review of PR #10: this migration's own back-fill certifies `note_rate_pct` on
    a legacy row while deliberately leaving the other contract fields NULL, so an
    ACCEPTED row of that shape is a partial contract. It can be neither demoted
    (an accepted disclosure is immutable) nor completed (that would invent
    terms), which is why the all-or-nothing CHECK goes on NOT VALID.

    PostgreSQL enforces a NOT VALID check on every subsequent UPDATE, including
    one that touches a single unrelated column. So disclosure-service's runtime
    `decision_id` stamp could not adopt such a row -- it would raise a check
    violation and the offer would stay unreachable, which is the exact state the
    runtime fix exists to end. The migration therefore stamps it here, before the
    constraint exists.

    Asserted against the real migration, not against a hand-built table: the
    earlier test used its own schema and so could not reproduce the constraint at
    all, which is why it missed this.
    """
    _legacy_database(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        # An accepted, boarded, pre-0011 offer: app_id set, decision_id NULL.
        cur.execute("INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, "
                    "finance_charge, monthly_payment, amount_financed, "
                    "total_of_payments, accepted_at) "
                    "VALUES (8, NULL, 0.03, 5.196, 3021.44, 439.35, 17460.00, "
                    "21021.44, '2026-01-01T00:00:00+00:00')")
        cur.execute("INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
                    "VALUES (8, 'Pre 0011', 18000.00, 5.196, 48)")
        cur.execute("INSERT INTO decisions (app_id, outcome) VALUES (8, 'approve')")
    _apply_0030(conn)

    row = _offers_full(conn)[8]
    assert row["decision_id"] == 8, (
        "a pre-0011 offer was left with a NULL decision_id; once the NOT VALID "
        "check is installed the service can no longer stamp it"
    )
    # And the row is still accepted and still untouched in its money.
    assert row["accepted_at"] is not None


def test_an_accepted_partial_contract_survives_the_migration_unchanged(conn):
    """The row that forces the constraint to be NOT VALID.

    It must not be demoted, must not be completed, and must not abort the
    migration. What it gets is a decision_id and nothing else.
    """
    _legacy_database(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, "
                    "finance_charge, monthly_payment, amount_financed, "
                    "total_of_payments, accepted_at) "
                    "VALUES (9, NULL, 0.03, 11.250, 1934.10, 386.00, 11640.00, "
                    "13574.10, '2026-02-02T00:00:00+00:00')")
        # A loan whose stored rate reproduces the payment, so the back-fill
        # certifies note_rate_pct and leaves this row PARTIAL.
        cur.execute("INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
                    "VALUES (9, 'Accepted Partial', 12000.00, 11.250, 36)")
        cur.execute("INSERT INTO decisions (app_id, outcome) VALUES (9, 'approve')")
    _apply_0030(conn)

    row = _offers_full(conn)[9]
    assert row["accepted_at"] is not None, "an accepted offer was un-accepted"
    assert float(row["monthly_payment"]) == 386.00, "an accepted offer's money changed"
    assert row["decision_id"] == 9, "the accepted orphan never got its decision_id"
    # Whatever the back-fill decided about the note rate, the schedule columns
    # were not invented for it.
    assert row["schedule_version"] is None
    assert row["final_payment"] is None
