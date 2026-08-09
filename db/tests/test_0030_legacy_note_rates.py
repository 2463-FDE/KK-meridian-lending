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

            -- Three offers with three different histories:
            --   1 boarded at 11.25%   (loan exists, accepted_at NULL: pre-0021)
            --   2 boarded at 22.99%   (the top of the bulk seed's range)
            --   3 never boarded       (no loan, so no recoverable rate)
            INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, finance_charge,
                                monthly_payment, amount_financed, total_of_payments)
            VALUES (1, 1, 0.03, 13.10, 2000.00, 400.00, 9700.00, 11700.00),
                   (2, 2, 0.03, 25.40, 5000.00, 500.00, 19400.00, 24400.00),
                   (3, 3, 0.03, 10.07, 2369.15, 469.98, 14550.00, 16919.15);

            INSERT INTO loans (app_id, applicant_name, principal, apr, term_months, opened_at)
            VALUES (1, 'Boarded Eleven', 10000.00, 11.250, 36, '2026-01-02T00:00:00Z'),
                   (2, 'Boarded TwentyTwo', 20000.00, 22.990, 48, '2026-02-03T00:00:00Z');
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
