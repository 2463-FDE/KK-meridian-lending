"""ADR 0011 step 1: what the maker-checker schema guarantees on its own.

**This schema does not close D8.** No application writer creates a proposal yet,
so `adjust-balance` and `waive-fee` still move money on one person's say-so. What
is proven here is narrower and worth having first: that when the cutover does
write these rows, the database will not let it get the control subtly wrong.

Every case runs against real PostgreSQL. A deferred constraint trigger that does
not fire, a `FOR SHARE` that does not block, and a `BEFORE INSERT` trigger that
silently does not overwrite the actor are all invisible to a mock — and each of
those is one of the invariants below.

**What is deliberately NOT tested here, because it does not exist yet:**
`resolve_pending_movement`, the proposal and approval endpoints, and the
configured limits (`MAKER_CHECKER_ADMIN_THRESHOLD`, `MAKER_CHECKER_MAX_DELTA`).
The limits are human-approved configuration read at runtime, not database facts;
this schema states no figure of its own, and a test asserting one here would be
inventing policy the database was deliberately not given.
"""
import os
import pathlib

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[2]
INIT = REPO / "db" / "init"
SCHEMA = "pending_movements_test"

#: The fresh-install files, in the order a new database runs them.
INIT_FILES = ("001_schema.sql", "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql", "007_ledger_opening_balances.sql")


@pytest.fixture
def db():
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
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
    conn.commit()
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.commit()
    conn.close()


def _cursor(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SET search_path TO {SCHEMA}")
    return cur


@pytest.fixture
def loan(db):
    """A serviced loan with a balances row -- the target a proposal needs."""
    with _cursor(db) as cur:
        cur.execute("INSERT INTO applicants (name) VALUES ('Test Borrower') RETURNING id")
        applicant = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO applications (applicant_id, amount, term_months, status) "
            "VALUES (%s, 5000, 24, 'funded') RETURNING id", (applicant,))
        application = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO loans (app_id, applicant_name, principal, note_rate_pct, term_months, status) "
            "VALUES (%s, 'Test Borrower', 5000.00, 7.990, 24, 'current') RETURNING id",
            (application,))
        loan_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO balances (loan_id, balance, past_due) VALUES (%s, 5000.00, 80.00)",
            (loan_id,))
    db.commit()
    return loan_id


def _propose(conn, loan_id, *, component="principal", amount="-250.00",
             entry_type="adjustment", requested_by=1, requested_role="csr",
             reason="borrower overcharged on a fee reversal"):
    with _cursor(conn) as cur:
        cur.execute(
            "INSERT INTO pending_movements "
            "(loan_id, component, amount, entry_type, reason, requested_by, requested_role) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (loan_id, component, amount, entry_type, reason, requested_by, requested_role))
        return cur.fetchone()["id"]


def _approve(conn, movement_id, *, resolver=2, role="underwriter", threshold="500.00"):
    """Mark approved. Does NOT insert the entry -- callers do that explicitly, in
    the order the triggers require, because the order is part of what is tested."""
    with _cursor(conn) as cur:
        cur.execute(
            "UPDATE pending_movements SET resolution = 'approved', resolved_by = %s, "
            "resolved_role = %s, resolved_at = now(), resolved_threshold = %s "
            "WHERE id = %s", (resolver, role, threshold, movement_id))


def _insert_entry(conn, movement_id, loan_id, *, component="principal",
                  amount="-250.00", entry_type="adjustment", actor_id=None,
                  actor_role=None):
    with _cursor(conn) as cur:
        cur.execute(
            "INSERT INTO ledger_entries "
            "(loan_id, component, amount, entry_type, reason, actor_id, actor_role, "
            " pending_movement_id) "
            "VALUES (%s, %s, %s, %s, 'approved adjustment', %s, %s, %s) RETURNING id",
            (loan_id, component, amount, entry_type, actor_id, actor_role, movement_id))
        return cur.fetchone()["id"]


# --- the sequence the design permits, end to end ------------------------------


def test_a_proposal_moves_no_money(db, loan):
    """The first property the control depends on: proposing is not doing.

    Counted as a DELTA rather than an absolute. Seeding the loan writes a
    `balances` row, and 0035's compatibility bridge captures that as a
    `legacy_direct_write` entry -- so "the ledger is empty" is false before this
    test does anything, and asserting it would have failed for a reason that has
    nothing to do with proposals.
    """
    with _cursor(db) as cur:
        cur.execute("SELECT balance FROM balances WHERE loan_id = %s", (loan,))
        before = cur.fetchone()["balance"]
        cur.execute("SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s", (loan,))
        entries_before = cur.fetchone()["n"]

    _propose(db, loan)
    db.commit()

    with _cursor(db) as cur:
        cur.execute("SELECT balance FROM balances WHERE loan_id = %s", (loan,))
        assert cur.fetchone()["balance"] == before, "raising a proposal moved the balance"
        cur.execute("SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s", (loan,))
        assert cur.fetchone()["n"] == entries_before, (
            "raising a proposal wrote a ledger entry -- proposing must not be doing"
        )


def test_an_approval_produces_exactly_one_entry_and_moves_the_balance(db, loan):
    movement = _propose(db, loan)
    _approve(db, movement)
    entry = _insert_entry(db, movement, loan)
    with _cursor(db) as cur:
        cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                    (entry, movement))
    db.commit()

    with _cursor(db) as cur:
        cur.execute("SELECT count(*) AS n FROM ledger_entries WHERE pending_movement_id = %s",
                    (movement,))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT balance FROM balances WHERE loan_id = %s", (loan,))
        # 5000.00 - 250.00, moved by the ledger projection rather than by the API.
        assert cur.fetchone()["balance"] == pytest.approx(4750.00)


