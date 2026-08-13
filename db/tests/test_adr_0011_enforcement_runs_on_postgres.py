"""ADR 0011's enforcement must actually commit, on real PostgreSQL.

The review finding this file exists for: the deferred constraint trigger read
`NEW`. PostgreSQL queues one deferred trigger event per UPDATE, each carrying the
row version *that event* produced, and fires every one of them at COMMIT. The
documented approval sequence updates the proposal twice -- once to resolve it,
once to link the entry it just inserted -- so the first event still arrived at
COMMIT holding `resolution = 'approved'` and `ledger_entry_id IS NULL`. The
trigger raised on **every** approval. Following the ADR would have blocked all
staff money movements, and the page looked correct.

That was the third time this document produced a rule that was right in isolation
and unsatisfiable next to the ones already there. So the SQL is no longer read for
plausibility: the blocks marked `<!-- executable: ... -->` in the ADR are
extracted **verbatim**, executed in document order against a real database, and
the approval transaction is then run exactly as the ADR prescribes.

Two properties make this a regression test rather than a demonstration:

* `test_the_new_reading_trigger_would_have_failed` re-installs the reported
  version of the trigger and asserts the same transaction cannot commit. Without
  it, a test that only proves the current design works would also pass on the
  broken one if the sequence happened to change.
* `test_every_executable_block_was_run` asserts the extractor found all of them,
  so a block silently renamed or dropped cannot make this file vacuously green.

`resolve_pending_movement()` is deliberately NOT executed: it is a signature in
the ADR, its body belongs with the migration that creates it, and the sequence it
would perform is what these tests perform directly.
"""
import os
import pathlib
import re

import psycopg2
import psycopg2.errors
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[2]
ADR = REPO / "adr" / "0011-maker-checker-for-servicing-adjustments.md"
SCHEMA = "adr_0011_enforcement_test"

# Every block the ADR marks executable, in the order it must run.
EXPECTED_BLOCKS = [
    "1-pending-movements",
    "2-ledger-entries-link",
    "3-single-transition",
    "4-retention",
    "5-resolution-complete",
    "6-entry-matches-proposal",
]

MAKER, CHECKER = 7001, 7002

# The parts of ADR 0010 this ADR builds on. Reduced to what 0011's constraints
# actually reference -- this file tests 0011, and standing up 0010's projection
# trigger and back-fill here would make a failure ambiguous between the two.
FIXTURE = """
CREATE TABLE loans (
    id             SERIAL PRIMARY KEY,
    applicant_name TEXT,
    principal      NUMERIC(14,2),
    apr            NUMERIC(6,3),
    term_months    INTEGER
);

CREATE TABLE payments (
    id      SERIAL PRIMARY KEY,
    loan_id INTEGER REFERENCES loans(id),
    amount  NUMERIC(14,2) NOT NULL
);

CREATE TABLE ledger_entries (
    id           BIGSERIAL   PRIMARY KEY,
    loan_id      INTEGER     NOT NULL REFERENCES loans(id),
    component    TEXT        NOT NULL CHECK (component IN ('principal','interest','fees')),
    amount       NUMERIC(14,2) NOT NULL CHECK (amount <> 0),
    entry_type   TEXT        NOT NULL CHECK (entry_type IN
                   ('opening_balance','disbursement','payment',
                    'fee_assessed','fee_waived','adjustment')),
    reason       TEXT,
    actor_id     INTEGER,
    actor_role   TEXT,
    payment_id   INTEGER     REFERENCES payments(id),
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE ledger_entries ADD CONSTRAINT ledger_actor_required CHECK (
    entry_type IN ('disbursement','fee_assessed','payment','opening_balance')
    OR (actor_id IS NOT NULL AND actor_role IS NOT NULL)
);

INSERT INTO loans (id, applicant_name, principal, apr, term_months)
VALUES (4471, 'Sam Okafor', 9000, 5.946, 24),
       (5582, 'Dana Whitfield', 12000, 6.240, 36);
"""

