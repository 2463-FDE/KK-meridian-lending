# ADR 0011: Maker-checker for servicing balance adjustments

- **Status:** Proposed — depends on ADR 0010
- **Date:** 2026-08-12
- **Bears on:** `docs/DEBT.md` D8 (any authenticated user can adjust a balance or
  waive a fee, with no second approver and no record of who asked)

## Required invariants

What must hold for an implementation to be this decision.

1. **A proposal is not a movement.** Writing a `pending_movements` row moves no
   money; only the approval writes a ledger entry.
2. **Exactly one terminal transition per proposal**, ever. A resolved proposal
   cannot be re-resolved, and its substance cannot change after it is raised.
3. **No self-approval.** The requester may not be the approver, enforced at the
   database and inside the resolving function.
4. **An approval produces exactly one ledger entry; a rejection produces none.**
   Checked at COMMIT, because the resolving transaction is legitimately mid-state.
5. **The entry matches the proposal field-for-field** — loan, component, amount,
   type — so an approval cannot authorise different terms than the ones reviewed.
6. **The ledger actor is the approver**, overwritten by trigger rather than
   trusted from the caller. The requester survives on the proposal.
7. **A rejected proposal is retained.** It is the evidence D8 says is missing.
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

> *Illustrative and non-runnable.* Shape of the decision, not the migration —
> the executable version lands in the migration PR where it can be tested.

```sql
CREATE TABLE pending_movements (
    id            BIGSERIAL   PRIMARY KEY,
    loan_id       INTEGER     NOT NULL REFERENCES loans(id),
    component     TEXT        NOT NULL,
    amount        NUMERIC(14,2) NOT NULL,
    entry_type    TEXT        NOT NULL CHECK (entry_type IN ('adjustment','fee_waived')),
    reason        TEXT        NOT NULL,      -- required: a proposal without one is unreviewable
    requested_by  INTEGER     NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Terminal state, written once. This table is NOT append-only: resolving a
    -- proposal is the one legitimate mutation in the design, and it is confined
    -- here precisely so the ledger's guarantee stays absolute.
    resolution    TEXT        CHECK (resolution IN ('approved','rejected')),
    resolved_by   INTEGER,
    resolved_at   TIMESTAMPTZ,
    ledger_entry_id BIGINT    REFERENCES ledger_entries(id),

    CONSTRAINT no_self_approval CHECK (resolved_by IS NULL OR resolved_by <> requested_by),
    CONSTRAINT resolution_complete CHECK (
        (resolution IS NULL     AND resolved_by IS NULL AND resolved_at IS NULL)
     OR (resolution IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
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

-- Exactly one terminal transition. pending -> approved or pending -> rejected,
-- and never anything afterwards: not approved -> rejected, not a second approver
-- overwriting the first, not an amount edited after review. Without this the
-- table is mutable in the ordinary sense and "who approved this" becomes a
-- question about the latest writer rather than about a decision.
CREATE FUNCTION pending_movements_single_transition() RETURNS trigger AS $$
BEGIN
    IF OLD.resolution IS NOT NULL THEN
        RAISE EXCEPTION 'pending movement % is already %', OLD.id, OLD.resolution;
    END IF;
    IF NEW.loan_id     IS DISTINCT FROM OLD.loan_id
    OR NEW.component   IS DISTINCT FROM OLD.component
    OR NEW.amount      IS DISTINCT FROM OLD.amount
    OR NEW.entry_type  IS DISTINCT FROM OLD.entry_type
    OR NEW.requested_by IS DISTINCT FROM OLD.requested_by THEN
        RAISE EXCEPTION 'the substance of a pending movement is immutable';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER pending_movements_one_way
    BEFORE UPDATE ON pending_movements
    FOR EACH ROW EXECUTE FUNCTION pending_movements_single_transition();

-- The approved entry must BE the movement that was approved. A unique link, so
-- one approval can never yield two entries and an entry cannot be attributed to
-- a proposal it does not match.
-- adjustment and fee_waived are the maker-checker subjects, so they may only
-- enter the ledger through an approved proposal.
ALTER TABLE ledger_entries ADD CONSTRAINT approved_entries_have_a_proposal CHECK (
    (entry_type IN ('adjustment','fee_waived') AND pending_movement_id IS NOT NULL)
 OR (entry_type NOT IN ('adjustment','fee_waived') AND pending_movement_id IS NULL)
);
```

**One column is added here, and none in ADR 0010's migration** —
`pending_movement_id`. A column belongs in the migration that can enforce its
invariant, and this one cannot be enforced without `pending_movements` to point
at: the foreign key, the `UNIQUE` constraint and the validation trigger all need
the table to exist.

`approved_required` and `approved_at` exist in **neither** ADR. They would have
been a second, writable answer to "was this approved" sitting beside the
proposal's own resolution, and the only thing they buy is an avoided join.
`approved_required` is derivable from `entry_type` and `approved_at` is
`pending_movements.resolved_at`.

Adding it here makes the two tables reference each other, which is a genuine
cycle: an entry points at the proposal that authorised it, and the proposal
points back at the entry it produced. Split the ordinary way — create the column
and the table, then add the foreign key — together with the commit-time rule
that replaces the impossible CHECK:

> *Illustrative and non-runnable.* Shape of the decision, not the migration —
> the executable version lands in the migration PR where it can be tested.

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

-- Approval implies exactly one entry; rejection implies none. Checked at COMMIT,
-- because the resolving transaction is legitimately mid-state between marking the
-- movement approved and linking the entry it just inserted.
CREATE FUNCTION pending_movement_resolution_is_complete() RETURNS trigger AS $$
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

CREATE CONSTRAINT TRIGGER pending_movements_resolution_complete
    AFTER INSERT OR UPDATE ON pending_movements
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION pending_movement_resolution_is_complete();
```

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

> *Illustrative and non-runnable.* Shape of the decision, not the migration —
> the executable version lands in the migration PR where it can be tested.

```sql
-- Signature only; the body lands with step 4's migration.
CREATE FUNCTION ledger_entry_matches_its_proposal() RETURNS trigger;

CREATE TRIGGER ledger_entries_match_proposal
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_matches_its_proposal();
```

**What it must reject**, for `adjustment` and `fee_waived` entries only —
machine-originated types (`payment`, `fee_assessed`, `disbursement`,
`opening_balance`) have no proposal and pass straight through:

- a NULL `pending_movement_id`, or one naming no proposal;
- a proposal that is not `approved`;
- a proposal whose approver is missing or equal to its requester;
- any disagreement on `loan_id`, `component`, `amount` or `entry_type` between
  the entry and the proposal it names.

And one thing it must **overwrite** rather than validate: `actor_id` is set to
the proposal's approver, so a direct INSERT cannot misattribute who authorised
the movement even while reproducing everything else correctly.

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
- **A rejected proposal is kept.** `pending_movements` accumulates rows that
  never became money, which is exactly the evidence D8 says is missing today.
- **`adjust-balance` and `waive-fee` change shape for their callers.** They stop
  returning a new balance, because there is not one yet. The frontend has to
  show "submitted for approval" and a queue, which is UI work this ADR does not
  cover.
- **D8 closes when this lands, not when ADR 0010 does.** ADR 0010 makes it
  answerable; this is the answer.
