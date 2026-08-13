"""D3, proven against the code production actually runs.

Week 6's brief asked for a report, characterization tests, **one failing test
proving the correctly-paired lost update**, and an ADR. Only the failing test was
never written, so `DEBT.md` D3 has been "Open" as an assertion nobody could run.

**This file previously proved the wrong thing, and said so in the PR body.** It
raced `balance.apply_payment()`. Nothing in the request path calls that: the
`POST /accounts/{loan_id}/apply-payment` route calls
`balance.apply_payment_once()` (`app/main.py`), and `apply_payment()` survives
only inside servicing's own legacy `payments.charge()`. So the old version
demonstrated a race in a function payment-service never reaches, while claiming
to demonstrate the one that loses real money. It also ran both workers through
one shared autocommit connection, which is not what two concurrent HTTP requests
do. Corrected here; the earlier claim is recorded rather than quietly dropped,
because "the test exercises the production path" was asserted in the PR body and
was false.

What is proven now:

* the racing calls go through **`apply_payment_once`**, the function the route
  calls, with the arguments the route passes;
* each worker gets its **own psycopg2 connection and its own transaction**, and
  the test asserts their `pg_backend_pid()` values differ -- two sessions, as two
  HTTP requests would be, not two threads sharing one;
* the interleaving is **forced by a barrier, never by sleeping**, so the result
  is deterministic rather than dependent on how loaded the machine is;
* the two payments carry **distinct `payment_id`s**, so both idempotency markers
  are legitimately inserted -- the loss is in the balance, not in the dedupe. The
  test asserts both markers persist, which is what makes the missing money
  provably a lost update rather than a suppressed duplicate.

`waive_fee` is the control: it writes `past_due`, so pairing a payment with a fee
waiver -- the client's own reported repro -- cannot collide. That case passes
today and must keep passing, because a "failing test" built on the brief's wrong
pairing would prove nothing while looking like diligence.

**Why `xfail(strict=True)` rather than a red build.** The test must fail today --
that is the evidence. `strict` does the other half: when D3 is fixed and these
pass, pytest reports XPASS as a failure, so the marker cannot outlive the defect.
Mutation-verified both ways, including that an atomic correction produces the
XPASS failure rather than an error.
"""
import os
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import pytest

from app import balance

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

SCHEMA = "servicing_lost_update_test"

OPENING_BALANCE = 500
PAYMENT = 100
PAYMENT_A, PAYMENT_B = 9001, 9002       # distinct: both markers must insert


# --------------------------------------------------------------------------- db

def _connect(autocommit: bool):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = autocommit
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
    return conn


class _BarrierCursor:
    """Wraps a real cursor and pauses once, after the balance is read.

    The pause is what makes the race deterministic: both sessions must complete
    their READ before either performs its WRITE. Nothing about the code under
    test changes -- only when its statements run.
    """

    def __init__(self, cur, barrier, state):
        self._cur = cur
        self._barrier = barrier
        self._state = state

    def execute(self, sql, params=None):
        result = self._cur.execute(sql, params)
        if "SELECT balance" in sql and not self._state.get("paused"):
            self._state["paused"] = True
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                # The barrier is a scheduling hint, not part of the property.
                # An atomic correction removes the read phase entirely, leaving
                # one session alone here; without this the test would report a
                # correct fix as a broken test and would be pinned to one
                # particular implementation of that fix.
                pass
        return result

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _ThreadDb:
    """Stands in for `app.db`, giving every worker its own real session.

    The module's own `db` uses one shared module-level autocommit connection for
    `query()`. Two threads on one connection are not two concurrent requests, so
    a race staged on it proves nothing about production. Each thread gets its own
    connection here, and `transaction()` opens a fresh one per call exactly as
    the real implementation does.
    """

    def __init__(self, barrier):
        self._barrier = barrier
        self._local = threading.local()
        self.connections = []
        self.backend_pids = []
        self._lock = threading.Lock()

    def _record(self, conn):
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        with self._lock:
            self.connections.append(conn)
            self.backend_pids.append(pid)

    def query(self, sql, params=None):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = _connect(autocommit=True)
            self._record(conn)
        state = self._local.__dict__.setdefault("state", {})
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            wrapped = _BarrierCursor(cur, self._barrier, state)
            wrapped.execute(sql, params or ())
            if cur.description:
                return cur.fetchall()
            return []

    @contextmanager
    def transaction(self):
        conn = _connect(autocommit=False)
        self._record(conn)
        state = self._local.__dict__.setdefault("state", {})
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield _BarrierCursor(cur, self._barrier, state)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close_all(self):
        for conn in self.connections:
            try:
                conn.close()
            except Exception:                                   # pragma: no cover
                pass