# The reported defect, restored verbatim in shape: validate the queued event's own
# NEW row instead of re-reading the proposal.
NEW_READING_TRIGGER = """
CREATE OR REPLACE FUNCTION pending_movement_resolution_is_complete() RETURNS trigger AS $$
BEGIN
    IF NEW.resolution = 'approved' AND NEW.ledger_entry_id IS NULL THEN
        RAISE EXCEPTION 'approved movement % has no ledger entry', NEW.id;
    END IF;
    IF NEW.resolution IS DISTINCT FROM 'approved' AND NEW.ledger_entry_id IS NOT NULL THEN
        RAISE EXCEPTION 'movement % is %, so it must have no ledger entry',
                        NEW.id, COALESCE(NEW.resolution, 'pending');
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;
"""


def _executable_blocks():
    """The `<!-- executable: name -->` fenced SQL blocks, in document order."""
    text = ADR.read_text(encoding="utf-8")
    found = re.findall(
        r"<!--\s*executable:\s*([\w-]+)\s*-->\s*\n\s*\n```sql\n(.*?)\n```",
        text,
        re.S,
    )
    return [(name, sql) for name, sql in found]


def _install(cur):
    cur.execute(f"SET search_path TO {SCHEMA}")
    cur.execute(FIXTURE)
    for _name, sql in _executable_blocks():
        cur.execute(sql)


@pytest.fixture
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = True
    with connection.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        _install(cur)
    connection.autocommit = False
    yield connection
    connection.rollback()
    connection.autocommit = True
    with connection.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.close()


def _propose(cur, *, component="fees", amount="-25.00", entry_type="fee_waived",
             reason="goodwill on a first late payment", requested_by=MAKER):
    cur.execute(f"SET search_path TO {SCHEMA}")
    cur.execute(
        "INSERT INTO pending_movements "
        "(loan_id, component, amount, entry_type, reason, requested_by, requested_role) "
        "VALUES (4471, %s, %s, %s, %s, %s, 'csr') RETURNING id",
        (component, amount, entry_type, reason, requested_by),
    )
    return cur.fetchone()[0]


def _approve(cur, movement_id, *, resolver=CHECKER, entry_overrides=None):
    """The ADR's documented approval sequence, step for step."""
    cur.execute(f"SET search_path TO {SCHEMA}")

    # 1. compare-and-swap: who approved it
    cur.execute(
        "UPDATE pending_movements SET resolution = 'approved', resolved_by = %s, "
        "resolved_role = 'underwriter', resolved_at = now() "
        "WHERE id = %s AND resolution IS NULL",
        (resolver, movement_id),
    )
    if cur.rowcount == 0:
        return None                       # somebody else resolved it first

    cur.execute(
        "SELECT loan_id, component, amount, entry_type, reason FROM pending_movements "
        "WHERE id = %s",
        (movement_id,),
    )
    loan_id, component, amount, entry_type, reason = cur.fetchone()
    values = {"loan_id": loan_id, "component": component, "amount": amount,
              "entry_type": entry_type, "reason": reason}
    values.update(entry_overrides or {})

    # 2. the entry, carrying the proposal it came from
    cur.execute(
        "INSERT INTO ledger_entries "
        "(loan_id, component, amount, entry_type, reason, actor_id, actor_role, "
        " pending_movement_id) "
        "VALUES (%(loan_id)s, %(component)s, %(amount)s, %(entry_type)s, %(reason)s, "
        "        NULL, NULL, %(movement_id)s) RETURNING id",
        {**values, "movement_id": movement_id},
    )
    entry_id = cur.fetchone()[0]

    # 3. the link back -- the one post-resolution write the transition trigger allows
    cur.execute(
        "UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
        (entry_id, movement_id),
    )
    return entry_id


# --- the finding: the approval transaction must COMMIT ------------------------

