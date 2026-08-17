"""The offsetting-defects regression, against real PostgreSQL.

`test_reconciliation_matches_transactions.py` proves the comparison with a fake
database, which is the right place to state the arithmetic. This file proves the
parts a fake cannot: that the ledger side's `GROUP BY loan_id, processor_ref`
runs against the real schema, that the run's outcome and its transaction-level
counts are what actually land in `reconciliation_runs`, and that the uniqueness
the comparison depends on is enforced by the database rather than assumed by the
application.

The scenario is the reported one. On loan 4471:

* the processor settled `PR-100244` for 99.99 that we have no payment row for;
* `PR-100231` shows a capture of 250.00 and a refund of 99.99 that we never
  recorded, so the file settles it at 150.01 while we hold 250.00.

Per loan both sides total 250.00. The previous implementation compared exactly
that number, found no difference, and recorded `outcome='ok'` -- publishing a
success timestamp for having netted two real defects against each other.
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

REPO = pathlib.Path(__file__).resolve().parents[3]
SCHEMA = "reconciliation_transaction_level_test"

SETTLEMENT_DAY = "2026-08-09"

# (processor_ref, amount, type)
SETTLEMENT_ROWS = [
    ("PR-100231", "250.00", "capture"),
    ("PR-100244", "99.99", "capture"),
    ("PR-100231", "99.99", "refund"),
]


@pytest.fixture
def db(monkeypatch):
    from app import reconciliation

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute((REPO / "db" / "init" / "001_schema.sql").read_text(encoding="utf-8"))
        cur.execute(
            "INSERT INTO loans (id, applicant_name, principal, note_rate_pct, term_months) "
            "VALUES (4471, 'Sam Okafor', 9000, 5.946, 24)"
        )
        # The one capture we did record, keyed by the processor's reference.
        cur.execute(
            "INSERT INTO payments (loan_id, amount, method, auth_status, "
            "captured_at, processor_ref, capture_source) "
            "VALUES (4471, 250.00, 'card', 'captured', %s::date + TIME '10:00', "
            "'PR-100231', 'processor')",
            (SETTLEMENT_DAY,),
        )

    scoped = f"{DATABASE_URL}?options=-csearch_path%3D{SCHEMA}"
    monkeypatch.setattr(reconciliation.db, "DATABASE_URL", scoped, raising=False)
    monkeypatch.setattr(reconciliation.db, "_conn", None, raising=False)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()
    monkeypatch.setattr(reconciliation.db, "_conn", None, raising=False)


@pytest.fixture
def settlement(tmp_path, monkeypatch):
    from app import reconciliation

    path = tmp_path / "settlement.csv"
    lines = ["settlement_date,processor_ref,loan_id,amount,type"]
    lines += [f"{SETTLEMENT_DAY},{ref},4471,{amount},{kind}"
              for ref, amount, kind in SETTLEMENT_ROWS]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", str(path), raising=False)
    return str(path)


def _runs(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "SELECT outcome, error_code, loans_compared, references_compared, "
            "unreferenced_captures, out_of_scope_captures, breaks_found, "
            "break_value, breaks FROM reconciliation_runs ORDER BY id"
        )
        return cur.fetchall()


def test_offsetting_defects_record_a_breach_not_an_ok(db, settlement, monkeypatch):
    """The regression, end to end and durable."""
    from app import reconcile_job, reconciliation

    monkeypatch.setattr(reconciliation, "BREAK_THRESHOLD", Decimal("0"))

    exit_code = reconcile_job.main([])

    rows = _runs(db)
    assert rows, "no run was recorded at all"
    final = rows[-1]
    assert final["outcome"] == "breach", (
        "two offsetting defects on one loan netted to a clean per-loan total and "
        f"the run recorded outcome={final['outcome']!r}. That advances "
        "last_successful_run and the Prometheus success timestamp for a day on "
        "which real money movement was wrong."
    )
    assert final["breaks_found"] == 2
    assert final["break_value"] == Decimal("199.98"), (
        "the two differences cancelled instead of summing as absolute values"
    )
    assert exit_code == reconcile_job.EXIT_BREACH


def test_the_run_records_how_fine_the_comparison_was(db, settlement):
    """`loans_compared` alone cannot distinguish a transaction-level run from the
    per-loan one it replaced, so the count that can is recorded too."""
    from app import reconcile_job

    reconcile_job.main([])

    final = _runs(db)[-1]
    assert final["loans_compared"] == 1
    assert final["references_compared"] == 2
    assert final["unreferenced_captures"] == 0


def test_each_recorded_break_names_its_reference(db, settlement):
    """A break stored without the reference is one an operator has to re-derive,
    which is what the per-loan comparison forced."""
    from app import reconcile_job

    reconcile_job.main([])

    breaks = {b["processor_ref"]: b for b in _runs(db)[-1]["breaks"]}
    assert set(breaks) == {"PR-100231", "PR-100244"}
    assert breaks["PR-100244"]["kind"] == "settlement_only"
    assert breaks["PR-100231"]["kind"] == "amount_mismatch"


def test_the_ledger_side_groups_by_reference_against_the_real_schema(db, settlement):
    """The fake models `GROUP BY loan_id, processor_ref`; this proves the real
    query does, on the real column."""
    from app import reconciliation

    by_ref, unreferenced = reconciliation._ledger_by_ref((SETTLEMENT_DAY, SETTLEMENT_DAY))

    assert by_ref == {(4471, "PR-100231"): Decimal("250.00")}
    assert unreferenced == []


def test_a_capture_without_a_reference_is_returned_for_reporting(db, settlement):
    """Rows predating db/migrations/0041 have no reference and cannot be matched.
    They must reach the caller so it can report them, not be dropped by the
    query -- dropping them understates our own side."""
    from app import reconciliation

    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "INSERT INTO payments (loan_id, amount, method, auth_status, captured_at, "
            "capture_source) "
            "VALUES (4471, 75.00, 'card', 'captured', %s::date + TIME '11:00', "
            "'processor')",
            (SETTLEMENT_DAY,),
        )

    _by_ref, unreferenced = reconciliation._ledger_by_ref((SETTLEMENT_DAY, SETTLEMENT_DAY))

    assert [row["loan_id"] for row in unreferenced] == [4471]
    assert unreferenced[0]["amount"] == Decimal("75.00")


# --- the database enforces the key the comparison depends on -----------------

def test_two_payments_cannot_claim_the_same_settlement_reference(db):
    """One settlement line, one capture.

    If two rows could share a reference the comparison would be ambiguous
    exactly where it has to be exact -- a double-recorded capture would sum into
    one side and look like an amount mismatch rather than a duplicate.
    """
    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO payments (loan_id, amount, method, auth_status, "
                "processor_ref, capture_source) "
                "VALUES (4471, 10.00, 'card', 'captured', 'PR-100231', 'processor')"
            )


def test_unreferenced_rows_do_not_collide_with_each_other(db):
    """The index is partial on purpose: legacy captures all carry NULL, and a
    non-partial unique index would have made the migration unrunnable on any
    table with more than one of them."""
    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "INSERT INTO payments (loan_id, amount, method, auth_status) "
            "VALUES (4471, 10.00, 'card', 'captured'), (4471, 11.00, 'card', 'captured')"
        )
        cur.execute("SELECT count(*) FROM payments WHERE processor_ref IS NULL")
        assert cur.fetchone()[0] == 2


# --- the control must not breach on our own non-processor writes -------------

def test_a_servicing_legacy_payment_is_not_reported_as_a_break(db, settlement):
    """The reported defect.

    servicing-service's legacy `POST /payments` inserts with no `auth_status`, so
    the column default made every one of its rows a 'captured' payment with no
    `processor_ref` -- and once unreferenced captures became breaks, the control
    breached on our own writes permanently. It could never have matched them:
    that route calls no processor, so no settlement file contains a line for it.
    """
    from app import reconciliation

    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        # created_at inside the window, and captured_at left NULL, because that
        # is exactly what the legacy route writes: it sets neither column, so the
        # window predicate falls back to created_at.
        cur.execute(
            "INSERT INTO payments (loan_id, last4, brand, amount, method, "
            "capture_source, created_at) "
            "VALUES (4471, '4242', 'visa', 88.00, 'card', 'servicing_legacy', "
            "%s::date + TIME '09:00')",
            (SETTLEMENT_DAY,),
        )
        cur.execute(
            "SELECT auth_status, processor_ref, captured_at FROM payments "
            "WHERE capture_source = 'servicing_legacy'"
        )
        row = cur.fetchone()
        assert row[0] == "captured" and row[1] is None and row[2] is None, (
            "this test no longer reproduces the reported shape -- the legacy "
            "insert must still land as a captured row with no reference"
        )

    result = reconciliation.compare()

    assert not [b for b in result["breaks"] if b["kind"] == "unreferenced_capture"], (
        "a payment written by the legacy servicing route was reported as an "
        "unreferenced capture. No settlement file can contain it, so this break "
        "would fire on every run for ever"
    )
    assert result["out_of_scope_captures"] == 1, (
        "the excluded row was not counted -- an exclusion nobody can see is how "
        "a comparison quietly narrows until it compares nothing"
    )


def test_a_capture_of_unestablished_provenance_is_excluded_and_counted(db, settlement):
    """The default is 'unknown' rather than 'processor'. Rows written before
    db/migrations/0042 may or may not have been processor-backed, and admitting
    them to a money comparison on the strength of missing evidence would
    manufacture breaks."""
    from app import reconciliation

    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "INSERT INTO payments (loan_id, amount, method, auth_status, captured_at) "
            "VALUES (4471, 12.00, 'card', 'captured', %s::date + TIME '12:00')",
            (SETTLEMENT_DAY,),
        )
        cur.execute("SELECT capture_source FROM payments WHERE amount = 12.00")
        assert cur.fetchone()[0] == "unknown", (
            "the column default is no longer 'unknown', so a writer that forgets "
            "this column is admitted to the comparison as processor-backed"
        )

    result = reconciliation.compare()

    assert result["out_of_scope_captures"] == 1
    assert not [b for b in result["breaks"] if b["kind"] == "unreferenced_capture"]


def test_the_run_records_what_it_excluded(db, settlement):
    from app import reconcile_job

    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "INSERT INTO payments (loan_id, amount, method, capture_source, created_at) "
            "VALUES (4471, 5.00, 'card', 'servicing_legacy', %s::date + TIME '09:00')",
            (SETTLEMENT_DAY,),
        )

    reconcile_job.main([])

    assert _runs(db)[-1]["out_of_scope_captures"] == 1


# --- the seam between the two services, bound mechanically -------------------

def test_the_scope_this_filters_on_is_the_scope_payment_service_writes():
    """The reported defect lived exactly here, in the gap between two services.

    This module's ledger side is `WHERE capture_source = 'processor'`.
    payment-service's capture UPDATE wrote the join key and the timestamp and
    never set that column, so every real capture kept the schema default and was
    filtered out -- and every test on both sides still passed, because each one
    wrote the row it then read.

    Neither service can import the other (both expose a package named `app`), so
    the agreement is asserted against the source. If either side changes the
    value, this fails on the side that did not.
    """
    reconciliation_src = (REPO / "services" / "servicing-service" / "app"
                          / "reconciliation.py").read_text(encoding="utf-8")
    payments_src = (REPO / "services" / "payment-service" / "app"
                    / "payments.py").read_text(encoding="utf-8")

    assert "capture_source = 'processor'" in reconciliation_src, (
        "this module no longer scopes its ledger side to processor-backed "
        "captures, so servicing's legacy POST /payments rows are compared "
        "against a settlement file that cannot contain them"
    )

    capture_updates = [
        stmt for stmt in payments_src.split("db.query(")
        if "auth_status = 'captured'" in stmt
    ]
    assert len(capture_updates) == 2, (
        f"expected payment-service's two capture paths, found "
        f"{len(capture_updates)}"
    )
    for stmt in capture_updates:
        assert "capture_source = 'processor'" in stmt, (
            "a payment-service capture UPDATE does not set capture_source, so "
            "the row it writes is outside the scope this module compares -- the "
            "run would report ok against a ledger side the filter emptied"
        )
