"""db/migrations/0048 -- a resolver's authority must be CURRENT when money moves.

G-02. `resolve_pending_movement` already re-reads the loan status and the
component balance inside its lock, on the stated grounds that a fact which was
true when a proposal was raised is not evidence about the state when money
moves. The resolver's own authority was the one such fact it did not re-read, so
an account deactivated after login could still approve a movement and write an
immutable `ledger_entries` row naming an approver whose authority had already
been withdrawn.

The gateway refuses a deactivated account too (`gateway/app/auth.py::get_session`)
and that is what closes the eight-hour session window. It cannot close the window
between its own check and this UPDATE. These cases are about the database half:
the part that holds when a deactivation is committed *concurrently* with the
approval.

Against real PostgreSQL, in a throwaway schema built from `db/init` and then
migrated with 0048 -- the same shape as `test_0046_one_late_fee_per_installment.py`.
That makes these migration-path tests as well as behaviour tests: `db/init`
carries the same function body (backported, as this repository does for every
migration), so applying 0048 on top proves the two agree and that the migration
is re-runnable rather than only correct once.
"""
import os
import pathlib
import threading
import time

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.getenv("DATABASE_URL")

REPO = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = "resolver_authority_test"
INIT = REPO / "db" / "init"
MIGRATION = REPO / "db" / "migrations" / "0048_resolver_authority_is_current.sql"
INIT_FILES = ("001_schema.sql", "002_seed.sql", "003_seed_bulk.sql",
              "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql",
              "007_ledger_opening_balances.sql")

#: Approved cohort/demo values, passed in the way servicing passes them. No
#: policy is asserted here -- these only have to be *a* threshold and *a*
#: permitted-status list so the function has something to judge against.
THRESHOLD = "500.00"
STATUSES = ["current"]


