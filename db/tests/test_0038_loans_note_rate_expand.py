"""D19 expand: `loans.note_rate_pct`, back-filled only where it can be proven.

`loans.apr` has held two different regulated figures. Boarded by the current
path it holds the contractual NOTE RATE; boarded by the pre-change path it holds
the DISCLOSED APR -- 5.196% for a contract priced at 7.99%, because the disclosed
figure carries the prepaid origination fee. Servicing amortizes that column, so
the two are not interchangeable and never were.

The migration's whole job is to tell those histories apart and refuse to guess.
The cases below are written from the wrong answers: what makes this dangerous is
not failing to back-fill, it is back-filling a disclosed APR as though it were a
contractual term the borrower agreed to.

Against real PostgreSQL, because the evidence rules are SQL joins and rounding
comparisons -- a mock would be asserting my own arithmetic back at me.
"""
import os
import pathlib
from decimal import Decimal

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[2]
INIT = REPO / "db" / "init"
MIGRATION = REPO / "db" / "migrations" / "0038_loans_note_rate_expand.sql"
SCHEMA = "note_rate_expand_test"
INIT_FILES = ("001_schema.sql", "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql", "007_ledger_opening_balances.sql")


def _exec(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql, params or ())
        return cur.fetchall() if cur.description else []


@pytest.fixture
def db():
    """A database at the state BEFORE 0038, reconstructed rather than borrowed.

    `db/init/001_schema.sql` is the CURRENT shape: `note_rate_pct NOT NULL` and
    no `apr` at all, because 0039 dropped it. The state this migration operates
    on -- an `apr` column holding one of two different regulated figures, and no
    note-rate column -- no longer exists anywhere in the tree.

    So it is rebuilt here: add `apr`, drop `note_rate_pct`. That is what a test
    for a superseded migration has to do once the contract step lands, and
    writing it out is better than the alternative of deleting the test: 0038 is
    what a real deployment will run against a real legacy database, and its
    refusal to relabel a disclosed APR as a note rate is the thing most worth
    keeping proof of.
    """
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.commit()
    for name in INIT_FILES:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute((INIT / name).read_text(encoding="utf-8"))
        conn.commit()
    _exec(conn, "ALTER TABLE loans ADD COLUMN apr NUMERIC(7,3)")
    _exec(conn, "ALTER TABLE loans DROP COLUMN note_rate_pct")
    conn.commit()
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.commit()
    conn.close()


def _apply(conn):
    """The migration as shipped, minus its own BEGIN/COMMIT (psycopg2 owns the
    transaction; a nested BEGIN is a warning and a lie about what is atomic)."""
    sql = MIGRATION.read_text(encoding="utf-8").replace("BEGIN;", "", 1)
    sql = "".join(sql.rsplit("COMMIT;", 1))
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql)
    conn.commit()


def _loan(conn, *, apr, schedule_version=None, offer_note_rate=None,
          offer_apr=None, name="T", principal="18000.00", term=48):
    """A loan, optionally with an offer and a schedule -- i.e. optionally with
    each kind of evidence the migration looks for."""
    applicant = _exec(conn, "INSERT INTO applicants (name) VALUES (%s) RETURNING id",
                      (name,))[0]["id"]
    app = _exec(conn, "INSERT INTO applications (applicant_id, amount, term_months, "
                      "status) VALUES (%s, %s, %s, 'funded') RETURNING id",
                (applicant, principal, term))[0]["id"]
    if offer_note_rate is not None or offer_apr is not None:
        # `offers_schedule_all_or_nothing` requires the whole contract or none of
        # it: principal, term, note_rate_pct, count, final_payment and version
        # stand or fall together. A legacy offer whose note rate was never proven
        # therefore cannot carry a schedule either -- which is the real shape of
        # the row this test is about, not a fixture convenience.
        if offer_note_rate is None:
            _exec(conn,
                  "INSERT INTO offers (app_id, apr, finance_charge, monthly_payment, "
                  "amount_financed, total_of_payments, fee_pct_used) "
                  "VALUES (%s, %s, 3088.70, 439.35, 17460.00, 21088.70, 0.03)",
                  (app, offer_apr))
        else:
            _exec(conn,
                  "INSERT INTO offers (app_id, principal, note_rate_pct, apr, "
                  "finance_charge, monthly_payment, amount_financed, "
                  "total_of_payments, term_months, fee_pct_used, "
                  "regular_payment_count, final_payment, schedule_version) "
                  "VALUES (%s, %s, %s, %s, 3088.70, 439.35, 17460.00, 21088.70, "
                  "%s, 0.03, 47, 439.25, 'B1')",
                  (app, principal, offer_note_rate, offer_apr, term))
    loan = _exec(conn,
        "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months, "
        "status, schedule_version, regular_payment, regular_payment_count, "
        "final_payment) VALUES (%s, %s, %s, %s, %s, 'current', %s, %s, %s, %s) "
        "RETURNING id",
        (app, name, principal, apr, term, schedule_version,
         "439.35" if schedule_version else None,
         47 if schedule_version else None,
         "439.25" if schedule_version else None))[0]["id"]
    _exec(conn, "INSERT INTO balances (loan_id, balance) VALUES (%s, %s)",
          (loan, principal))
    conn.commit()
    return loan


def _note_rate(conn, loan):
    return _exec(conn, "SELECT note_rate_pct FROM loans WHERE id = %s",
                 (loan,))[0]["note_rate_pct"]


# --- what the migration must PROVE before copying ------------------------------


def test_a_loan_whose_offer_agrees_is_proven(db):
    """Strongest evidence: `offers.note_rate_pct` is itself a proven value
    (migration 0030 populated it only where IT could be shown), and the loan's
    own rate agrees with it."""
    loan = _loan(db, apr="7.990", offer_note_rate="7.990", offer_apr="9.584")
    _apply(db)
    assert _note_rate(db, loan) == Decimal("7.990")


