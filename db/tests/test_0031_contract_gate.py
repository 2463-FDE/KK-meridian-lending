"""Migration 0031 must refuse to run unless the contract preconditions hold.

Reviewed as high severity: the migration runner applies every `*.sql` in
filename order, so a deploy carrying both the expand migration and this drop
would remove `payments.pan` in the same breath as the back-fill that made
losing it survivable. Any servicing instance still running the previous image
reads `payment.pan` and starts failing the moment the ALTER commits.

The separation between the two releases previously existed only in branch
topology (this PR is based on PR #11's branch, not on main) and in prose. A
merge to `main` erases both. These tests assert that the ordering is now
enforced by the migration itself, where it cannot be skipped by someone who did
not read the runbook.

Two independent preconditions, tested separately because they fail differently
and one can hold while the other does not:

  1. the back-fill is complete -- no row holds a `pan` with a NULL `last4`;
  2. the operator has acknowledged the deploy check, via a session GUC.

The negative cases matter more than the positive one here. A gate that lets the
migration through is indistinguishable from no gate at all, and the whole
finding was that the drop could happen too early.
"""
import os
import pathlib

import psycopg2
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
SCHEMA = "contract_gate_0031"

_0031 = (MIGRATIONS / "0031_drop_payments_pan_cvv.sql").read_text()

# Just enough of `payments` for the gate to have something to inspect. Built by
# hand rather than from db/init so the test states exactly which columns the
# gate depends on.
_SETUP = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};
SET search_path TO {SCHEMA};
CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    loan_id INTEGER,
    pan TEXT,
    cvv TEXT,
    last4 TEXT,
    amount NUMERIC(14,2),
    method TEXT
);
"""


@pytest.fixture
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = True
    with connection.cursor() as cur:
        cur.execute(_SETUP)
    yield connection
    with connection.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.close()


def _run_0031(conn, *, acknowledged: bool):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        if acknowledged:
            cur.execute("SET meridian.pan_drop_acknowledged = 'yes'")
        cur.execute(_0031)


def _columns(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'payments'",
            (SCHEMA,),
        )
        return {r[0] for r in cur.fetchall()}


def _insert(conn, **row):
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(f"INSERT INTO payments ({cols}) VALUES ({marks})", tuple(row.values()))


def test_without_the_acknowledgement_the_migration_refuses(conn):
    """The default path. An operator who runs the migrations folder in order,
    with no ceremony, must not drop these columns."""
    _insert(conn, loan_id=1, pan="4111111111111111", cvv="123", last4="1111")

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _run_0031(conn, acknowledged=False)

    assert "0031 refused" in str(exc.value)
    # And nothing was changed -- a gate that raises after the ALTER is no gate.
    assert {"pan", "cvv"} <= _columns(conn)


def test_an_incomplete_backfill_blocks_the_drop_even_when_acknowledged(conn):
    """The acknowledgement is about DEPLOYED CODE. It says nothing about
    whether the data migration finished, so it must not be able to wave that
    through: a row holding a pan with no last4 would lose the only record of
    the card used."""
    _insert(conn, loan_id=1, pan="4111111111111111", cvv="123", last4=None)

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _run_0031(conn, acknowledged=True)

    assert "0031 refused" in str(exc.value)
    assert "back-fill" in str(exc.value)
    assert {"pan", "cvv"} <= _columns(conn)


def test_the_error_names_what_to_do_next(conn):
    """An operator hitting this at 3am needs the next command, not a diagnosis.
    Asserted so a future edit cannot quietly reduce it to 'permission denied'."""
    _insert(conn, loan_id=1, pan="4111111111111111", last4="1111")

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _run_0031(conn, acknowledged=False)

    message = str(exc.value)
    assert "check_no_pan_readers" in message
    assert "meridian.pan_drop_acknowledged" in message
    assert "RUNBOOK" in message


def test_with_both_preconditions_satisfied_the_columns_are_dropped(conn):
    """The gate must not be so strict that the migration can never run."""
    _insert(conn, loan_id=1, pan="4111111111111111", cvv="123", last4="1111")

    _run_0031(conn, acknowledged=True)

    remaining = _columns(conn)
    assert "pan" not in remaining
    assert "cvv" not in remaining
    assert "last4" in remaining, "the surviving identifier must not be dropped too"


def test_a_database_with_no_legacy_rows_still_needs_the_acknowledgement(conn):
    """An empty table makes the back-fill check vacuous. The deploy risk is
    unchanged -- old instances still SELECT the column whether or not any row
    has a value in it -- so the acknowledgement must still be required."""
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _run_0031(conn, acknowledged=False)
    assert "0031 refused" in str(exc.value)
    assert {"pan", "cvv"} <= _columns(conn)


def test_the_migration_is_idempotent_once_it_has_run(conn):
    """Replay safety. After a successful drop the columns are gone, and running
    it again must be a no-op rather than an error -- the runner may replay."""
    _insert(conn, loan_id=1, pan="4111111111111111", last4="1111")
    _run_0031(conn, acknowledged=True)
    _run_0031(conn, acknowledged=True)
    assert "pan" not in _columns(conn)