def test_a_rejection_writes_no_entry_and_the_proposal_is_kept(db, loan):
    movement = _propose(db, loan)
    with _cursor(db) as cur:
        cur.execute(
            "UPDATE pending_movements SET resolution = 'rejected', resolved_by = 2, "
            "resolved_role = 'underwriter', resolved_at = now(), "
            "resolved_threshold = 500.00 WHERE id = %s", (movement,))
    db.commit()

    with _cursor(db) as cur:
        cur.execute("SELECT count(*) AS n FROM ledger_entries WHERE pending_movement_id = %s",
                    (movement,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT resolution, reason FROM pending_movements WHERE id = %s",
                    (movement,))
        row = cur.fetchone()
        assert row["resolution"] == "rejected"
        assert row["reason"], "the rejected proposal lost the reason it was raised with"


# --- invariant 3: no self-approval --------------------------------------------


def test_the_requester_cannot_approve_their_own_proposal(db, loan):
    movement = _propose(db, loan, requested_by=7)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _approve(db, movement, resolver=7)
    db.rollback()


def test_an_entry_whose_proposal_has_no_distinct_approver_is_refused(db, loan):
    """The money path re-checks rather than trusting the table constraint.

    Constraints get dropped. This is the insert that moves the balance, so it
    asks the proposal directly.
    """
    movement = _propose(db, loan, requested_by=7)
    db.commit()
    # Force the row past the table CHECK the only way possible -- by dropping it --
    # to prove the trigger is a second, independent guard rather than a comment.
    with _cursor(db) as cur:
        cur.execute("ALTER TABLE pending_movements DROP CONSTRAINT no_self_approval")
    _approve(db, movement, resolver=7)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _insert_entry(db, movement, loan)
    assert "no distinct approver" in str(exc.value)
    db.rollback()


# --- invariants 1 and 2: immutability and a single transition ------------------


@pytest.mark.parametrize("column, value", [
    ("loan_id", 999999), ("component", "'fees'"), ("amount", "-999.00"),
    ("entry_type", "'fee_waived'"), ("reason", "'rewritten after the fact'"),
    ("requested_by", 42), ("requested_role", "'admin'"),
    ("requested_at", "now() - interval '5 days'"),
])
def test_the_substance_of_a_proposal_is_immutable(db, loan, column, value):
    """`reason` and `requested_at` are in this list on purpose.

    An earlier ADR revision froze only the money fields, which left *why* a
    movement was requested rewritable after a second person approved the reason
    they were shown. The reason is the evidence D8 says is missing.
    """
    movement = _propose(db, loan)
    db.commit()
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        with _cursor(db) as cur:
            cur.execute(f"UPDATE pending_movements SET {column} = {value} WHERE id = %s",
                        (movement,))
    assert "immutable" in str(exc.value)
    db.rollback()


def test_a_resolved_proposal_cannot_be_resolved_again(db, loan):
    movement = _propose(db, loan)
    _approve(db, movement, resolver=2)
    entry = _insert_entry(db, movement, loan)
    with _cursor(db) as cur:
        cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                    (entry, movement))
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException):
        with _cursor(db) as cur:
            cur.execute(
                "UPDATE pending_movements SET resolution = 'rejected', resolved_by = 3, "
                "resolved_role = 'admin', resolved_at = now(), "
                "resolved_threshold = 500.00 WHERE id = %s", (movement,))
    db.rollback()


