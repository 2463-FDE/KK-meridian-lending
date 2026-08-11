"""D3, proven: two concurrent payments against one loan lose one of them.

Week 6's brief asked for a legacy-comprehension report, characterization tests,
**one failing test proving the correctly-paired lost update**, and an ADR. Every
part shipped except the failing test, so `DEBT.md` D3 has sat "Open" for five
weeks as an assertion nobody could run. This is that test.

**The client's own reported repro is wrong, and reproducing it would have proved
nothing.** They described a payment and a concurrent fee waiver both reading
`balance=500` and both writing. But `apply_payment` writes `balance` and
`waive_fee` writes `past_due` -- different columns, so that exact pairing never
collided. `assess_late_fee` writes `past_due` too. The real lost update needs two
writers on the *same* column: two `apply_payment` calls, or `apply_payment`
racing `adjust_balance`. Both are exercised below.

**Why this is `xfail(strict=True)` rather than a red build.** The test must fail
today -- that is the entire point, it is evidence the defect is real rather than
argued. `strict=True` then does the other half of the job: if D3 is ever fixed and
this starts passing, pytest reports XPASS as a **failure**, so the marker cannot
outlive the defect it documents. A plain `skip` would rot silently; a plain
failing test would make CI permanently red and train people to ignore it.

**Why real Postgres.** The property under test is what two database sessions do
to one row when their statements interleave. A mocked cursor cannot exhibit it --
it would assert only that the Python arithmetic is right, which it is. The bug is
not in the arithmetic.

**Why the interleaving is forced rather than raced.** Sleeping and hoping for a
collision produces a test that passes on a fast machine and fails in CI, which is
worse than no test. A barrier makes the schedule deterministic: both sessions
complete their READ before either performs its WRITE. Note what is *not* changed
to achieve that -- `apply_payment` is called exactly as production calls it, and
its own logic is untouched. Only the timing between its statements is controlled,
which is the difference between scheduling a real function and re-implementing it.
Re-implementing the read-modify-write here and racing that instead would prove
only that the test author can write a race.

**Mutation-verified, and the mutation found a real defect in this file.** An
`xfail` that can never pass is worthless -- it looks like evidence and asserts
nothing -- so D3 was temporarily closed with an atomic
`UPDATE balances SET balance = balance - %s` and this file re-run. The first
attempt did NOT report the fix: removing the read phase left one session waiting
alone at the barrier, so the test died with `BrokenBarrierError` instead of
passing, which would have reported a correct fix as a broken test and tied this
file to one particular implementation of that fix. The barrier is now tolerant of
that (see `scheduled_query`), and with the mutation applied the payment case
reports `XPASS(strict)` -- pytest failing the run to demand the marker's removal,
which is exactly the signal wanted. Reverted afterwards; `balance.py` is
unchanged by this PR.
"""
import os
import threading

import psycopg2
import psycopg2.extras
import pytest

from app import balance

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

SCHEMA = "servicing_lost_update_test"

# The starting balance and payment size are chosen so the two possible outcomes
# are unmistakable rather than off-by-a-cent: 500 - 100 - 100 = 300 if both
# payments land, 400 if one is lost. No rounding is involved in either.
OPENING_BALANCE = 500
PAYMENT = 100


def _fresh_schema(conn):
    """Only the columns this defect lives in, matching db/init/001_schema.sql."""
    with conn.cursor() as cur:
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
        cur.execute(
            "INSERT INTO balances (loan_id, balance) VALUES (%s, %s)",
            (1, OPENING_BALANCE),
        )
    conn.commit()


@pytest.fixture
def pg():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    _fresh_schema(conn)
    conn.autocommit = True
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