def test_the_documented_approval_transaction_commits(conn):
    """The whole point. Every earlier revision of this ADR described a sequence
    that could not reach COMMIT."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        entry_id = _approve(cur, movement_id)
        assert entry_id is not None
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "SELECT resolution, resolved_by, ledger_entry_id FROM pending_movements "
            "WHERE id = %s", (movement_id,)
        )
        assert cur.fetchone() == ("approved", CHECKER, entry_id)


def test_the_new_reading_trigger_would_have_failed(conn):
    """The regression, stated as the defect rather than as its absence.

    Restore the reported version -- validate the queued event's own NEW row --
    and the identical transaction cannot commit, because the event queued by step
    1 still says approved-without-entry when it fires.
    """
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(NEW_READING_TRIGGER)
    conn.autocommit = False

    with conn.cursor() as cur:
        movement_id = _propose(cur)
        _approve(cur, movement_id)
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            conn.commit()

    assert "has no ledger entry" in str(excinfo.value), (
        "the NEW-reading trigger did not raise the reported error, so this test "
        "is not exercising the defect it claims to"
    )


def test_a_rejection_commits_and_writes_no_entry(conn):
    """The other branch of the same commit-time rule."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        cur.execute(
            "UPDATE pending_movements SET resolution = 'rejected', resolved_by = %s, "
            "resolved_role = 'underwriter', resolved_at = now() WHERE id = %s",
            (CHECKER, movement_id),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) FROM ledger_entries")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM pending_movements WHERE resolution = 'rejected'")
        assert cur.fetchone()[0] == 1, (
            "the rejected proposal was not retained -- it is the evidence D8 says "
            "is missing"
        )


def test_an_approval_with_no_entry_still_fails_at_commit(conn):
    """Guard the guard. Re-reading the current state must not make the rule
    toothless: an approval that genuinely never inserted an entry has to fail."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        cur.execute(
            "UPDATE pending_movements SET resolution = 'approved', resolved_by = %s, "
            "resolved_role = 'underwriter', resolved_at = now() WHERE id = %s",
            (CHECKER, movement_id),
        )
        with pytest.raises(psycopg2.errors.RaiseException):
            conn.commit()


def test_a_rejection_cannot_carry_an_entry(conn):
    """The reverse direction of the same rule."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        _approve(cur, movement_id)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        # A second proposal, rejected, then illegally linked to the first entry.
        other = _propose(cur, reason="a second request")
        cur.execute(
            "UPDATE pending_movements SET resolution = 'rejected', resolved_by = %s, "
            "resolved_role = 'underwriter', resolved_at = now() WHERE id = %s",
            (CHECKER, other),
        )
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            cur.execute(
                "UPDATE pending_movements SET ledger_entry_id = "
                "(SELECT min(id) FROM ledger_entries) WHERE id = %s", (other,)
            )
        assert "produces no ledger entry" in str(excinfo.value)
    conn.rollback()


# --- the substance is frozen, reason and requested_at included ---------------

@pytest.mark.parametrize("column,value", [
    ("reason", "'a different reason entirely'"),
    ("requested_at", "now() - INTERVAL '3 days'"),
    ("requested_by", "9999"),
    ("requested_role", "'admin'"),
    ("amount", "-9999.00"),
    ("component", "'principal'"),
    ("loan_id", "5582"),
    ("entry_type", "'adjustment'"),
])
def test_the_substance_cannot_be_rewritten_before_resolution(conn, column, value):
    """A proposal in the queue is what the approver will be shown. If its reason
    can be edited between raising and approving, the approver signs one request
    and a different one is recorded."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        with pytest.raises(psycopg2.errors.Error):
            cur.execute(
                f"UPDATE pending_movements SET {column} = {value} WHERE id = %s",
                (movement_id,),
            )
    conn.rollback()


@pytest.mark.parametrize("column,value", [
    ("reason", "'rewritten after the fact'"),
    ("requested_at", "now() - INTERVAL '3 days'"),
])
def test_reason_and_requested_at_cannot_be_rewritten_after_resolution(conn, column, value):
    """The reported finding. `reason` and `requested_at` were not in the frozen
    set, so anything holding the application database role could rewrite why a
    staff money movement was requested after it had been approved -- gutting the
    audit trail this ADR exists to create.
    """
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        _approve(cur, movement_id)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            cur.execute(
                f"UPDATE pending_movements SET {column} = {value} WHERE id = %s",
                (movement_id,),
            )
        assert "immutable" in str(excinfo.value)
    conn.rollback()


def test_a_resolved_proposal_cannot_be_re_resolved(conn):
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        _approve(cur, movement_id)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute(
                "UPDATE pending_movements SET resolution = 'rejected' WHERE id = %s",
                (movement_id,),
            )
    conn.rollback()


def test_the_entry_link_cannot_be_swapped_once_set(conn):
    with conn.cursor() as cur:
        first = _propose(cur)
        _approve(cur, first)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute(
                "UPDATE pending_movements SET ledger_entry_id = ledger_entry_id + 1 "
                "WHERE id = %s", (first,)
            )
    conn.rollback()


# --- no self-approval, and the entry must match what was reviewed -------------

def test_the_requester_cannot_approve_their_own_request(conn):
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "UPDATE pending_movements SET resolution = 'approved', resolved_by = %s, "
                "resolved_role = 'admin', resolved_at = now() WHERE id = %s",
                (MAKER, movement_id),
            )
    conn.rollback()


@pytest.mark.parametrize("override", [
    {"amount": "-9999.00"},
    {"component": "principal"},
    {"entry_type": "adjustment"},
])
def test_an_entry_that_disagrees_with_its_proposal_is_refused(conn, override):
    """An approval that inserted different terms than the ones reviewed would be
    a bypass wearing the shape of an approval."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            _approve(cur, movement_id, entry_overrides=override)
        assert "does not match pending movement" in str(excinfo.value)
    conn.rollback()


