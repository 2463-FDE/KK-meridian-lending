# ADR 0011: Maker-checker for servicing balance adjustments

- **Status:** Accepted and **fully implemented**. All three steps have landed:
  the schema in `db/migrations/0036_pending_movements.sql` (step 1), the approval
  function in `db/migrations/0037_resolve_pending_movement.sql` (step 2), and the
  API cutover in `services/servicing-service/app/maker_checker.py` (step 3), which
  turned `adjust-balance` and `waive-fee` into proposals that move no money.
  **D8 is closed** — no one person can move a balance alone through the
  application. See `docs/DEBT.md` D8 for what that does and does not cover, and
  *Limitations* below for the direct-`INSERT` boundary, which the cutover does
  not change.
- **Date:** 2026-08-12 (steps 1-3 all landed 2026-08-16, PRs #34 and #35)
- **Depends on:** ADR 0010, and on the signed human principal that
  `services/servicing-service/app/principal.py` verifies — a proposal's
  `requested_by` and an approval's `resolved_by` are meaningless without an
  identity the caller cannot forge
- **Bears on:** `docs/DEBT.md` D8 (any authenticated user can adjust a balance or
  waive a fee, with no second approver and no record of who asked)

## Configured limits, approved for this cohort/demo environment

The project owner approved four values on 2026-08-16. They are **configuration**,
not architecture, and this ADR records them so a reader can tell what the control
was built and tested against — **they are not Lending Operations policy, and
production adoption requires Lending Operations to set or approve each one.**

| Decision | Approved value | Enforced where |
|---|---|---|
| `MAKER_CHECKER_ADMIN_THRESHOLD` | 500.00 — at or below, underwriter or admin may approve; above, admin only; csr never | API, from configuration |
| `MAKER_CHECKER_MAX_DELTA` | 5000.00 — above it, refused at creation for every role | API, from configuration |
| Permitted loan statuses | exactly `{"current"}`; anything else, including unrecognised, fails closed and is re-checked at approval | API, from configuration |
| Maker authorization scope | spec 0002 REQ-VAL-14 **option 2**: any staff principal may propose against any serviced, `current` loan with a `balances` row. A reviewed limitation, not an assignment model | API |

**Neither figure appears in the schema, and that is deliberate.** A CHECK
carrying 500.00 or 5000.00 would make a policy change a migration, and would
freeze a cohort/demo number into the shape of the data. `pending_movements`
records `resolved_threshold` — the limit a resolution was actually judged
against — so a later reader can tell which bar applied without the schema
asserting what the bar should be.

## Required invariants

What must hold for an implementation to be this decision.

1. **A proposal is not a movement.** Writing a `pending_movements` row moves no
   money; only the approval writes a ledger entry.
2. **Exactly one terminal transition per proposal**, ever. A resolved proposal
   cannot be re-resolved, and its substance cannot change after it is raised.
   *Substance* is enumerated rather than left to judgement: `loan_id`,
   `component`, `amount`, `entry_type`, `reason`, `requested_by` and
   `requested_at`. `reason` and `requested_at` are in that list because the
   reason is the evidence D8 says is missing, and a reason that can be rewritten
   after approval is a note rather than evidence.
3. **No self-approval.** The requester may not be the approver, enforced at the
   database and inside the resolving function.
4. **An approval produces exactly one ledger entry; a rejection produces none.**
   Checked at COMMIT, because the resolving transaction is legitimately mid-state.
5. **The entry matches the proposal field-for-field** — loan, component, amount,
   type — so an approval cannot authorise different terms than the ones reviewed.
6. **The ledger actor is the approver** — both `actor_id` and `actor_role`,
   overwritten by trigger rather than trusted from the caller. Overwriting only
   the id would leave half the attribution caller-supplied on the entry whose
   whole purpose is to record who authorised the movement. The requester and
   their role survive on the proposal.
7. **A rejected proposal is retained**, and so is an unresolved one. It is the
   evidence D8 says is missing. Enforced by a `BEFORE DELETE` trigger, not by
   convention -- a `REVOKE DELETE` does not stick when every service connects as
   the schema owner, and until that trigger existed this invariant was a
   sentence.
8. **A proposal must describe a movement the ledger can represent.** A
   `fee_waived` proposal targets the `fees` component and nothing else, refused
   at insert rather than at approval — an approver should never be asked to sign
   off a request that cannot be executed.
9. **Approval state is single-sourced, with no second copy.**
   `pending_movements.resolution` is the fact and the only place it is stored. The
   ledger entry carries `pending_movement_id` and nothing else about approval, so
   there is no denormalised copy that can drift — `approved_at` is the proposal's
   `resolved_at`, one join away, and a join is cheaper than two answers.

## Why this is a separate ADR

ADR 0010 makes servicing balances a projection of an append-only ledger. That is
what makes this possible — before it there was nowhere to put a request that is
not yet a fact, because the only place to write was `balances` itself and writing
there IS the movement.

It was drafted as part of 0010 and split out for a plain reason: a reviewer
could not approve the ledger without also approving this. They are different
decisions with different risks. The ledger changes how a number is stored; this
changes who is allowed to move money and what evidence that leaves. A reviewer
who accepts the first and wants to argue about the second should be able to.

**Where this lands in 0010's sequence, stated in both documents so they cannot
drift apart.** This ADR is **PR-4**, and it comes *before* the staff paths move
to the ledger:

| Step | What happens |
|---|---|
| PR-3 | The three machine writers convert. `adjust_balance` and `waive_fee` still write `balances` directly. |
| **PR-4** | **This ADR.** `pending_movements`, `resolve_pending_movement()`, maker-checker on adjust and waive. |
| PR-5 | `adjust_balance` and `waive_fee` convert to ledger entries, then the direct-write guard is attached. |

The order is not interchangeable. Converting the staff paths first would write
unapproved staff money movements into an append-only table that cannot be
corrected -- a worse permanent record than the mutable column they write today.
And 0010's "the projection is the only writer of `balances`" is not true until
PR-5 completes, which is why 0010 does not claim it before then.

**What ADR 0010 already settles, and this ADR inherits rather than reopens:**

- proposals live in their own table, not as `approved_by`/`approved_at` columns
  on `ledger_entries` — an approver updating a ledger row is what the
  append-only trigger forbids, and a rejected proposal has to survive as
  evidence, which a column on an entry that was never written cannot carry;
- `entry_type` separates human-authorised movements (`adjustment`,
  `fee_waived`) from machine-originated ones (`payment`, `fee_assessed`,
  `disbursement`, `opening_balance`), because only the first kind needs an
  approver and the constraint saying so has to tell them apart.

Both had to be decided in 0010 because retrofitting either means migrating the
money table a second time. Everything below is this ADR's to decide.

## Decision

Money-moving staff actions — `adjust-balance` and `waive-fee` — become proposals
that a **different** staff account resolves. A proposal moves no money; the
approval is what writes the ledger entry, and the projection trigger does the
rest.

## Why the ledger is shaped for this

> Approval columns on `ledger_entries` would require the approver to `UPDATE`
> that row, which the append-only trigger forbids. Immutability and approval have
> to be checked against each other, not each alone.

The answer is not to weaken the trigger. It is to stop putting proposals in the
ledger at all.

**A row in `ledger_entries` means money moved.** A proposed adjustment is not a
movement — it is a request that may never become one. Those are different facts
and they get different tables:

**This SQL runs.** Unlike the illustrative blocks in ADR 0010, every block below
marked `<!-- executable -->` is extracted verbatim and executed against real
PostgreSQL by `db/tests/test_adr_0011_enforcement_runs_on_postgres.py`, which
then performs the approval transaction exactly as documented and asserts that it
**commits**. That is deliberate and it is a departure from 0010's rule that
function bodies are illustration, for one reason: this ADR's whole content is a
set of constraints that have to hold *against each other*, and three successive
review rounds each added a correct-looking rule that made the approval path
unsatisfiable. A constraint design that cannot commit is not a design, and prose
cannot tell you which of these it is.

`resolve_pending_movement()` stays a signature. It is application orchestration,
its body belongs with the migration that creates it, and what this ADR fixes
about it is that approval is one function rather than a sequence an application
is trusted to perform in order.

<!-- executable: 1-pending-movements -->

```sql
CREATE TABLE pending_movements (
    id            BIGSERIAL   PRIMARY KEY,
    loan_id       INTEGER     NOT NULL REFERENCES loans(id),
    component     TEXT        NOT NULL,
    amount        NUMERIC(14,2) NOT NULL,
    entry_type    TEXT        NOT NULL CHECK (entry_type IN ('adjustment','fee_waived')),
    reason        TEXT        NOT NULL,      -- required: a proposal without one is unreviewable
    requested_by  INTEGER     NOT NULL,
    -- The role is stored alongside the id on both sides because the ledger's
    -- own actor constraint requires both, and the entry's actor is written FROM
    -- this row rather than from the caller (invariant 6). A proposal that
    -- recorded only ids would leave the role to be supplied by whoever inserts
    -- the entry.
    requested_role TEXT       NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Terminal state, written once. This table is NOT append-only: resolving a
    -- proposal is the one legitimate mutation in the design, and it is confined
    -- here precisely so the ledger's guarantee stays absolute.
    resolution    TEXT        CHECK (resolution IN ('approved','rejected')),
    resolved_by   INTEGER,
    resolved_role TEXT,
    resolved_at   TIMESTAMPTZ,
    ledger_entry_id BIGINT    REFERENCES ledger_entries(id),

    CONSTRAINT no_self_approval CHECK (resolved_by IS NULL OR resolved_by <> requested_by),
    CONSTRAINT resolution_complete CHECK (
        (resolution IS NULL
            AND resolved_by IS NULL AND resolved_role IS NULL AND resolved_at IS NULL)
     OR (resolution IS NOT NULL
            AND resolved_by IS NOT NULL AND resolved_role IS NOT NULL
            AND resolved_at IS NOT NULL)
    ),
    -- "an approval produces exactly one ledger entry, a rejection produces none"
    -- is NOT a CHECK. It cannot be: the entry is inserted after the row is marked
    -- approved, so an immediate CHECK fails mid-transaction on a state that is
    -- legitimately transient. It is enforced at COMMIT instead -- see the
    -- deferred constraint trigger below.
    --
    -- PostgreSQL CHECK constraints cannot be DEFERRABLE, which is exactly why
    -- this is a constraint trigger rather than a deferred CHECK.

    -- Same component vocabulary as the ledger. Without this a proposal could
    -- name a component the ledger cannot hold, and the mismatch would surface
    -- only at approval time -- after a human had already reviewed and accepted it.
    CONSTRAINT pending_component CHECK (component IN ('principal','interest','fees')),

    -- A fee waiver moves fees. ADR 0010 fixes `fee_waived` to the `fees`
    -- component, so a proposal naming any other one describes a movement the
    -- ledger cannot represent -- and it would fail at the ledger insert, AFTER a
    -- second person had approved it. Refusing it here means the approver never
    -- sees an incoherent request in their queue.
    --
    -- `adjustment` is deliberately left open to all three: adjusting principal,
    -- accrued interest or fees are all real corrections, and which one is being
    -- adjusted is the substance of the request.
    CONSTRAINT pending_fee_waiver_is_fees CHECK (
        entry_type <> 'fee_waived' OR component = 'fees'
    )
);
```

**One column is added to `ledger_entries` here, and none in ADR 0010's
migration** — `pending_movement_id`. A column belongs in the migration that can
enforce its invariant, and this one cannot be enforced without `pending_movements`
to point at: the foreign key, the `UNIQUE` constraint and the validation trigger
all need the table to exist.

`approved_required` and `approved_at` exist in **neither** ADR. They would have
been a second, writable answer to "was this approved" sitting beside the
proposal's own resolution, and the only thing they buy is an avoided join.
`approved_required` is derivable from `entry_type` and `approved_at` is
`pending_movements.resolved_at`.

Adding the column makes the two tables reference each other, which is a genuine
cycle: an entry points at the proposal that authorised it, and the proposal
points back at the entry it produced. Split the ordinary way — create the table
first (its foreign key to `ledger_entries` resolves immediately), then add the
column and the foreign key back.

**The `approved_entries_have_a_proposal` CHECK belongs in this block and not the
one above**, because it names `pending_movement_id` — a column that does not
exist until the `ALTER` two lines earlier. An earlier revision of this ADR put it
in the table block, which made the document's own SQL unrunnable in the order it
was written. That is the third time this document has had an ordering
contradiction, and it is why the blocks are executed by a test now rather than
read for plausibility.

<!-- executable: 2-ledger-entries-link -->

```sql
-- ONE column, not three. An earlier version of this ADR added
-- `approved_required` and `approved_at` alongside it, and then claimed
-- `pending_movements.resolution` was the single source of truth for approval --
-- which cannot both be true. They are gone: the proposal is the record, the entry
-- points at it, and any reader wanting approval metadata joins one row.
--
-- Nothing was lost with them. `approved_required` is derivable from entry_type
-- (an 'adjustment' or 'fee_waived' requires a proposal; nothing else does) and
-- `approved_at` is `pending_movements.resolved_at`. Keeping denormalised copies
-- would have bought one avoided join and cost the thing this ADR is for: a
-- second, writable answer to "was this approved".
ALTER TABLE ledger_entries
    ADD COLUMN pending_movement_id BIGINT UNIQUE;

ALTER TABLE ledger_entries
    ADD CONSTRAINT ledger_entries_pending_movement_fk
    FOREIGN KEY (pending_movement_id) REFERENCES pending_movements(id);

-- The approved entry must BE the movement that was approved. The UNIQUE above
-- means one approval can never yield two entries; this says which entry types
-- must come from an approval at all. adjustment and fee_waived are the
-- maker-checker subjects, so they may only enter the ledger through a proposal;
-- the machine-originated types must carry none, so a payment cannot be dressed
-- up as an approved adjustment.
ALTER TABLE ledger_entries ADD CONSTRAINT approved_entries_have_a_proposal CHECK (
    (entry_type IN ('adjustment','fee_waived') AND pending_movement_id IS NOT NULL)
 OR (entry_type NOT IN ('adjustment','fee_waived') AND pending_movement_id IS NULL)
);
```

### Exactly one terminal transition, and nothing else ever changes

`pending_movements` is the one mutable table in this design, so what may change
about a row is enumerated rather than left to whoever writes the next UPDATE.

**`reason` and `requested_at` are frozen with the rest of the substance.** An
earlier revision froze only loan, component, amount, type and requester, which
left *why* a money movement was requested rewritable after it had been approved
— by any code path holding the application database role, and this system
deliberately does not rely on privileges for enforcement. The reason is the
evidence D8 says is missing; a reason that can be rewritten after the fact is not
evidence, it is a note. `requested_at` goes with it for the same reason: an
approval reviewed at one time and re-dated to another is a different record of
the same decision.

<!-- executable: 3-single-transition -->

```sql
CREATE FUNCTION pending_movements_single_transition() RETURNS trigger AS $$
BEGIN
    -- The substance never changes, resolved or not. Checked first so it applies
    -- to both branches below.
    --
    -- `reason` and `requested_at` are in this list, not merely the fields that
    -- describe the money. Anything with the application role could otherwise
    -- rewrite why a staff money movement was requested, after a second person
    -- had approved the reason they were shown.
    IF NEW.loan_id      IS DISTINCT FROM OLD.loan_id
    OR NEW.component    IS DISTINCT FROM OLD.component
    OR NEW.amount       IS DISTINCT FROM OLD.amount
    OR NEW.entry_type   IS DISTINCT FROM OLD.entry_type
    OR NEW.reason        IS DISTINCT FROM OLD.reason
    OR NEW.requested_by   IS DISTINCT FROM OLD.requested_by
    OR NEW.requested_role IS DISTINCT FROM OLD.requested_role
    OR NEW.requested_at   IS DISTINCT FROM OLD.requested_at THEN
        RAISE EXCEPTION 'the substance of a pending movement is immutable';
    END IF;

    IF OLD.resolution IS NOT NULL THEN
        -- Already resolved. Exactly ONE further write is legal: attaching the
        -- ledger entry this approval produced, once, from NULL.
        --
        -- An earlier revision of this ADR refused every post-resolution UPDATE
        -- outright, which made its own approval order impossible: mark
        -- approved, insert the entry, then write ledger_entry_id back. That
        -- third step hit this trigger and raised, so no staff adjustment or fee
        -- waiver could ever complete. The rule was right and the exception was
        -- missing.
        --
        -- Narrow on purpose. The resolution, the resolver and the substance are
        -- all still frozen; only a NULL link may be filled, and only once, so
        -- an entry cannot be swapped for a different one afterwards.
        IF OLD.ledger_entry_id IS NOT NULL THEN
            RAISE EXCEPTION 'pending movement % is already linked to entry %',
                OLD.id, OLD.ledger_entry_id;
        END IF;
        IF NEW.ledger_entry_id IS NULL THEN
            RAISE EXCEPTION 'pending movement % is already %', OLD.id, OLD.resolution;
        END IF;
        -- Only an APPROVAL produces an entry. The deferred trigger catches a
        -- rejection carrying one at COMMIT anyway, but catching it at the
        -- statement names the offending write instead of failing the whole
        -- transaction later with no indication of which statement did it.
        IF OLD.resolution <> 'approved' THEN
            RAISE EXCEPTION 'pending movement % was %, so it produces no ledger entry',
                OLD.id, OLD.resolution;
        END IF;
        IF NEW.resolution    IS DISTINCT FROM OLD.resolution
        OR NEW.resolved_by   IS DISTINCT FROM OLD.resolved_by
        OR NEW.resolved_role IS DISTINCT FROM OLD.resolved_role
        OR NEW.resolved_at   IS DISTINCT FROM OLD.resolved_at THEN
            RAISE EXCEPTION 'a resolved movement may only gain its ledger entry link';
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER pending_movements_one_way
    BEFORE UPDATE ON pending_movements
    FOR EACH ROW EXECUTE FUNCTION pending_movements_single_transition();
```

### Retention is a delete guard, not a promise

Invariant 7 says a rejected proposal is retained -- it is the evidence D8 says is
missing, and the whole reason proposals live in their own table rather than as
columns on a ledger entry that was never written.

**Nothing enforced it.** The transition trigger is `BEFORE UPDATE`, so it says
what may CHANGE and nothing about what may be REMOVED. A rejected proposal has no
ledger entry by design, so `ledger_entries.pending_movement_id` does not hold it
down either, and anything with the application database role could delete the
row. The invariant was a sentence.

That is the same shape as the finding above and the two before it: a rule stated
in prose, enforced nowhere, and invisible because the surrounding rules are
enforced. It is also why the ADR's claim about privileges matters here --
`REVOKE DELETE` does not stick when every service connects as the schema owner
(ADR 0002, ADR 0006), so this has to be a trigger for the same reason
append-only does.

**Every delete is refused, not only resolved ones.** A proposal that was raised
and then vanished is the same evidence gap as one that was rejected and then
vanished: the question D8 asks is *what did staff ask for*, and a request removed
before anyone answered it is exactly the record that would be worth removing.
Withdrawal, if it is ever wanted, is a third `resolution` -- a decision recorded
in the table -- and not a `DELETE`.

<!-- executable: 4-retention -->

```sql
CREATE FUNCTION pending_movements_are_retained() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'pending movement % may not be deleted: proposals are retained as the '
        'evidence of what staff asked for (%)',
        OLD.id, COALESCE(OLD.resolution, 'pending');
END $$ LANGUAGE plpgsql;

CREATE TRIGGER pending_movements_no_delete
    BEFORE DELETE ON pending_movements
    FOR EACH ROW EXECUTE FUNCTION pending_movements_are_retained();
```

This makes `pending_movements` append-and-resolve-only: rows are inserted, one
terminal transition is permitted, the ledger link may be filled once, and nothing
is ever removed. The ledger's own guarantee is stricter still -- no UPDATE at all
-- and the difference is deliberate: resolving a proposal is the one legitimate
mutation in this design, confined to this table precisely so the ledger's
guarantee stays absolute.

### The commit-time rule, and why it must re-read the row

Approval implies exactly one entry; rejection implies none. This cannot be an
immediate check, because the resolving transaction is legitimately mid-state
between marking the movement approved and linking the entry it just inserted.

**It also cannot read `NEW`.** PostgreSQL queues one deferred trigger event per
UPDATE, each carrying the row version *that event* produced, and fires every one
of them at COMMIT. The approval sequence updates the row twice — once to resolve
it, once to link the entry — so the first event still arrives at COMMIT holding
`resolution = 'approved'` and `ledger_entry_id IS NULL`. A trigger reading `NEW`
therefore raises on **every** approval, blocking all staff money movements, while
looking correct on the page.

An earlier revision of this ADR read `NEW`. It was reported by review, and it is
the exact failure this document has now produced three times: a rule that is
right in isolation and unsatisfiable next to the ones already there. So the
trigger re-selects the proposal's **current** state at firing time, which is the
only state the transaction is actually committing:

<!-- executable: 5-resolution-complete -->

```sql
CREATE FUNCTION pending_movement_resolution_is_complete() RETURNS trigger AS $$
DECLARE
    final_resolution TEXT;
    final_entry      BIGINT;
BEGIN
    -- The row AS IT STANDS NOW, not as it stood when this event was queued.
    -- Running at COMMIT inside the same transaction, this sees every earlier
    -- statement's effect -- so the intermediate approved-without-entry state
    -- that queued one of these events is no longer what is being validated.
    --
    -- Both queued events re-read the same final row and agree, which makes the
    -- check idempotent rather than order-dependent. That is the property worth
    -- having: an implementation that adds a third UPDATE to this sequence must
    -- not be able to break the constraint by doing so.
    SELECT resolution, ledger_entry_id
      INTO final_resolution, final_entry
      FROM pending_movements
     WHERE id = NEW.id;

    IF NOT FOUND THEN
        -- Inserted and deleted in the same transaction. Nothing is being
        -- committed about this proposal, so there is nothing to validate.
        RETURN NULL;
    END IF;

    IF final_resolution = 'approved' AND final_entry IS NULL THEN
        RAISE EXCEPTION 'approved movement % has no ledger entry', NEW.id;
    END IF;
    IF final_resolution IS DISTINCT FROM 'approved' AND final_entry IS NOT NULL THEN
        RAISE EXCEPTION 'movement % is %, so it must have no ledger entry',
                        NEW.id, COALESCE(final_resolution, 'pending');
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER pending_movements_resolution_complete
    AFTER INSERT OR UPDATE ON pending_movements
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION pending_movement_resolution_is_complete();
```

### The approval sequence this permits

Stated explicitly, because the triggers above are what enforce it and getting the
order wrong is what deadlocked two earlier revisions:

1. `UPDATE pending_movements SET resolution='approved', resolved_by=…,
   resolved_role=…, resolved_at=now() WHERE id=… AND resolution IS NULL` — the
   compare-and-swap
   that decides who approved it. Zero rows means somebody else already resolved
   it, and the caller stops.
2. `INSERT INTO ledger_entries (…, pending_movement_id) VALUES (…)` — the entry,
   carrying the proposal it came from. The validation trigger requires the
   proposal to be approved already, which is why this cannot come first.
3. `UPDATE pending_movements SET ledger_entry_id=… WHERE id=…` — the link back.
   This is the one post-resolution write the transition trigger allows, and only
   from NULL.
4. `COMMIT` — the deferred trigger re-reads the row and checks
   approval-implies-entry against its final state.

All four run in one transaction, so a crash between them leaves no approved
proposal without its entry. Step 3 is separate only because the entry's id does
not exist until step 2 has run; a single statement would need the id before it
was generated.

`db/tests/test_adr_0011_enforcement_runs_on_postgres.py` executes exactly this
sequence against real PostgreSQL and asserts the COMMIT succeeds — and asserts,
by reverting the trigger to the `NEW`-reading version inside the test, that it
would not have.

Field-for-field agreement between the proposal and the entry it produces is
enforced twice — inside `resolve_pending_movement()`, which builds the entry from
the locked proposal row, and by `ledger_entry_matches_its_proposal()` further
down, which rejects any entry that disagrees with the proposal it names — and
asserted by a migration test: an approval that inserted a different loan,
component, amount or type than the one reviewed would be a maker-checker bypass
wearing the shape of an approval.

> *Illustrative and non-runnable.* Shape of the decision, not the migration —
> the executable version lands in the migration PR where it can be tested.

```sql
-- Signature only. The body belongs in the migration that creates it (step 4),
-- where it can be executed and tested; what this ADR fixes is that approval is
-- ONE function rather than a sequence of statements an application is trusted
-- to perform in the right order.
CREATE FUNCTION resolve_pending_movement(
    p_movement_id BIGINT,
    p_resolver    INTEGER,
    p_resolution  TEXT          -- 'approved' | 'rejected'
) RETURNS BIGINT;                -- the new ledger entry, or NULL for a rejection
```

**What step 4 has to prove about it.** These are the requirements, not
suggestions — each one is a test in that PR, and the function is not done until
every one of them fails when removed:

1. **It locks the proposal** (`SELECT ... FOR UPDATE`) before reading its state.
   Two approvers clicking at once would otherwise both read `resolution IS NULL`
   and both insert.
2. **Exactly one transition, ever.** A proposal that already has a resolution is
   refused — not approved-then-rejected, not a second approver overwriting the
   first. Once resolved, the resolution cannot change.
3. **The requester may not approve their own request.** Enforced here as well as
   by the table constraint, because this is the path that writes the money.
4. **An approval writes exactly one ledger entry; a rejection writes none.**
5. **The entry is built FROM the locked proposal row, never from caller input**,
   so `loan_id`, `component`, `amount` and `entry_type` cannot differ from what
   was reviewed. An approval that inserted different terms than the ones reviewed
   would be a bypass wearing the shape of an approval.
6. **The ledger actor is the approver** — see the actor rule above.

**One ordering constraint the implementer cannot discover from the requirements,
because getting it wrong deadlocks:** the entry must be inserted *after* the
movement is marked approved, since the validation trigger below requires an
approved proposal — while "approved implies an entry" cannot then be an immediate
CHECK, because that state is legitimately transient inside the transaction. So:

    1. mark approved (`ledger_entry_id` still NULL)
    2. insert the entry (the trigger now sees 'approved')
    3. write `ledger_entry_id` back
    4. COMMIT → the deferred trigger checks approval-implies-entry

Written the other way round these block each other and every approval fails.
PostgreSQL CHECK constraints cannot be `DEFERRABLE`, which is why step 4's rule
is a constraint trigger rather than a deferred CHECK.

## What makes the function the only path

The prose claimed `adjustment` and `fee_waived` entries were "unreachable except
through" `resolve_pending_movement()`. The constraints shown do not achieve that:
they require a `pending_movement_id`, and nothing stopped a direct `INSERT` that
supplied a valid one. Naming the mechanism rather than asserting the outcome:

**Privileges are not the mechanism here.** The obvious answer -- `REVOKE INSERT ON
ledger_entries` from the application role and `GRANT EXECUTE` on the function --
does not hold in this system, for the same reason `decision_events` is protected
by a trigger rather than a `GRANT` (ADR 0002, ADR 0006): every service connects as
the schema-owning role, so a revoke from the owner does not stick.

**A validation trigger is.** It compares the entry being inserted against the
proposal it names, and rejects any mismatch, so a direct `INSERT` can only succeed
by reproducing exactly what an approver already authorised:

This one is executable too, and for the same reason as the others: it is half of
the ordering constraint that deadlocked earlier revisions. It requires the
proposal to be approved *already*, which is what forces the entry insert to come
second, and a version of this document that only described that requirement could
not show whether the sequence it prescribes actually satisfies it.

**What it rejects**, for `adjustment` and `fee_waived` entries only —
machine-originated types (`payment`, `fee_assessed`, `disbursement`,
`opening_balance`) have no proposal and pass straight through:

- a NULL `pending_movement_id`, or one naming no proposal;
- a proposal that is not `approved`;
- a proposal whose approver is missing or equal to its requester;
- any disagreement on `loan_id`, `component`, `amount` or `entry_type` between
  the entry and the proposal it names.

And one thing it **overwrites** rather than validates: `actor_id` is set to the
proposal's approver, so a direct INSERT cannot misattribute who authorised the
movement even while reproducing everything else correctly.

<!-- executable: 6-entry-matches-proposal -->

```sql
CREATE FUNCTION ledger_entry_matches_its_proposal() RETURNS trigger AS $$
DECLARE
    proposal pending_movements;
BEGIN
    -- Machine-originated entries have no proposal and are not this trigger's
    -- business. `approved_entries_have_a_proposal` already refuses them a
    -- pending_movement_id, so there is nothing here to check.
    IF NEW.entry_type NOT IN ('adjustment','fee_waived') THEN
        RETURN NEW;
    END IF;

    IF NEW.pending_movement_id IS NULL THEN
        RAISE EXCEPTION 'a % entry must name the proposal that authorised it',
                        NEW.entry_type;
    END IF;

    -- FOR SHARE, not a plain read: the proposal must not be resolved differently
    -- by another transaction between this check and the commit that depends on it.
    SELECT * INTO proposal FROM pending_movements
     WHERE id = NEW.pending_movement_id FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'pending movement % does not exist', NEW.pending_movement_id;
    END IF;

    IF proposal.resolution IS DISTINCT FROM 'approved' THEN
        RAISE EXCEPTION 'pending movement % is %, so it authorises no entry',
                        proposal.id, COALESCE(proposal.resolution, 'pending');
    END IF;

    -- Belt and braces with `no_self_approval` on the table. This is the path
    -- that writes the money, so it re-checks rather than assuming the constraint
    -- that guards the other path was never dropped.
    IF proposal.resolved_by IS NULL OR proposal.resolved_by = proposal.requested_by THEN
        RAISE EXCEPTION 'pending movement % has no distinct approver', proposal.id;
    END IF;

    IF NEW.loan_id    IS DISTINCT FROM proposal.loan_id
    OR NEW.component  IS DISTINCT FROM proposal.component
    OR NEW.amount     IS DISTINCT FROM proposal.amount
    OR NEW.entry_type IS DISTINCT FROM proposal.entry_type THEN
        RAISE EXCEPTION 'entry does not match pending movement % -- an approval '
                        'may not authorise different terms than the ones reviewed',
                        proposal.id;
    END IF;

    -- Overwritten, not validated: the actor on a human-authorised entry is the
    -- APPROVER, and a caller reproducing everything else correctly must not get
    -- to choose who gets the credit for authorising it. Both fields, because
    -- ledger_actor_required needs both and overwriting only the id would leave
    -- the role caller-supplied on exactly the row that exists to record it.
    NEW.actor_id   := proposal.resolved_by;
    NEW.actor_role := proposal.resolved_role;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_entries_match_proposal
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_matches_its_proposal();
```

So the honest claim is narrower than the original one and it is enforced: a direct
`INSERT` is *possible*, and it cannot produce anything an approver did not already
authorise -- including the actor, which the trigger overwrites with the approver
rather than trusting the caller.

A CSR raising an adjustment writes a `pending_movements` row. The balance does
not move, because nothing was written to the ledger. A **different** staff
account approves it, and that approval inserts the `ledger_entries` row and
records its id here, in one transaction. A rejection resolves the proposal and
writes no entry — and the rejected request survives as evidence, which the
column-on-the-ledger design would have lost entirely.

Self-approval is refused by a database constraint rather than by a service
remembering to check, the same choice as the append-only trigger and for the same
reason.

**What this buys beyond correctness:** the projection stays a plain
`SUM(amount)` with no approval filter. Under the original design every reader
had to remember `WHERE approved_at IS NOT NULL`, and one that forgot would
silently count unapproved money. Here there is nothing to forget — if a row is in
the ledger it counts, with no exceptions, which is the same reasoning that makes
the append-only trigger worth having.

This is why maker-checker was blocked before: there was nowhere to put a request
that is not yet a fact.

## Limitations

**A direct `INSERT` into `ledger_entries` is possible.** This is the boundary of
the guarantee and it belongs here rather than buried in the mechanism section,
because it changes what "maker-checker" means in this system.

The obvious enforcement — `REVOKE INSERT` from the application role and
`GRANT EXECUTE` on the approval function — does not hold here: every service
connects as the schema-owning role, so a revoke from the owner does not stick
(ADR 0002, ADR 0006). The same constraint that makes the append-only trigger
necessary makes privilege-based enforcement unavailable.

So the honest claim is narrower than "the function is the only path":

- a direct `INSERT` **cannot** produce an `adjustment` or `fee_waived` entry that
  no approver authorised, because the validation trigger rejects an entry whose
  proposal is missing, unapproved, self-approved, or disagrees on loan,
  component, amount or type;
- it **cannot** misattribute the actor, because the trigger overwrites
  `actor_id` with the proposal's approver rather than trusting the caller;
- it **can** be issued by anything holding a database connection, reproducing
  exactly what an approver already authorised — which is a replay of an
  authorised movement, not a forgery of an unauthorised one, and the `UNIQUE`
  constraint on `pending_movement_id` stops the same proposal being replayed
  twice.

**What that leaves open:** anyone with direct database access can move money
within the shape of something already approved. Maker-checker is a control on the
application's staff paths, not a defence against a compromised database
credential. Treating it as the latter would be the same overclaim this ADR
corrects elsewhere.

## Consequences

- **Two people are needed for an adjustment.** That is the point, and it is also
  the cost: a single-CSR shop cannot approve its own corrections, and an
  after-hours fix waits for a second person.
- **A rejected proposal is kept**, and cannot be deleted. `pending_movements`
  accumulates rows that never became money, which is exactly the evidence D8 says
  is missing today. The cost is that the table only grows, on the same footing as
  `ledger_entries`; archival is a separate decision for both.
- **`adjust-balance` and `waive-fee` change shape for their callers.** They stop
  returning a new balance, because there is not one yet. The frontend has to
  show "submitted for approval" and a queue, which is UI work this ADR does not
  cover.
- **D8 closes when this lands, not when ADR 0010 does.** ADR 0010 makes it
  answerable; this is the answer.