def test_the_ledger_link_may_be_filled_once_and_never_swapped(db, loan):
    movement = _propose(db, loan)
    _approve(db, movement)
    first = _insert_entry(db, movement, loan)
    with _cursor(db) as cur:
        cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                    (first, movement))
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        with _cursor(db) as cur:
            cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                        (first + 1, movement))
    assert "already linked" in str(exc.value)
    db.rollback()


def test_a_rejected_proposal_cannot_gain_an_entry(db, loan):
    movement = _propose(db, loan)
    with _cursor(db) as cur:
        cur.execute(
            "UPDATE pending_movements SET resolution = 'rejected', resolved_by = 2, "
            "resolved_role = 'underwriter', resolved_at = now(), "
            "resolved_threshold = 500.00 WHERE id = %s", (movement,))
    db.commit()
    with pytest.raises(psycopg2.errors.RaiseException):
        _insert_entry(db, movement, loan)
    db.rollback()


# --- invariant 4: enforced at COMMIT, because the intermediate state is legal ---


def test_an_approval_without_an_entry_fails_at_commit(db, loan):
    """The state is legal mid-transaction and illegal at COMMIT.

    This is why it is a deferred constraint trigger and not a CHECK: the entry is
    inserted after the row is marked approved, so an immediate check would fail on
    a state the design requires to exist for a moment.
    """
    movement = _propose(db, loan)
    _approve(db, movement)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        db.commit()
    assert "has no ledger entry" in str(exc.value)
    db.rollback()


def test_the_intermediate_state_really_is_allowed_before_commit(db, loan):
    """Guards the guard above: if marking approved failed immediately, the test
    would pass for the wrong reason and the real sequence would be impossible."""
    movement = _propose(db, loan)
    _approve(db, movement)
    with _cursor(db) as cur:
        cur.execute("SELECT resolution, ledger_entry_id FROM pending_movements WHERE id = %s",
                    (movement,))
        row = cur.fetchone()
    assert row["resolution"] == "approved" and row["ledger_entry_id"] is None
    entry = _insert_entry(db, movement, loan)
    with _cursor(db) as cur:
        cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                    (entry, movement))
    db.commit()


# --- invariant 5: retention ----------------------------------------------------


@pytest.mark.parametrize("state", ["pending", "rejected"])
def test_a_proposal_can_never_be_deleted(db, loan, state):
    movement = _propose(db, loan)
    if state == "rejected":
        with _cursor(db) as cur:
            cur.execute(
                "UPDATE pending_movements SET resolution = 'rejected', resolved_by = 2, "
                "resolved_role = 'underwriter', resolved_at = now(), "
                "resolved_threshold = 500.00 WHERE id = %s", (movement,))
    db.commit()
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        with _cursor(db) as cur:
            cur.execute("DELETE FROM pending_movements WHERE id = %s", (movement,))
    assert "may not be deleted" in str(exc.value)
    db.rollback()


# --- invariants 6 and 7: the entry must BE what was approved --------------------


@pytest.mark.parametrize("field, value", [
    ("loan_id", "OTHER"), ("component", "'fees'"), ("amount", "-999.00"),
])
def test_an_entry_that_differs_from_its_proposal_is_refused(db, loan, field, value):
    """An approval may not authorise different terms than the ones reviewed."""
    movement = _propose(db, loan)
    _approve(db, movement)
    kwargs = {}
    if field == "loan_id":
        with _cursor(db) as cur:
            cur.execute("INSERT INTO applicants (name) VALUES ('Other') RETURNING id")
            other_applicant = cur.fetchone()["id"]
            cur.execute("INSERT INTO applications (applicant_id, amount, term_months, status) "
                        "VALUES (%s, 1000, 12, 'funded') RETURNING id", (other_applicant,))
            other_app = cur.fetchone()["id"]
            cur.execute("INSERT INTO loans (app_id, applicant_name, principal, note_rate_pct, "
                        "term_months, status) "
                        "VALUES (%s, 'Other', 1000.00, 7.990, 12, 'current') RETURNING id",
                        (other_app,))
            other_loan = cur.fetchone()["id"]
            cur.execute("INSERT INTO balances (loan_id, balance) VALUES (%s, 1000.00)",
                        (other_loan,))
        target = other_loan
        kwargs = {}
    else:
        target = loan
        kwargs = {field: value.strip("'")}

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _insert_entry(db, movement, target, **kwargs)
    assert "does not match pending movement" in str(exc.value)
    db.rollback()


