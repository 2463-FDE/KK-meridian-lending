"""PR #8 review (high) -- a captured payment must reach the loan balance even
if the borrower never comes back.

The reported gap: `_apply_via_servicing` catches any servicing failure and
returns False after the card is already authorized. The only recovery was a
client retry on the same idempotency_key, and `applied_at IS NULL` was queried
nowhere in the repository -- so a borrower who closed the tab left money
captured, the balance uncredited, and nothing that would even list the row.

The reviewer's first-named test is the one this file leads with: the processor
captures, servicing returns 500, and the payment still reconciles with no user
retry.

Real Postgres, because every property being asserted is a property of SQL:
the claim is an `UPDATE ... FOR UPDATE SKIP LOCKED` that must be atomic under
concurrency, the backoff is computed in the database, and the partial index
predicate is what scopes the work. A mocked cursor would assert none of it.
Only the HTTP boundary to servicing is faked.
"""
import os
import threading

import psycopg2
import psycopg2.extras
import pytest

from app import db, reconcile
from app.config import RECONCILE_BACKOFF_CAP_SECONDS, RECONCILE_MAX_ATTEMPTS

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

SCHEMA = "payment_reconcile_test"


def _schema_sql():
    """The payments columns this drain depends on, matching db/init/001_schema.sql
    plus db/migrations/0028."""
    return f"""
        SET search_path TO {SCHEMA};
        CREATE TABLE payments (
            id SERIAL PRIMARY KEY,
            loan_id INTEGER,
            amount NUMERIC(14,2) NOT NULL,
            method TEXT DEFAULT 'card',
            auth_status TEXT NOT NULL DEFAULT 'captured',
            authorization_id TEXT,
            idempotency_key TEXT,
            applied_at TIMESTAMPTZ,
            apply_attempts INTEGER NOT NULL DEFAULT 0,
            apply_next_attempt_at TIMESTAMPTZ,
            apply_last_error TEXT,
            -- db/migrations/0043. The drain RETURNS this column, so a fixture
            -- without it fails the claim query rather than the assertion, and
            -- the failure names SQL instead of behaviour.
            correlation_id TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE INDEX idx_payments_unapplied ON payments (apply_next_attempt_at)
            WHERE auth_status = 'captured' AND applied_at IS NULL;
    """