def test_an_adjustment_entry_with_no_proposal_is_refused(conn):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        with pytest.raises(psycopg2.errors.Error):
            cur.execute(
                "INSERT INTO ledger_entries "
                "(loan_id, component, amount, entry_type, actor_id, actor_role) "
                "VALUES (4471, 'fees', -25.00, 'fee_waived', 1, 'admin')"
            )
    conn.rollback()


def test_an_entry_naming_an_unapproved_proposal_is_refused(conn):
    """The ordering constraint, from the other side: the entry cannot come first."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            cur.execute(
                "INSERT INTO ledger_entries "
                "(loan_id, component, amount, entry_type, pending_movement_id) "
                "VALUES (4471, 'fees', -25.00, 'fee_waived', %s)",
                (movement_id,),
            )
        assert "authorises no entry" in str(excinfo.value)
    conn.rollback()


def test_the_actor_is_overwritten_with_the_approver(conn):
    """Both fields. The insert above passes NULL for actor_id and actor_role, and
    `ledger_actor_required` would refuse the row -- so the trigger having filled
    them from the proposal is what lets it commit at all."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        entry_id = _approve(cur, movement_id)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT actor_id, actor_role FROM ledger_entries WHERE id = %s",
                    (entry_id,))
        assert cur.fetchone() == (CHECKER, "underwriter")


def test_a_caller_supplied_actor_is_ignored(conn):
    """A direct INSERT reproducing everything else correctly must not get to
    choose who is recorded as having authorised the movement."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        cur.execute(
            "UPDATE pending_movements SET resolution = 'approved', resolved_by = %s, "
            "resolved_role = 'underwriter', resolved_at = now() WHERE id = %s",
            (CHECKER, movement_id),
        )
        cur.execute(
            "INSERT INTO ledger_entries "
            "(loan_id, component, amount, entry_type, actor_id, actor_role, "
            " pending_movement_id) "
            "VALUES (4471, 'fees', -25.00, 'fee_waived', 424242, 'admin', %s) "
            "RETURNING actor_id, actor_role",
            (movement_id,),
        )
        assert cur.fetchone() == (CHECKER, "underwriter")
    conn.rollback()


def test_one_proposal_cannot_yield_two_entries(conn):
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        _approve(cur, movement_id)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO ledger_entries "
                "(loan_id, component, amount, entry_type, pending_movement_id) "
                "VALUES (4471, 'fees', -25.00, 'fee_waived', %s)",
                (movement_id,),
            )
    conn.rollback()


def test_a_machine_entry_may_not_carry_a_proposal(conn):
    """`approved_entries_have_a_proposal` runs both ways, so a payment cannot be
    dressed up as an approved adjustment or vice versa."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        _approve(cur, movement_id)
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO ledger_entries "
                "(loan_id, component, amount, entry_type, pending_movement_id) "
                "VALUES (4471, 'principal', -100.00, 'payment', %s)",
                (movement_id,),
            )
    conn.rollback()