def _apply_migration(conn):
    """The migration as shipped, minus its own BEGIN/COMMIT.

    psycopg2 manages the transaction, and a nested BEGIN inside one is a warning
    and a lie about what is atomic. The statements are unchanged.
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    sql = sql.replace("BEGIN;", "", 1)
    sql = "".join(sql.rsplit("COMMIT;", 1))
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql)
    conn.commit()


def _connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
    conn.commit()
    return conn


@pytest.fixture(scope="module")
def db():
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.commit()
        for name in INIT_FILES:
            path = INIT / name
            if not path.exists():
                continue
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                cur.execute(path.read_text(encoding="utf-8"))
            conn.commit()
        _apply_migration(conn)
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.commit()
        conn.close()


@pytest.fixture()
def cur(db):
    """A cursor whose work is rolled back, so cases cannot leak into each other."""
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        yield c
    db.rollback()


# ---------------------------------------------------------------------------
# fixtures for the objects the function reads
# ---------------------------------------------------------------------------

def _a_loan(c):
    c.execute(
        "INSERT INTO loans (applicant_name, principal, note_rate_pct, term_months, "
        "                   regular_payment, regular_payment_count, final_payment, "
        "                   schedule_version, status) "
        "VALUES ('Authority Fixture', 15000.00, 7.99, 36, 469.98, 35, 469.87, "
        "        'B1', 'current') RETURNING id")
    loan_id = c.fetchone()["id"]
    c.execute("INSERT INTO balances (loan_id, balance, past_due) "
              "VALUES (%s, 15000.00, 250.00)", (loan_id,))
    return loan_id


def _a_user(c, role="admin", active=True, username=None):
    username = username or f"authfix_{role}_{time.time_ns()}"
    c.execute(
        "INSERT INTO users (username, password_hash, role, display_name, is_active) "
        "VALUES (%s, 'x', %s, %s, %s) RETURNING id",
        (username, role, f"Authority Fixture {role}", active))
    return c.fetchone()["id"]


def _a_proposal(c, loan_id, requester, requester_role="underwriter",
                component="fees", amount="-25.00", entry_type="fee_waived"):
    c.execute(
        "INSERT INTO pending_movements (loan_id, component, amount, entry_type, "
        "                               reason, requested_by, requested_role) "
        "VALUES (%s, %s, %s, %s, 'authority fixture', %s, %s) RETURNING id",
        (loan_id, component, amount, entry_type, requester, requester_role))
    return c.fetchone()["id"]


def _resolve(c, movement_id, resolver, resolver_role, resolution="approved"):
    c.execute("SELECT resolve_pending_movement(%s, %s, %s, %s, %s, %s) AS entry_id",
              (movement_id, resolver, resolver_role, resolution, THRESHOLD, STATUSES))
    return c.fetchone()["entry_id"]


def _ledger_count(c, loan_id):
    c.execute("SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s", (loan_id,))
    return c.fetchone()["n"]


# ---------------------------------------------------------------------------
# 1. The control holds for an account that IS current
# ---------------------------------------------------------------------------

def test_an_active_resolver_still_approves_and_writes_one_entry(cur):
    """The guard must refuse the right thing and nothing else.

    Stated first because a check that refuses everybody would pass every
    negative case below while breaking the feature entirely.
    """
    loan = _a_loan(cur)
    proposer = _a_user(cur, "underwriter")
    approver = _a_user(cur, "admin")
    movement = _a_proposal(cur, loan, proposer)
    before = _ledger_count(cur, loan)

    entry_id = _resolve(cur, movement, approver, "admin")

    assert entry_id is not None
    assert _ledger_count(cur, loan) == before + 1
    cur.execute("SELECT resolution, resolved_by, ledger_entry_id "
                "FROM pending_movements WHERE id = %s", (movement,))
    row = cur.fetchone()
    assert row["resolution"] == "approved"
    assert row["resolved_by"] == approver
    assert row["ledger_entry_id"] == entry_id


# ---------------------------------------------------------------------------
# 2. Deactivated, and the state-changing paths
# ---------------------------------------------------------------------------

def test_a_deactivated_resolver_cannot_approve(cur):
    """The case G-02 is about: authority withdrawn after the session was minted.

    The raise IS the no-ledger-entry assertion. `resolve_pending_movement` is
    one function in one transaction, so a `RAISE EXCEPTION` inside it aborts
    everything it had done -- there is no partial state to inspect afterwards,
    and a query attempting to look would only find the aborted transaction. That
    an approval and its ledger row are atomic is `test_0037`'s guarantee;
    relying on it here is the point rather than a shortcut.
    """
    loan = _a_loan(cur)
    proposer = _a_user(cur, "underwriter")
    approver = _a_user(cur, "admin", active=False)
    movement = _a_proposal(cur, loan, proposer)

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(cur, movement, approver, "admin")

    assert "current authority" in str(exc.value)
    assert str(approver) in str(exc.value), (
        "the refusal must name which account failed, so a route's bug is legible")


def test_the_refused_proposal_is_still_answerable_by_someone_with_authority(cur):
    """Refusing must not strand the row.

    Separate connection work is unnecessary: this shows the proposal survives a
    refusal by re-attempting it with an account that IS current, in a fresh
    transaction, and getting an approval.
    """
    loan = _a_loan(cur)
    proposer = _a_user(cur, "underwriter")
    stale = _a_user(cur, "admin", active=False)
    current = _a_user(cur, "admin")
    movement = _a_proposal(cur, loan, proposer)
    cur.execute("SAVEPOINT before_refusal")

    with pytest.raises(psycopg2.errors.RaiseException):
        _resolve(cur, movement, stale, "admin")
    cur.execute("ROLLBACK TO SAVEPOINT before_refusal")

    assert _resolve(cur, movement, current, "admin") is not None


def test_a_deactivated_resolver_cannot_reject_either(cur):
    """A rejection moves no money, and is still refused.

    Deliberate, and worth stating because the function treats approvals and
    rejections differently elsewhere: it skips the *target* revalidation for a
    rejection so a proposal against a closed loan stays answerable. That
    exception is about the target, not the person. Recording a rejection is
    still an authority act attributed to a named human, and an account whose
    authority has been withdrawn should not be the one recorded making it.
    """
    loan = _a_loan(cur)
    proposer = _a_user(cur, "underwriter")
    approver = _a_user(cur, "admin", active=False)
    movement = _a_proposal(cur, loan, proposer)

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(cur, movement, approver, "admin", resolution="rejected")

    assert "current authority" in str(exc.value)


def test_a_resolver_whose_role_changed_cannot_resolve_as_the_old_one(cur):
    """`resolved_role` is written from the caller's claim.

    So the role is re-read as well as the flag: recording `admin` for somebody
    who is now a csr would store evidence of an authority they do not hold, and
    the threshold that resolution was judged against was chosen for the role.
    Same reasoning as `manual_dti_is_permitted` (BDTI-02).
    """
    loan = _a_loan(cur)
    proposer = _a_user(cur, "underwriter")
    approver = _a_user(cur, "csr")            # actually a csr...
    movement = _a_proposal(cur, loan, proposer)

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(cur, movement, approver, "admin")   # ...claiming admin

    assert "current authority" in str(exc.value)


def test_an_unknown_resolver_is_refused_rather_than_assumed(cur):
    """Fail closed: an account that cannot be found is not a current one."""
    loan = _a_loan(cur)
    proposer = _a_user(cur, "underwriter")
    movement = _a_proposal(cur, loan, proposer)

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(cur, movement, 2_000_000_000, "admin")

    assert "current authority" in str(exc.value)


def test_self_approval_is_still_refused_before_authority_is_considered(cur):
    """Ordering: the older guarantee must not be weakened by the new check.

    An active proposer approving their own proposal must still be refused for
    being the proposer, not accidentally allowed because their account is fine.
    """
    loan = _a_loan(cur)
    proposer = _a_user(cur, "admin")
    movement = _a_proposal(cur, loan, proposer, requester_role="admin")

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(cur, movement, proposer, "admin")

    assert "may not" in str(exc.value) and "resolve it" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. What is deliberately NOT refused
# ---------------------------------------------------------------------------

def test_a_deactivated_PROPOSER_does_not_strand_the_proposal(cur):
    """Only the resolver is re-checked, and that is a decision rather than an omission.

    A proposal moves nothing. If raising one permanently bound the queue to the
    continued employment of whoever raised it, a proposal left behind by someone
    who has since left could be neither approved nor rejected -- the same
    for-ever-stuck row the rejection path is deliberately shaped to avoid.
    """
    loan = _a_loan(cur)
    proposer = _a_user(cur, "underwriter", active=False)
    approver = _a_user(cur, "admin")
    movement = _a_proposal(cur, loan, proposer)

    entry_id = _resolve(cur, movement, approver, "admin")

    assert entry_id is not None


# ---------------------------------------------------------------------------
# 4. The race the gateway cannot close
# ---------------------------------------------------------------------------

def test_a_deactivation_racing_an_approval_cannot_interleave(db):
    """Genuinely overlapping sessions, not a simulation.

    The point of putting this in the database is the window between the
    gateway's check and this UPDATE. Session A begins resolving and takes
    `FOR SHARE` on the resolver's row; session B tries to deactivate that
    account. B must WAIT rather than commit underneath A -- if it could, the
    ledger would carry an entry naming an approver who was already deactivated
    at the instant the money moved.

    Asserted the strict way: B is still blocked while A is uncommitted.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    # Own connections throughout. The module fixture's connection is shared with
    # the rolled-back cases above, and committing setup data through it would
    # leak fixtures into them.
    s = _connect()
    with s.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as setup:
        setup.execute(f"SET search_path TO {SCHEMA}")
        loan = _a_loan(setup)
        proposer = _a_user(setup, "underwriter")
        approver = _a_user(setup, "admin")
        movement = _a_proposal(setup, loan, proposer)
    s.commit()

    a = _connect()
    b = _connect()
    b_finished = threading.Event()
    b_error = []

    try:
        with a.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as ca:
            ca.execute(f"SET search_path TO {SCHEMA}")
            entry_id = _resolve(ca, movement, approver, "admin")
            assert entry_id is not None          # A has approved, NOT committed

            def deactivate():
                try:
                    with b.cursor() as cb:
                        cb.execute(f"SET search_path TO {SCHEMA}")
                        cb.execute("UPDATE users SET is_active = false WHERE id = %s",
                                   (approver,))
                    b.commit()
                except Exception as exc:          # noqa: BLE001 -- reported below
                    b_error.append(exc)
                finally:
                    b_finished.set()

            t = threading.Thread(target=deactivate, daemon=True)
            t.start()
            # THE ASSERTION. Still blocked while A holds the share lock.
            assert not b_finished.wait(timeout=2.0), (
                "the deactivation committed while the approval was still open -- "
                "FOR SHARE is not holding, so a resolver could be deactivated "
                "between the check and the ledger write")
            a.commit()

        assert b_finished.wait(timeout=10.0), "the deactivation never completed"
        assert not b_error, f"the deactivation failed: {b_error}"

        # Serialised order: the approval landed, then the deactivation. A second
        # approval by the same, now-deactivated, account is refused.
        with a.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as ca:
            ca.execute(f"SET search_path TO {SCHEMA}")
            second = _a_proposal(ca, loan, proposer)
            with pytest.raises(psycopg2.errors.RaiseException):
                _resolve(ca, second, approver, "admin")
        a.rollback()
    finally:
        a.rollback(); a.close()
        b.rollback(); b.close()
        # No row cleanup, deliberately. This case commits an approval, and the
        # entry it writes CANNOT be deleted -- `ledger_entries` is append-only
        # and the trigger refuses it, which is exactly the guarantee the rest of
        # the suite depends on. The whole schema is dropped in the module
        # fixture's teardown, so the rows go with it; every other case here
        # builds its own loan and rolls back, so nothing reads these.
        s.rollback()
        s.close()