def test_the_entry_actor_is_the_approver_not_the_caller(db, loan):
    """Overwritten, not validated.

    A caller reproducing every other field correctly must not get to choose who
    is credited with authorising the movement -- that field is the whole audit
    answer to "who allowed it".
    """
    movement = _propose(db, loan, requested_by=1, requested_role="csr")
    _approve(db, movement, resolver=99, role="admin")
    entry = _insert_entry(db, movement, loan, actor_id=1, actor_role="csr")
    with _cursor(db) as cur:
        cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                    (entry, movement))
        cur.execute("SELECT actor_id, actor_role FROM ledger_entries WHERE id = %s", (entry,))
        row = cur.fetchone()
    db.commit()
    assert row["actor_id"] == 99 and row["actor_role"] == "admin", (
        "the caller's claimed actor survived -- the ledger would credit the "
        "requester with approving their own movement"
    )


def test_an_adjustment_entry_without_a_proposal_is_refused(db, loan):
    """The maker-checker subjects may only enter the ledger through an approval.

    Two guards refuse this and the TRIGGER wins the race -- it runs BEFORE INSERT,
    while `approved_entries_have_a_proposal` is checked after the row is built.
    Either refusal is correct; the trigger's is the more useful one, because it
    names what is missing rather than the constraint that noticed.
    """
    with pytest.raises((psycopg2.errors.RaiseException,
                        psycopg2.errors.CheckViolation)) as exc:
        with _cursor(db) as cur:
            cur.execute(
                "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, actor_id, "
                "actor_role) VALUES (%s, 'principal', -100.00, 'adjustment', 1, 'csr')",
                (loan,))
    assert "must name the proposal" in str(exc.value) or "approved_entries" in str(exc.value)
    db.rollback()


def test_a_machine_entry_may_not_claim_a_proposal(db, loan):
    """A payment cannot be dressed up as an approved adjustment."""
    movement = _propose(db, loan)
    db.commit()
    with pytest.raises(psycopg2.errors.CheckViolation):
        with _cursor(db) as cur:
            cur.execute(
                "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                "pending_movement_id) VALUES (%s, 'principal', -50.00, 'payment', %s)",
                (loan, movement))
    db.rollback()


def test_one_approval_cannot_yield_two_entries(db, loan):
    movement = _propose(db, loan)
    _approve(db, movement)
    first = _insert_entry(db, movement, loan)
    with _cursor(db) as cur:
        cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                    (first, movement))
    # UNIQUE on pending_movement_id is what forbids the second entry. The
    # proposal-matching trigger passes it -- the terms are identical, which is
    # precisely why a uniqueness rule is needed and matching is not enough.
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _insert_entry(db, movement, loan)
    db.rollback()


# --- the schema states no policy figure ----------------------------------------


def test_the_schema_hardcodes_no_configured_limit():
    """The approved threshold and cap are configuration, not database facts.

    Baking either into a CHECK would make a policy change a migration and would
    freeze a cohort/demo figure into the shape of the data. They are read from
    the environment at runtime and fail closed when unset.
    """
    sql = (REPO / "db" / "migrations" / "0036_pending_movements.sql").read_text(encoding="utf-8")
    fresh = (REPO / "db" / "init" / "001_schema.sql").read_text(encoding="utf-8")
    for text, name in ((sql, "0036"), (fresh, "001_schema")):
        pending = text[text.index("CREATE TABLE IF NOT EXISTS pending_movements"):]
        for figure in ("500.00", "500)", "5000.00", "5000)"):
            assert figure not in pending.split("COMMIT")[0], (
                f"{name} encodes the configured limit {figure} in the schema"
            )


# --- review round 1: the four findings, each with the case that found it -------


def test_a_resolution_cannot_attach_a_ledger_entry_in_the_same_statement(db, loan):
    """PM-LINK-001, the blocker.

    The transition trigger only restricted `ledger_entry_id` once the row was
    ALREADY resolved. On the first resolution a caller could set `resolution`,
    the resolver fields and `ledger_entry_id` together -- and nothing checked
    that the entry it named belonged to this proposal. Invariants 6 and 7 were
    enforced on the entry's way in and not on the proposal's way out.

    Requiring a separate update is what makes the reciprocal checkable: the
    entry must exist first, and by then it carries a `pending_movement_id`.
    """
    other = _propose(db, loan)
    _approve(db, other)
    other_entry = _insert_entry(db, other, loan)
    with _cursor(db) as cur:
        cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                    (other_entry, other))
    db.commit()

    movement = _propose(db, loan)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        with _cursor(db) as cur:
            cur.execute(
                "UPDATE pending_movements SET resolution = 'approved', resolved_by = 2, "
                "resolved_role = 'underwriter', resolved_at = now(), "
                "resolved_threshold = 500.00, ledger_entry_id = %s WHERE id = %s",
                (other_entry, movement))
    assert "same statement that resolves it" in str(exc.value)
    db.rollback()