# --- the proposal must describe a movement the ledger can represent ----------

def test_a_fee_waiver_targeting_another_component_is_refused_at_insert(conn):
    """Refused when raised, not when approved -- an approver should never be
    asked to sign off a request that cannot be executed."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            _propose(cur, component="principal", entry_type="fee_waived")
    conn.rollback()


def test_a_proposal_needs_a_reason(conn):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        with pytest.raises(psycopg2.errors.NotNullViolation):
            cur.execute(
                "INSERT INTO pending_movements "
                "(loan_id, component, amount, entry_type, requested_by, requested_role) "
                "VALUES (4471, 'fees', -25.00, 'fee_waived', %s, 'csr')",
                (MAKER,),
            )
    conn.rollback()


# --- this file must not be able to go vacuously green ------------------------

def test_every_executable_block_was_run():
    """A block renamed or dropped from the ADR must break this file loudly rather
    than reduce what it proves."""
    names = [name for name, _sql in _executable_blocks()]
    assert names == EXPECTED_BLOCKS, (
        f"the ADR's executable blocks are {names}; this file installs and tests "
        f"{EXPECTED_BLOCKS}"
    )


def test_the_resolve_function_is_still_only_a_signature():
    """Its body belongs with the migration. If a body appears here, these tests
    stop covering the ADR's actual content and start covering an implementation
    the ADR does not claim to specify."""
    text = ADR.read_text(encoding="utf-8")
    assert "CREATE FUNCTION resolve_pending_movement(" in text
    assert "RETURNS BIGINT;" in text, (
        "resolve_pending_movement() is no longer a signature-only declaration"
    )


# --- retention: the invariant that was a sentence -----------------------------

def test_a_rejected_proposal_cannot_be_deleted(conn):
    """Invariant 7 promises rejected proposals are retained as D8 evidence.

    Nothing enforced it: the transition trigger is BEFORE UPDATE, so it governs
    what may change and says nothing about what may be removed, and a rejected
    proposal has no ledger entry -- so the foreign key does not hold it down
    either. Anything with the application database role could erase the record of
    a refused money movement, which is the one record a control exists to leave.
    """
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        cur.execute(
            "UPDATE pending_movements SET resolution = 'rejected', resolved_by = %s, "
            "resolved_role = 'underwriter', resolved_at = now() WHERE id = %s",
            (CHECKER, movement_id),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            cur.execute("DELETE FROM pending_movements WHERE resolution = 'rejected'")
        assert "may not be deleted" in str(excinfo.value)
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) FROM pending_movements WHERE resolution = 'rejected'")
        assert cur.fetchone()[0] == 1, "the rejected proposal did not survive"
    conn.rollback()


def test_an_approved_proposal_cannot_be_deleted(conn):
    """The other resolution. An approved proposal is the authorisation behind a
    ledger entry that cannot be updated or deleted; removing it would leave an
    immutable money movement pointing at nothing."""
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        _approve(cur, movement_id)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("DELETE FROM pending_movements WHERE id = %s", (movement_id,))
    conn.rollback()


def test_an_unresolved_proposal_cannot_be_deleted_either(conn):
    """A request raised and then removed before anyone answered it is the same
    evidence gap as one rejected and then removed -- and it is the record most
    worth removing, because nobody has looked at it yet.

    Withdrawal, if it is ever wanted, is a third resolution recorded in the
    table. It is not a DELETE.
    """
    with conn.cursor() as cur:
        movement_id = _propose(cur)
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            cur.execute("DELETE FROM pending_movements WHERE id = %s", (movement_id,))
        assert "pending" in str(excinfo.value)
    conn.rollback()


def test_a_bulk_delete_cannot_slip_past_the_row_trigger(conn):
    """FOR EACH ROW, so a statement touching many rows raises on the first one
    and the whole statement rolls back. A STATEMENT-level trigger reading OLD
    would not have fired at all."""
    with conn.cursor() as cur:
        first = _propose(cur)
        second = _propose(cur, reason="a second request")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("DELETE FROM pending_movements")
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) FROM pending_movements")
        assert cur.fetchone()[0] == 2
        assert first != second
    conn.rollback()