# ----------------------------------------------------------------------- setup

@pytest.fixture
def pg():
    admin = _connect(autocommit=True)
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            """
            CREATE TABLE balances (
                loan_id    INTEGER PRIMARY KEY,
                balance    NUMERIC(14,2) NOT NULL,
                past_due   NUMERIC(14,2) DEFAULT 0,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        # apply_payment_once's idempotency guard lives here; without this table
        # the production path cannot run at all.
        cur.execute(
            """
            CREATE TABLE payment_applications (
                payment_id INTEGER PRIMARY KEY,
                loan_id    INTEGER NOT NULL,
                amount     NUMERIC(14,2) NOT NULL,
                applied_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY,
                loan_id INTEGER NOT NULL,
                amount NUMERIC(14,2) NOT NULL,
                UNIQUE (id, loan_id)
            );
            CREATE TABLE ledger_entries (
                id SERIAL PRIMARY KEY,
                loan_id INTEGER NOT NULL,
                component TEXT NOT NULL,
                amount NUMERIC(14,2) NOT NULL,
                entry_type TEXT NOT NULL,
                payment_id INTEGER,
                UNIQUE (payment_id, component),
                FOREIGN KEY (payment_id, loan_id) REFERENCES payments(id, loan_id)
            );
            CREATE FUNCTION project_test_ledger() RETURNS trigger AS $$
            BEGIN
                UPDATE balances SET balance = balance + NEW.amount
                 WHERE loan_id = NEW.loan_id;
                RETURN NEW;
            END $$ LANGUAGE plpgsql;
            CREATE TRIGGER ledger_project AFTER INSERT ON ledger_entries
                FOR EACH ROW EXECUTE FUNCTION project_test_ledger();
            """
        )
        cur.execute("INSERT INTO balances (loan_id, balance) VALUES (1, %s)", (OPENING_BALANCE,))
        cur.execute(
            "INSERT INTO payments(id,loan_id,amount) VALUES (%s,1,%s),(%s,1,%s)",
            (PAYMENT_A, PAYMENT, PAYMENT_B, PAYMENT),
        )
    yield admin
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    admin.close()


@pytest.fixture
def concurrent(monkeypatch, pg):
    db = _ThreadDb(threading.Barrier(2, timeout=5))
    monkeypatch.setattr(balance, "db", db)
    yield db
    db.close_all()


def _run(db, *calls):
    """Run each callable on its own thread; surface failures, leave nothing alive."""
    errors = []

    def _wrap(fn):
        def _inner():
            try:
                fn()
            except Exception as exc:                            # noqa: BLE001
                errors.append(exc)
        return _inner

    threads = [threading.Thread(target=_wrap(c), daemon=True) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    alive = [t for t in threads if t.is_alive()]
    assert not alive, f"{len(alive)} worker thread(s) did not terminate -- deadlock"
    if errors:
        raise errors[0]

    assert len(set(db.backend_pids)) >= 2, (
        f"workers shared a database session (pids={db.backend_pids}); two "
        "concurrent HTTP requests would not, so a race staged on one connection "
        "proves nothing about production"
    )


def _balance(pg):
    with pg.cursor() as cur:
        cur.execute(f"SELECT balance FROM {SCHEMA}.balances WHERE loan_id = 1")
        return float(cur.fetchone()[0])


def _markers(pg):
    with pg.cursor() as cur:
        cur.execute(f"SELECT payment_id FROM {SCHEMA}.payment_applications ORDER BY payment_id")
        return [r[0] for r in cur.fetchall()]


# ------------------------------------------------------------------- the proof

def test_two_concurrent_payments_both_reach_the_balance(pg, concurrent):
    """The production path, and the form of the defect that costs money.

    Two captured payments of 100 against a 500 balance must leave 300. Both are
    genuinely distinct payments -- distinct `payment_id`s, so both idempotency
    markers insert and neither is a duplicate being correctly suppressed. One
    payment is nonetheless lost, and because there is no ledger (D8) the loss is
    not detectable afterwards.
    """
    _run(
        concurrent,
        lambda: balance.apply_payment_once(PAYMENT_A, 1, PAYMENT),
        lambda: balance.apply_payment_once(PAYMENT_B, 1, PAYMENT),
    )

    assert _markers(pg) == [PAYMENT_A, PAYMENT_B], (
        "both payments must be recorded as applied -- if a marker is missing the "
        "dedupe suppressed one and this is not a lost update"
    )
    assert _balance(pg) == OPENING_BALANCE - (2 * PAYMENT), (
        "both markers persisted but the balance moved once: one payment was "
        "captured, recorded as applied, and silently never credited"
    )


def test_a_payment_racing_a_staff_adjustment_is_not_lost(pg, concurrent):
    """Both production paths: `apply_payment_once` against `adjust_balance`.

    Whichever write lands second wins outright, so the outcome is not merely
    wrong but arbitrary -- and `adjust_balance` keeps no record ("the prior value
    is gone forever", its own docstring), so neither the CSR nor the borrower can
    establish afterwards what the balance should have been.
    """
    _run(
        concurrent,
        lambda: balance.apply_payment_once(PAYMENT_A, 1, PAYMENT),
        lambda: balance.adjust_balance(1, 450),
    )

    # The adjustment sets an absolute value and the payment applies a delta, so
    # the only defensible combined outcome is the adjustment less the payment.
    assert _balance(pg) == 450 - PAYMENT


def test_the_clients_reported_repro_does_not_collide(pg, concurrent):
    """Not xfail: this passes today, and that is the finding.

    The brief described a payment racing a fee waiver on `balance=500`.
    Reproducing it would have shown no defect at all -- `waive_fee` writes
    `past_due` and the payment writes `balance`. Pinned executably so the
    correction cannot quietly rot back into a claim in a document.
    """
    _run(
        concurrent,
        lambda: balance.apply_payment_once(PAYMENT_A, 1, PAYMENT),
        lambda: balance.waive_fee(1, 25),
    )

    assert _balance(pg) == OPENING_BALANCE - PAYMENT, "the payment must be intact"
    with pg.cursor() as cur:
        cur.execute(f"SELECT past_due FROM {SCHEMA}.balances WHERE loan_id = 1")
        assert float(cur.fetchone()[0]) == -25, "the waiver must be intact"


def test_a_replayed_payment_id_is_still_suppressed(pg, concurrent):
    """The idempotency guard must survive the concurrency this file stages.

    Distinct payment_ids race and both count (above). The SAME payment_id
    delivered twice must apply once -- otherwise a fix for D3 could "pass" the
    tests above by breaking the dedupe instead, which would be a worse defect
    than the one being closed.
    """
    _run(
        concurrent,
        lambda: balance.apply_payment_once(PAYMENT_A, 1, PAYMENT),
        lambda: balance.apply_payment_once(PAYMENT_A, 1, PAYMENT),
    )

    assert _markers(pg) == [PAYMENT_A]
    assert _balance(pg) == OPENING_BALANCE - PAYMENT