# ---------------------------------------------------------------------------
# 5. The migration and db/init say the same thing
# ---------------------------------------------------------------------------

def test_the_migration_and_fresh_init_carry_the_same_check():
    """A fresh volume and a migrated database must agree.

    `db/init` is what a fresh volume gets; `db/migrations` upgrades an existing
    one. A check present in only one of them means the guarantee depends on how
    the database was built, which is the defect `test_migration_paths_converge`
    exists to catch generally and this pins for 0048 specifically.
    """
    init_sql = (INIT / "001_schema.sql").read_text(encoding="utf-8")
    migration_sql = MIGRATION.read_text(encoding="utf-8")
    needle = "does not hold current authority as"
    assert needle in init_sql, "db/init has no resolver-authority check"
    assert needle in migration_sql, "migration 0048 has no resolver-authority check"
    for sql in (init_sql, migration_sql):
        assert "FOR SHARE" in sql


def test_the_migration_is_rerunnable(db):
    """CREATE OR REPLACE, so applying it twice is a no-op rather than an error.

    Uses its own connection: re-applying commits, and doing that through the
    shared module connection would end the transaction the `cur` fixture relies
    on for isolation.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    conn = _connect()
    try:
        _apply_migration(conn)
        _apply_migration(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute(f"SET search_path TO {SCHEMA}")
            c.execute("SELECT prosrc FROM pg_proc p JOIN pg_namespace n "
                      "ON n.oid = p.pronamespace "
                      "WHERE p.proname = 'resolve_pending_movement' "
                      "AND n.nspname = %s", (SCHEMA,))
            bodies = [r["prosrc"] for r in c.fetchall()]
    finally:
        conn.rollback()
        conn.close()
    assert bodies, "the function is gone after re-applying the migration"
    assert any("current authority" in b for b in bodies)