@pytest.fixture
def interleaved(monkeypatch, pg):
    """Point `balance` at the test schema and force both READs before either WRITE.

    The wrapper is deliberately thin: it forwards every statement to the real
    `db.query` and only pauses *after* a balance SELECT returns, at which point
    the cursor is already closed. Nothing about `apply_payment`'s own behaviour
    changes -- it still reads, computes in Decimal, and writes exactly as it does
    in production.
    """
    barrier = threading.Barrier(2, timeout=5)
    real_query = balance.db.query
    seen_read = threading.local()

    def scheduled_query(sql, params=None):
        rows = real_query(f"SET search_path TO {SCHEMA}; {sql}", params)
        # Any READ of the balances row, not just `SELECT balance` -- `waive_fee`
        # reads `past_due` from the same row, and gating on the column name meant
        # only one of the two sessions ever reached the barrier, so it timed out
        # instead of interleaving. The schedule has to be defined by "has read
        # the row", which is what both writers actually do first.
        is_read = sql.lstrip().upper().startswith("SELECT") and "balances" in sql
        if is_read and not getattr(seen_read, "done", False):
            seen_read.done = True
            try:
                barrier.wait()  # hold here until the other session has read too
            except threading.BrokenBarrierError:
                # The barrier is a scheduling HINT that makes the race
                # deterministic, not a requirement of the property under test.
                #
                # Found by mutation: closing D3 with an atomic
                # `UPDATE ... SET balance = balance - %s` removes the read phase
                # entirely, so only one session ever arrives here and the barrier
                # times out. Without this except, the test would then fail with
                # BrokenBarrierError instead of passing -- reporting the *fix* as
                # a broken test, and pinning the test to one particular
                # implementation of the fix rather than to the outcome.
                #
                # Proceeding is safe: a writer that does not read cannot lose an
                # update by interleaving, so the assertion below is the arbiter
                # either way.
                pass
        return rows

    monkeypatch.setattr(balance.db, "query", scheduled_query)
    return barrier


def _run_concurrently(*calls):
    """Run each callable on its own thread and re-raise the first failure."""
    errors = []

    def _wrap(fn):
        def _inner():
            try:
                fn()
            except Exception as exc:                       # noqa: BLE001
                errors.append(exc)
        return _inner

    threads = [threading.Thread(target=_wrap(c)) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    if errors:
        raise errors[0]


def _balance(pg):
    with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT balance FROM {SCHEMA}.balances WHERE loan_id = 1")
        return float(cur.fetchone()["balance"])


@pytest.mark.xfail(
    strict=True,
    reason="DEBT.md D3: balance.apply_payment is an unlocked read-modify-write. "
           "When this XPASSes, D3 is fixed -- delete the marker, do not delete "
           "the test.",
)
def test_two_concurrent_payments_both_reach_the_balance(pg, interleaved):
    """The money question: a borrower pays twice, both payments must count.

    This is the defect in the form that costs someone money. Two payments of 100
    against a 500 balance must leave 300. Today one of them is silently
    discarded, the borrower is billed for it, and nothing anywhere records that
    it happened -- there is no ledger, so the loss is not even detectable after
    the fact (D8).
    """
    _run_concurrently(
        lambda: balance.apply_payment(1, PAYMENT),
        lambda: balance.apply_payment(1, PAYMENT),
    )

    assert _balance(pg) == OPENING_BALANCE - (2 * PAYMENT), (
        "one payment was lost: both sessions read the same starting balance and "
        "the second write overwrote the first"
    )


@pytest.mark.xfail(
    strict=True,
    reason="DEBT.md D3: same unlocked read-modify-write, reached via adjust_balance.",
)
def test_a_payment_racing_a_staff_adjustment_is_not_lost(pg, interleaved):
    """The second correctly-paired case, and the one with an audit consequence.

    A CSR adjusts a balance to 450 while a 100 payment posts. Whichever write
    lands second wins outright, so the outcome is not merely wrong, it is
    *arbitrary* -- and because `adjust_balance` keeps no record ("the prior value
    is gone forever", its own docstring), neither the CSR nor the borrower can
    later establish what the balance should have been.
    """
    _run_concurrently(
        lambda: balance.apply_payment(1, PAYMENT),
        lambda: balance.adjust_balance(1, 450),
    )

    # The adjustment is an absolute set and the payment a relative delta, so the
    # only defensible combined outcome is the adjustment less the payment.
    assert _balance(pg) == 450 - PAYMENT


def test_the_clients_reported_repro_does_not_collide(pg, interleaved):
    """Not xfail: this one passes today, and that is the finding.

    The brief described a payment and a fee waiver racing on `balance=500`.
    Reproducing it would have shown no defect at all, because `waive_fee` writes
    `past_due` and `apply_payment` writes `balance`. Shipping a "failing test"
    built on this pairing would have proved nothing while looking like diligence,
    so it is pinned here to keep the correction executable rather than a claim in
    a document.
    """
    _run_concurrently(
        lambda: balance.apply_payment(1, PAYMENT),
        lambda: balance.waive_fee(1, 25),
    )

    assert _balance(pg) == OPENING_BALANCE - PAYMENT, "the payment must be intact"

    with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT past_due FROM {SCHEMA}.balances WHERE loan_id = 1")
        assert float(cur.fetchone()["past_due"]) == -25, "the waiver must be intact"