@pytest.fixture
def pg(monkeypatch):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(_schema_sql())
    monkeypatch.setattr(db, "_conn", conn, raising=False)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def _rows(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _payment(conn, *, loan_id=1, amount="100.00", auth_status="captured",
             applied=False, attempts=0, next_attempt="NULL"):
    row = _rows(
        conn,
        "INSERT INTO payments (loan_id, amount, auth_status, applied_at, apply_attempts, "
        f"apply_next_attempt_at) VALUES (%s, %s, %s, %s, %s, {next_attempt}) RETURNING id",
        (loan_id, amount, auth_status,
         "2026-01-01T00:00:00+00:00" if applied else None, attempts),
    )
    return row[0]["id"]


class _Servicing:
    """Stands in for the HTTP call. `up` flips servicing between down and up
    without touching production code."""

    def __init__(self, up=True):
        self.up = up
        self.calls = []

    def install(self, monkeypatch, conn):
        from app import payments as payments_mod

        def fake_apply(loan_id, amount, payment_id):
            self.calls.append(payment_id)
            if not self.up:
                _rows(conn, "UPDATE payments SET apply_last_error = %s WHERE id = %s",
                      ("HTTPStatusError", payment_id))
                return False
            _rows(conn, "UPDATE payments SET applied_at = now() WHERE id = %s", (payment_id,))
            return True

        monkeypatch.setattr(payments_mod, "_apply_via_servicing", fake_apply)
        return self


# --- the reviewer's first-named test ------------------------------------------

def test_captured_payment_with_servicing_down_reconciles_without_a_user_retry(pg, monkeypatch):
    """Processor captured, servicing 500'd, borrower never comes back. The drain
    must credit the balance on its own once servicing recovers."""
    payment_id = _payment(pg)                      # captured, applied_at NULL
    servicing = _Servicing(up=False).install(monkeypatch, pg)

    # Servicing still down: the row is claimed, retried, and left pending --
    # not lost, not silently marked applied.
    first = reconcile.reconcile_once()
    assert first == {"claimed": 1, "applied": 0, "still_pending": 1}
    row = _rows(pg, "SELECT * FROM payments WHERE id = %s", (payment_id,))[0]
    assert row["applied_at"] is None
    assert row["apply_attempts"] == 1
    assert row["apply_next_attempt_at"] is not None, "a failed attempt must schedule the next one"
    assert row["apply_last_error"] == "HTTPStatusError"

    # Backoff is real: an immediate second pass must not re-claim it.
    assert reconcile.reconcile_once() == {"claimed": 0, "applied": 0, "still_pending": 0}

    # Servicing recovers, its backoff comes due -- no borrower involvement.
    servicing.up = True
    _rows(pg, "UPDATE payments SET apply_next_attempt_at = now() - interval '1 second' WHERE id = %s",
          (payment_id,))
    assert reconcile.reconcile_once() == {"claimed": 1, "applied": 1, "still_pending": 0}

    row = _rows(pg, "SELECT * FROM payments WHERE id = %s", (payment_id,))[0]
    assert row["applied_at"] is not None, "the balance was never credited"
    assert servicing.calls == [payment_id, payment_id]


def test_backoff_grows_and_is_capped(pg, monkeypatch):
    _Servicing(up=False).install(monkeypatch, pg)
    payment_id = _payment(pg)

    delays = []
    for _ in range(4):
        _rows(pg, "UPDATE payments SET apply_next_attempt_at = now() - interval '1 second' WHERE id = %s",
              (payment_id,))
        reconcile.reconcile_once()
        row = _rows(pg, "SELECT apply_attempts, "
                        "extract(epoch FROM (apply_next_attempt_at - now()))::int AS wait "
                        "FROM payments WHERE id = %s", (payment_id,))[0]
        delays.append(row["wait"])

    assert delays == sorted(delays), f"backoff must not shrink: {delays}"
    assert max(delays) <= RECONCILE_BACKOFF_CAP_SECONDS


def test_retries_stop_after_the_cap_but_the_row_stays_visible(pg, monkeypatch):
    """Giving up on automatic retry must not look like resolution -- the money
    was captured and never credited, so it stays in the operator's report."""
    _Servicing(up=False).install(monkeypatch, pg)
    payment_id = _payment(pg, attempts=RECONCILE_MAX_ATTEMPTS)

    assert reconcile.reconcile_once()["claimed"] == 0, "exhausted rows must not be re-claimed"

    summary = reconcile.unreconciled_summary()
    assert summary["pending"] == 1
    assert summary["exhausted"] == 1
    assert summary["amount_pending"] == pytest.approx(100.00)
    row = _rows(pg, "SELECT applied_at FROM payments WHERE id = %s", (payment_id,))[0]
    assert row["applied_at"] is None


# --- scoping: the drain must not touch anything else --------------------------

@pytest.mark.parametrize("kind,kwargs", [
    ("already applied", {"applied": True}),
    ("declined", {"auth_status": "failed"}),
    ("still authorizing", {"auth_status": "pending"}),
])
def test_reconciler_ignores_payments_it_has_no_business_touching(pg, monkeypatch, kind, kwargs):
    servicing = _Servicing(up=True).install(monkeypatch, pg)
    _payment(pg, **kwargs)

    assert reconcile.reconcile_once()["claimed"] == 0, f"{kind} row was claimed"
    assert servicing.calls == []


def test_unreconciled_summary_counts_only_captured_and_unapplied(pg):
    _payment(pg, amount="10.00")                       # counted
    _payment(pg, amount="20.00")                       # counted
    _payment(pg, amount="99.00", applied=True)         # not counted
    _payment(pg, amount="77.00", auth_status="failed")  # not counted

    summary = reconcile.unreconciled_summary()
    assert summary["pending"] == 2
    assert summary["amount_pending"] == pytest.approx(30.00)
    assert summary["exhausted"] == 0
    assert summary["oldest_created_at"] is not None


# --- concurrency ---------------------------------------------------------------

def test_two_concurrent_workers_never_claim_the_same_payment(pg):
    """Two replicas polling at the same instant must get disjoint sets, or the
    same capture gets applied twice. FOR UPDATE SKIP LOCKED is what guarantees
    it; this proves it against real Postgres with independent connections."""
    for _ in range(6):
        _payment(pg)

    start = threading.Barrier(2)
    claimed = {}

    def worker(name):
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                start.wait(timeout=10)
                cur.execute(reconcile._CLAIM_SQL,
                            (RECONCILE_BACKOFF_CAP_SECONDS,
                             RECONCILE_MAX_ATTEMPTS, 6))
                claimed[name] = {r["id"] for r in cur.fetchall()}
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not (claimed["a"] & claimed["b"]), (
        f"the same payment was claimed by both workers: {claimed['a'] & claimed['b']}"
    )
    assert len(claimed["a"]) + len(claimed["b"]) == 6, "every due payment must be claimed exactly once"