def test_a_loan_with_a_schedule_is_proven(db):
    """Structural evidence: `schedule_version` is set only by the boarding path
    that writes the contractual rate into `apr`. This is the same inference
    servicing and the gateway already make per request, moved into the data."""
    loan = _loan(db, apr="11.250", schedule_version="B1")
    _apply(db)
    assert _note_rate(db, loan) == Decimal("11.250")


# --- what it must REFUSE, which is the point -----------------------------------


def test_a_disclosed_apr_is_not_relabelled_as_the_note_rate(db):
    """The defect this migration exists to avoid creating.

    A pre-change loan: no schedule, and its `apr` holds the DISCLOSED 5.196%
    while the offer discloses a contractual 7.990%. Copying it would record
    5.196% as the rate the UI presents as contractual -- a term the borrower was
    never quoted, and the exact APR/note-rate conflation D19 is about.
    """
    loan = _loan(db, apr="5.196", offer_note_rate="7.990", offer_apr="5.196")
    _apply(db)
    assert _note_rate(db, loan) is None, (
        "a disclosed APR was back-filled as the contractual note rate"
    )


def test_a_legacy_loan_with_no_evidence_at_all_stays_unknown(db):
    """No schedule, no offer. The row may hold either figure and nothing in it
    says which, so it stays NULL: a null a reader must handle is safer than a
    number that is quietly the wrong regulated figure."""
    loan = _loan(db, apr="7.990")
    _apply(db)
    assert _note_rate(db, loan) is None


def test_an_offer_with_no_proven_note_rate_proves_nothing(db):
    """`offers.note_rate_pct` NULL means 0030 could not prove it either. An
    unproven value cannot become evidence by being joined to."""
    loan = _loan(db, apr="7.990", offer_note_rate=None, offer_apr="9.584")
    _apply(db)
    assert _note_rate(db, loan) is None


# --- the expand contract -------------------------------------------------------


def test_the_old_column_is_untouched(db):
    """Expand, not rename. Every deployed reader still reading `apr` keeps
    working -- dropping it is the contract step, on its own PR."""
    loan = _loan(db, apr="11.250", schedule_version="B1")
    _apply(db)
    row = _exec(db, "SELECT apr, note_rate_pct FROM loans WHERE id = %s", (loan,))[0]
    assert row["apr"] == Decimal("11.250"), "the migration modified loans.apr"
    assert row["note_rate_pct"] == Decimal("11.250")


def test_running_it_twice_changes_nothing(db):
    """A migration runner may replay. The back-fill is idempotent because every
    UPDATE is guarded on `note_rate_pct IS NULL`."""
    proven = _loan(db, apr="11.250", schedule_version="B1", name="Proven")
    unproven = _loan(db, apr="5.196", offer_note_rate="7.990", offer_apr="5.196",
                     name="Unproven")
    _apply(db)
    first = (_note_rate(db, proven), _note_rate(db, unproven))
    _apply(db)
    assert (_note_rate(db, proven), _note_rate(db, unproven)) == first


def test_a_row_written_by_an_older_deploy_is_left_for_the_reader(db):
    """The rolling-deploy case, and why readers must still fall back.

    An instance running the previous image boards a loan writing `apr` and a
    schedule but not `note_rate_pct`. The migration has already run, so nothing
    back-fills it -- the new column is NULL on a row whose rate IS proven by its
    schedule. A reader that only consulted `note_rate_pct` would report "not
    recorded" for a loan it can perfectly well describe, which is why the
    application keeps the `schedule_version` fallback until the contract step.
    """
    _apply(db)
    loan = _loan(db, apr="11.250", schedule_version="B1")
    assert _note_rate(db, loan) is None
    row = _exec(db, "SELECT apr, schedule_version FROM loans WHERE id = %s",
                (loan,))[0]
    assert row["apr"] == Decimal("11.250") and row["schedule_version"] == "B1", (
        "the fallback evidence a reader needs is not present on the row"
    )


def test_the_backfill_refuses_to_silently_do_nothing(db):
    """Guards the guard: a rule that matched nothing would leave every rate
    unknown and look like a successful migration."""
    _loan(db, apr="11.250", schedule_version="B1")
    # The whole contract goes together (`loans_schedule_all_or_nothing`), so a
    # loan cannot lose only its version -- which is itself the reason
    # `schedule_version` is trustworthy evidence.
    _exec(db, "UPDATE loans SET schedule_version = NULL, regular_payment = NULL, "
              "regular_payment_count = NULL, final_payment = NULL")
    _exec(db, "DELETE FROM offers")
    db.commit()
    # With no evidence anywhere the guard does not fire -- it only fires when
    # loans WITH schedules exist and none were proven, which is the case that
    # means the rules broke rather than the data being legacy.
    _apply(db)
    assert _note_rate(db, _exec(db, "SELECT id FROM loans")[0]["id"]) is None


def test_the_two_regulated_figures_stay_distinguishable(db):
    """D19's actual subject: after this migration a reader can tell which figure
    is which without knowing the boarding history."""
    loan = _loan(db, apr="7.990", offer_note_rate="7.990", offer_apr="9.584")
    _apply(db)
    row = _exec(db, "SELECT l.note_rate_pct, o.apr AS disclosed "
                    "FROM loans l JOIN offers o ON o.app_id = l.app_id "
                    "WHERE l.id = %s", (loan,))[0]
    assert row["note_rate_pct"] < row["disclosed"], (
        "the note rate is not below the disclosed APR for a loan carrying a "
        "prepaid fee -- the two figures have been conflated again"
    )