def test_a_proposal_cannot_link_another_proposals_entry(db, loan):
    """PM-LINK-001, the half the deferred check owns.

    Non-null was not enough. Attaching a foreign entry in a separate update now
    fails at COMMIT, when the reciprocal is verified in both directions.
    """
    first = _propose(db, loan)
    _approve(db, first)
    first_entry = _insert_entry(db, first, loan)
    with _cursor(db) as cur:
        cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                    (first_entry, first))
    db.commit()

    second = _propose(db, loan)
    _approve(db, second, resolver=3, role="admin")
    with _cursor(db) as cur:
        cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                    (first_entry, second))
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        db.commit()
    assert "does not name it" in str(exc.value)
    db.rollback()


@pytest.mark.parametrize("reason", ["", "   ", "\t", "\n  \t "])
def test_a_blank_reason_is_refused(db, loan, reason):
    """PM-REASON-001. `NOT NULL` admits '' and whitespace.

    The reason is the evidence D8 names as missing; an empty one is the same
    absence wearing a value. Spec 0002 AC-17 refuses absent, empty and
    whitespace-only alike, so the constraint matches on a non-space character
    rather than trimming spaces only.
    """
    with pytest.raises(psycopg2.errors.CheckViolation) as exc:
        _propose(db, loan, reason=reason)
    assert "pending_reason_not_blank" in str(exc.value)
    db.rollback()


def test_a_resolution_must_record_the_threshold_it_was_judged_against(db, loan):
    """PM-THRESHOLD-001. Spec 0002 AC-22.

    The column existed and was nullable, so a resolution could commit recording
    no bar at all -- which is precisely what the column exists to prevent. A
    history of approvals is unreadable if the limit moved and nothing says when.
    """
    movement = _propose(db, loan)
    with pytest.raises(psycopg2.errors.CheckViolation) as exc:
        with _cursor(db) as cur:
            cur.execute(
                "UPDATE pending_movements SET resolution = 'approved', resolved_by = 2, "
                "resolved_role = 'underwriter', resolved_at = now() WHERE id = %s",
                (movement,))
    assert "resolution_complete" in str(exc.value)
    db.rollback()


def test_an_unresolved_proposal_may_not_carry_a_threshold(db, loan):
    """The other direction: a bar recorded before anyone judged against it is a
    number with no decision behind it."""
    with pytest.raises(psycopg2.errors.CheckViolation):
        with _cursor(db) as cur:
            cur.execute(
                "INSERT INTO pending_movements (loan_id, component, amount, entry_type, "
                "reason, requested_by, requested_role, resolved_threshold) "
                "VALUES (%s, 'principal', -10.00, 'adjustment', 'premature bar', 1, "
                "'csr', 500.00)", (loan,))
    db.rollback()


@pytest.mark.parametrize("component, amount, entry_type, constraint", [
    ("principal", "0", "adjustment", "pending_amount_nonzero"),
    ("fees", "10.00", "fee_waived", "pending_fee_waiver_reduces"),
    ("interest", "10.00", "adjustment", "pending_component"),
    ("principal", "-10.00", "fee_waived", "pending_"),
])
def test_a_proposal_the_ledger_could_never_execute_is_refused_at_creation(
        db, loan, component, amount, entry_type, constraint):
    """PM-TERMS-001.

    The queue accepted proposals the ledger can never write -- a zero movement, a
    positive waiver, an adjustment against interest -- which would have failed at
    execution, after a second person had reviewed and accepted them. ADR 0011 §3b
    is explicit that an approver should never be shown a request the system was
    always going to reject.

    These mirror ADR 0010's executable ledger constraints, so the refusal happens
    where the requester can act on it.
    """
    with pytest.raises(psycopg2.errors.CheckViolation) as exc:
        _propose(db, loan, component=component, amount=amount, entry_type=entry_type)
    assert constraint in str(exc.value)
    db.rollback()


def test_the_proposals_the_ledger_CAN_execute_are_still_accepted(db, loan):
    """Guards the four refusals above: a rule that rejected everything would
    satisfy all of them and stop the control working entirely."""
    for component, amount, entry_type in (
        ("principal", "-250.00", "adjustment"),
        ("principal", "250.00", "adjustment"),
        ("fees", "-40.00", "adjustment"),
        ("fees", "-40.00", "fee_waived"),
    ):
        movement = _propose(db, loan, component=component, amount=amount,
                            entry_type=entry_type)
        assert movement, f"{entry_type} on {component} for {amount} was refused"
    db.rollback()
