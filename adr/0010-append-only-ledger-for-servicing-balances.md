# ADR 0010: An append-only ledger for servicing balances

- **Status:** Proposed
- **Date:** 2026-08-11
- **Author:** In-house team
- **Closes:** the ADR Week 6 owed and never produced (`docs/ROADMAP.md`, G-ADR-0010)
- **Bears on:** `DEBT.md` D3, D8, D14 — see *What this closes* below for which of the three this actually closes

## What this ADR decides

**It approves one thing: servicing balances become a projection of an
append-only ledger.** That is the decision under review, and it is
implementable on its own — steps 1 to 3 of the migration plan close D3 without
any maker-checker work existing.

**Maker-checker is described here but not approved here.** It appears because it
is the reason the ledger is shaped the way it is: a separate `pending_movements`
table rather than approval columns on the ledger, and an `entry_type` set that
distinguishes human-authorised movements from machine-originated ones. Those are
ledger decisions, and they are the ones that need approving now — if the ledger
shipped without them, retrofitting maker-checker would mean migrating the money
table a second time.

So what the maker-checker section below commits to is the **requirements** step 4
must satisfy and the schema the ledger has to expose for it. The design itself
gets its own PR, its own tests, and its own review. A reviewer who disagrees with
the approval workflow can still approve this ADR; a reviewer who disagrees that
`pending_movements` should be a separate table cannot, because that is a ledger
decision.

## Required invariants

What must hold for an implementation to be this decision. Everything after this
section is the reasoning behind them; a reviewer who reads only this list has the
approvable content.

1. **A ledger entry is immutable.** `UPDATE` and `DELETE` on `ledger_entries` are
   refused by a trigger, the same mechanism `decision_events` uses and for the
   same reason — every service connects as the schema owner, so a `REVOKE` does
   not stick.
2. **An entry is a signed delta, never a total.** Two concurrent entries compose;
   they cannot lose each other's write. This is what closes D3.
3. **`balances` is a projection, not a record.** It is written by the projection
   trigger and by nothing else, enforced by a guard trigger. `SUM(ledger_entries)`
   is the auditable truth.
4. **The sign of an entry is keyed to what the borrower owes**, per the component
   table below: `balance` for `principal`, `past_due` for `fees`, and `interest`
   projects nowhere.
5. **Every entry that a human directed carries that human.** `actor_id` and
   `actor_role` are required except for machine-originated types, enforced by a
   CHECK rather than by convention.
6. **Parity holds per loan, not in aggregate**: `balances.balance` equals the sum
   of that loan's `principal` entries, and `past_due` the sum of its `fees`
   entries, with `interest` excluded from both.
7. **An applied payment produces exactly one entry.** `payment_id` is unique on
   the ledger, so a retried apply cannot post twice.

## What this closes

One statement, referred to rather than repeated:

| Debt | This ADR | Why |
|---|---|---|
| **D3** — lost update on `balances` | **Closes** at step 3 | The read-modify-write disappears; entries are appended and the projection is the trigger's job |
| **D8** — no maker-checker, no audit of who moved money | **Makes answerable**, closes in ADR 0011 | The ledger is where a proposal can point once approved; the workflow is a separate decision |
| **D14** — no payment waterfall | **Enables, does not close** | A payment can write one row per component, which is the prerequisite. The allocation algorithm — order, short payments, partial periods — is its own change with its own tests |

The D14 line is the one that has been overclaimed before, so to be exact: a
schema that *can* record a fees/interest/principal split is not a system that
*decides* the split. This ADR gives the first and neither specifies nor delivers
the second.

## Context

Week 6's brief asked for a servicing dashboard and said *"reps are trusted folks —
don't over-engineer permissions, just make it usable."* The review that followed
produced a legacy-comprehension finding, an RBAC fix at the gateway, and a
promise of an ADR proposing RBAC, maker-checker and an append-only ledger. The
RBAC half shipped. **The ADR was never written**, so the other two halves have
had nowhere to be decided for five weeks, and the Weeks 1–6 audit lists them as
`Not started` with the note that they are blocked on a schema decision rather
than on effort. This is that decision.

Today `balances` is one mutable row per loan:

> *Illustrative and non-runnable.* Shape of the decision, not the migration —
> the executable version lands in the migration PR where it can be tested. See
> Appendix A.

```sql
CREATE TABLE IF NOT EXISTS balances (
    loan_id     INTEGER PRIMARY KEY REFERENCES loans(id),
    balance     NUMERIC(14,2) NOT NULL,    -- D12: was DOUBLE PRECISION, UPDATE-d in place
    past_due    NUMERIC(14,2) DEFAULT 0,   -- D12: was DOUBLE PRECISION
    updated_at  TIMESTAMPTZ DEFAULT now()
);
```

(Quoted verbatim from `db/init/001_schema.sql`, including its own comments — the
`NUMERIC` migration recorded there closed D12 and is unrelated to what follows.
The defect is not the column type. It is that there is one row and it is
overwritten.)

Three defects follow from that shape, and they are usually treated as three
problems. They are one.

**D3 — the lost update.** `balance.apply_payment` is a read-modify-write with no
lock:

```python
current = get_balance(loan_id)                                   # READ
new_balance = float(_to_decimal(current) - _to_decimal(amount))  # MODIFY
db.query("UPDATE balances SET balance = %s WHERE loan_id = %s",  # WRITE
         (new_balance, loan_id))
```

Two concurrent payments both read 500, both write, and one payment vanishes. The
client's own reported repro for this was *wrong* — they paired a payment with a
fee waiver, and `waive_fee` writes `past_due`, a different column, so that exact
pairing never collided. The real repro needs two writers on the same column: two
`apply_payment` calls, or `apply_payment` racing `adjust_balance`. **Week 6 asked
for a failing test proving it and none was ever written**, which is why the
defect is still open and still unproven in CI.

**D8 — no audit trail.** `adjust_balance` overwrites the column outright; its own
docstring says *"No ledger entry; the prior value is gone forever."* A controller
asking "who changed this balance, when, and why" cannot be answered at all — not
slowly, not approximately. The data does not exist.

**D14 — no waterfall.** A payment is subtracted straight off `balance`. There is
nowhere to record that $50 went to fees, $30 to interest and $120 to principal,
because there is one number and it has no components.

## Decision

**Record money movements as immutable rows. Derive the balance from them.**

> *Illustrative and non-runnable.* Shape of the decision, not the migration —
> the executable version lands in the migration PR where it can be tested. See
> Appendix A.

```sql
-- Full runnable DDL is in Appendix A; the excerpts below are the shape of the
-- decision, not the migration file.
--
-- ADR 0011 adds pending_movement_id to this table, and the two tables then
-- reference each other: an entry points at the proposal that authorised it, and
-- the proposal points back at the entry it produced. That cycle is 0011's to
-- split -- create both, then add the second foreign key with an ALTER -- and it
-- is named here only so nobody designs around a constraint this migration does
-- not have.
CREATE TABLE ledger_entries (
    id           BIGSERIAL   PRIMARY KEY,
    loan_id      INTEGER     NOT NULL REFERENCES loans(id),

    -- What moved. A signed delta, never a total: an entry says "this much
    -- changed", so two concurrent entries compose instead of racing.
    component    TEXT        NOT NULL CHECK (component IN ('principal','interest','fees')),
    amount       NUMERIC(14,2) NOT NULL CHECK (amount <> 0),

    -- Why it moved.
    -- 'opening_balance' is the back-fill's marker and nothing else may use it:
    -- it means "this loan's balance as it stood when the ledger began, with no
    -- record of how it got there". See the migration plan.
    entry_type   TEXT        NOT NULL CHECK (entry_type IN
                   ('opening_balance','disbursement','payment',
                    'fee_assessed','fee_waived','adjustment')),
    reason       TEXT,

    -- Who moved it. Null for machine-initiated entries (a scheduled late fee);
    -- required for anything a human directed -- enforced below, because "who"
    -- is the whole question D8 asks and a nullable column would not answer it.
    actor_id     INTEGER,
    actor_role   TEXT,

    -- Provenance. payment_id makes an apply idempotent by construction: the
    -- unique index means the second attempt to post the same payment cannot
    -- create a second entry, so servicing stops needing payment_applications
    -- to tell it whether it already ran.
    payment_id   INTEGER     REFERENCES payments(id),

    -- Read by the projection trigger below, so they ship with this table: a
    -- trigger cannot reference a column added later, and adding them afterwards
    -- means rewriting it on a live money table.
    approved_required   BOOLEAN NOT NULL DEFAULT false,
    approved_at         TIMESTAMPTZ,

    -- pending_movement_id is NOT here. It belongs to ADR 0011 and ships with the
    -- table it points at. An earlier draft put it in this migration, justified by
    -- the same "the trigger reads it" argument as the two columns above -- which
    -- is true of them and false of it, as the trigger below shows. A maker-checker
    -- column in a ledger-only migration needed a reason, and that was the reason.

    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ledger_entries_payment_component
    ON ledger_entries (payment_id, component) WHERE payment_id IS NOT NULL;

CREATE INDEX ledger_entries_loan ON ledger_entries (loan_id, occurred_at);

-- A human-DIRECTED entry must name the human. Machine-originated ones must not
-- be forced to invent one.
--
-- 'payment' is exempt: servicing's apply-payment receives an amount and a
-- payment_id and no actor, because the borrower is not "acting" on the balance
-- in the sense this column means -- the processor captured a payment and
-- servicing is posting it. Requiring an actor here would fail every real payment
-- on insert. Its provenance is stronger than an actor string anyway: payment_id
-- points at the row carrying the idempotency key, the amount and the capture.
-- 'opening_balance' is exempt because no one authored it.
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_actor_required CHECK (
    entry_type IN ('disbursement','fee_assessed','payment','opening_balance')
    OR (actor_id IS NOT NULL AND actor_role IS NOT NULL)
);
```

`'opening_balance'` is the back-fill's marker, and it is a distinct `entry_type`
rather than an `is_reconstructed` boolean. A separate boolean would have worked;
a distinct `entry_type` is better here because it
cannot be defaulted, cannot be forgotten on insert, and every query that means
"real money movements" is already filtering on `entry_type` — so the exclusion is
expressed in the same vocabulary rather than as a second thing to remember.

Append-only is enforced by a trigger, not by a `GRANT` — the same reasoning
`decision_events` settled in ADR 0006, and for the same reason: every service
connects as the schema-owning role, so revoking `UPDATE` from the owner does not
stick. A `BEFORE UPDATE OR DELETE` trigger raising an exception does.

### The race disappears rather than being locked

This is the part worth stating plainly, because it changes what work remains.

The conventional fix for D3 is `SELECT … FOR UPDATE` around the read-modify-write.
That works. But an append-only ledger has **no read-modify-write to protect** —
posting a payment is an `INSERT` of a delta, and two concurrent inserts both
land. Nothing is overwritten, so nothing can be lost.

D3 therefore closes as a *consequence* of this design rather than as separate
work, and it is the prerequisite for D14 (see *What this closes*).

**This is not a reason to skip the failing test.** Week 6 asked for a test proving
the lost update, and it should still be written first, against today's code, and
watched to fail — otherwise the claim that the ledger fixes the race is exactly
the kind of assertion this engagement keeps catching: plausible, undemonstrated,
and believed because it sounds structural. The test that fails on `balances` and
passes on the ledger is the evidence. Without it we would be trusting an argument.

### Balance is a projection, maintained by trigger

The truth is `SUM(amount)` over the ledger. The *read path* is a cached
projection in the existing `balances` row, updated by the same trigger that
guards immutability.

`balances` keeps its shape and its callers. `balance.get_balance`,
`servicing-service`'s loan reads, the frontend, the E2E specs — none of them
change. What changes is who writes it: nothing, except the trigger.

Correctness is held by a test, not by discipline:

```
for every loan: balances.balance == SUM(ledger_entries.amount)
```

run in `db/tests` against real PostgreSQL, over seeded and post-backfill data.

### What a balance means, and which way the numbers point

Left undefined, this is the part that produces two subtly different
implementations six months apart. Decided here.

**`balances.balance` is the outstanding principal**, not a total receivable. That
is what it means today -- `apply_payment` subtracts the whole payment from it and
`schedule.py` amortises against it -- and redefining it would silently change
every existing read, including the borrower's own "My loan" screen. Interest is
not carried on the balance and is not accrued as a stored figure anywhere in this
system; the schedule computes it per period. So the `interest` component exists
in the ledger for the waterfall to allocate against, and it does **not** project
into `balances.balance`.

**`balances.past_due` is fees owed**, and is included in this design -- see the
decision below.

**Signed deltas, one rule: the sign is the effect on what the borrower owes.**
Negative reduces the debt, positive increases it. No entry type is exempt, so a
reader never has to remember a per-type convention.

| `entry_type` | `component` | Sign | Projects into |
|---|---|---|---|
| `opening_balance` | `principal` | **+** | `balance` |
| `disbursement` | `principal` | **+** | `balance` |
| `payment` | `principal` | **−** | `balance` |
| `payment` | `fees` | **−** | `past_due` |
| `payment` | `interest` | **−** | *(neither -- see above)* |
| `fee_assessed` | `fees` | **+** | `past_due` |
| `fee_waived` | `fees` | **−** | `past_due` |
| `adjustment` | `principal` | ± | `balance` |
| `adjustment` | `fees` | ± | `past_due` |

A CHECK enforces the ones that are not genuinely bidirectional, so a `payment`
that increases a balance cannot be written at all:

> *Illustrative and non-runnable.* Shape of the decision, not the migration —
> the executable version lands in the migration PR where it can be tested. See
> Appendix A.

```sql
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_sign_matches_type CHECK (
    (entry_type IN ('opening_balance','disbursement','fee_assessed') AND amount > 0)
 OR (entry_type IN ('payment','fee_waived')                          AND amount < 0)
 OR (entry_type = 'adjustment')            -- genuinely bidirectional
);
```

Only `adjustment` may go either way, which is precisely why it is the entry type
that requires an actor, a reason, and (below) a second approver.

**Who the actor is, in one rule:** `ledger_entries.actor_id` is whoever *authorised* the movement. For an `adjustment` or `fee_waived` that is the **approver**, not the requester -- the ledger answers "who allowed this money to move". The requester is preserved rather than discarded, by the linkage ADR 0011 adds: `pending_movements.requested_by` holds it, and the entry's `pending_movement_id` is UNIQUE, so both people are recoverable from either direction. Recorded here because it decides what `actor_id` MEANS on this table, which is a ledger question and has to be answered before anything writes to it. Machine-originated entries (`payment`, `fee_assessed`, `disbursement`, `opening_balance`) have no actor, and the CHECK exempts exactly those.

### `past_due` is in scope, and it ships in steps 1-3

**Fee movements go in the ledger, and the `past_due` projection lands with the
ledger — not with maker-checker.** Step 4 previously listed a "`past_due`
projection" as well, which read as though fees waited on the approval workflow.
They do not, and the ordering matters in the direction that is easy to get wrong:
`waive_fee` is one of the two actions maker-checker exists to govern, so the fees
component has to be recorded *before* anything approves a waiver. A maker-checker
step that had to introduce its own projection would be approving movements the
ledger could not yet represent.

Leaving them out would put a hole in the audit trail exactly where fee waivers
are -- and waivers are one of the two actions Week 6 named as needing a second
approver, so the maker-checker work would have had nowhere to record half of its
own subject matter. `waive_fee` and `assess_late_fee` are money-affecting writes
to a mutable column with no history, which is the same defect as the balance, one
column over.

The cost is that `balances` now has **two** derived columns rather than one, and
the parity test has to cover both:

```
for every loan:
    balances.balance  == SUM(amount) WHERE component = 'principal'
    balances.past_due == SUM(amount) WHERE component = 'fees'
```

### How the projection is maintained

**Incrementally, by an AFTER INSERT trigger on `ledger_entries`, never by
recompute.** A recompute-on-read would make every balance read O(entries) on a
table that grows without bound; a periodic recompute would leave a window in
which the projection and its ledger disagree with nothing detecting it.

> *Illustrative and non-runnable.* Shape of the decision, not the migration —
> the executable version lands in the migration PR where it can be tested. See
> Appendix A.

```sql
CREATE FUNCTION project_ledger_entry() RETURNS trigger AS $$
BEGIN
    IF NEW.approved_required AND NEW.approved_at IS NULL THEN
        RAISE EXCEPTION 'unapproved entries must not be written to the ledger';
    END IF;
    IF NEW.component = 'principal' THEN
        UPDATE balances SET balance  = balance  + NEW.amount, updated_at = now()
         WHERE loan_id = NEW.loan_id;
    ELSIF NEW.component = 'fees' THEN
        UPDATE balances SET past_due = past_due + NEW.amount, updated_at = now()
         WHERE loan_id = NEW.loan_id;
    END IF;                      -- 'interest' projects nowhere, by definition above
    RETURN NEW;
END $$ LANGUAGE plpgsql;
```

Three things this depends on, each of which is a way to get it wrong:

- **Initialisation.** The back-fill writes one `opening_balance` entry per loan
  equal to that loan's current balance, **with the projection suppressed for
  that insert**, because `balances` already holds the number the entry records.

  An earlier version of this section said the back-fill zeroes `balances` and
  reprojects through the trigger, which is wrong twice over. It is a window in
  which a live payment can land on a zeroed balance and be lost, and it
  contradicts the rollout section a few pages down, which says direct writes
  continue during the back-fill. Reprojecting is the tidier story and it needs a
  pause to be true; suppressing the projection needs nothing, because the value
  is already there.
- **Direct writes are blocked.** `balances` becomes trigger-maintained only: a
  `BEFORE UPDATE OR DELETE` trigger on `balances` raises unless the projection
  function is the one writing. Otherwise `balance.py`'s existing `UPDATE
  balances` statements -- or a psql session -- would silently desynchronise the
  projection from its own ledger, and the parity test would only notice
  afterwards.

  **How the guard knows.** A session-local setting, set by the projection
  function around its own writes and cleared immediately after:

  ```sql
  -- inside project_ledger_entry(), around the UPDATEs shown above
  PERFORM set_config('meridian.projecting', 'on', true);   -- true = transaction-local
  ...                                                       -- the UPDATE balances
  PERFORM set_config('meridian.projecting', 'off', true);

  -- and the guard itself
  CREATE FUNCTION balances_are_trigger_maintained() RETURNS trigger AS $$
  BEGIN
      IF current_setting('meridian.projecting', true) IS DISTINCT FROM 'on' THEN
          RAISE EXCEPTION 'balances is maintained by the ledger projection; '
                          'write a ledger entry instead';
      END IF;
      RETURN NEW;
  END $$ LANGUAGE plpgsql;
  ```

  `set_config(..., true)` is transaction-local, so the flag cannot leak to a
  later statement on a pooled connection -- the way this kind of guard usually
  fails open.

  It is a guard against accident, not a privilege boundary: anyone who can run
  `set_config` can bypass it. That is the same honest limit as the append-only
  trigger and for the same reason -- every service connects as the schema-owning
  role, so a `REVOKE` from the owner does not stick (ADR 0002, ADR 0006).
- **The projection is not the record.** `SUM(ledger_entries)` remains the
  auditable truth; `balances` is a cache with a test asserting it agrees.

### Maker-checker needs somewhere to put a proposal

The ledger's shape is decided partly by a feature this ADR does **not** approve.
Two things follow from it and are decided here, because retrofitting either one
means migrating the money table a second time:

- proposals live in their own table, **not** as `approved_by`/`approved_at`
  columns on `ledger_entries` -- an approver updating a ledger row is exactly
  what the append-only trigger forbids, and a rejected proposal has to survive
  as evidence, which a column on an entry that was never written cannot do;
- `entry_type` distinguishes human-authorised movements (`adjustment`,
  `fee_waived`) from machine-originated ones (`payment`, `fee_assessed`,
  `disbursement`, `opening_balance`), because only the first kind needs an
  approver and the constraint that says so has to be able to tell them apart.

Everything else -- the approval function, its guarantees, the validation trigger,
the actor rule -- is **Appendix B**, which is a requirements list for a later ADR
and not a design approved by this one.

## Alternatives considered

**Pure event sourcing — no `balances` table, `SUM` on every read.** Rejected, and
it is the closer call. It is the cleaner model and removes the projection as a
thing that can drift. But every read path in `servicing-service` and the frontend
currently reads `balances.balance`, so this converts a contained schema addition
into a change touching every consumer, and it makes balance reads O(entries) on
the hot path with no cache to fall back to. The projection keeps the blast radius
small and keeps `SUM` available as the auditable truth; a parity test buys the
property that pure event sourcing gets structurally. If the projection ever does
drift in a way the test catches in CI, revisit this — that would be evidence the
trade was wrong, and it is the specific trigger to reopen this decision.

**`SELECT … FOR UPDATE` on `balances`, and stop there.** Closes D3 only. Leaves
D8 open and D14 impossible, and leaves maker-checker with nowhere to live. It is strictly
less work, and it is the right answer only if the ledger is never built — in
which case D8 stays permanently unanswerable, which Week 6 already rejected.

**An `audit_log` table alongside the mutable balance.** Rejected: two sources of
truth that can disagree, and the disagreement is undetectable because neither is
derived from the other. It also does not fix D3 or D14 — it only records that
they happened.

## Migration plan

Expand and contract, the same shape as the PAN/CVV removal (`0029` → `0031`),
which is in this repository specifically because a big-bang drop on a money table
is not recoverable.

| Step | What lands | Gate before the next step |
|---|---|---|
| 1 | The failing test for the lost update, against today's code | It fails, and the failure is the correctly-paired race — not the client's wrong repro |
| 2 | `ledger_entries` (with `approved_required` / `approved_at`, which the projection trigger reads), the triggers, and the back-fill | Parity green PER LOAN, not in aggregate, and **excluding `interest`**, for every loan with no balance movement since its opening entry. Loans that did move are expected to differ — see the delta pass |
| 3 | Writes move to the ledger; `balances` written only by the trigger | Step 1's test now passes. **D3 closes** |
| 4 | *(ADR 0011)* `pending_movements`, `ledger_entries.pending_movement_id` and the `ALTER` closing the cycle, `resolve_pending_movement()`, maker-checker on adjust and waive | Tests: self-approval refused; a resolved proposal cannot be re-resolved; an approval writes exactly one ledger entry whose loan, component, amount and entry_type match the proposal; a rejection writes none; two concurrent approvers produce one entry |
| 5 | *(separate change, not this ADR)* the payment waterfall — D14 | Allocation tests: order, short payments, partial periods |

**The back-fill is the risky step, and it is lossy in one direction that must be
stated rather than discovered.** Historical rows have a balance but no history:
there is no record of which past movements produced today's number, because that
is precisely what D8 says was never kept. So the back-fill writes one
`entry_type = 'opening_balance'` row per loan carrying the current balance. It is
not a reconstruction of the past — **it is an explicit admission that the past is
unavailable**, and the distinct type is what keeps that admission legible, so no
one later mistakes an opening balance for an audited movement.

`opening_balance` is its own value in the `entry_type` CHECK rather than a
`disbursement` "marked as reconstructed": a marker that exists only in prose is
not a marker, and reusing a real event type would make a reconstructed balance
indistinguishable from an actual disbursement to every query that asks.

## The minimum slice that is ADR-compliant

A reader should not have to infer where the first required boundary sits, so:

**Steps 1 to 3 are the whole obligation.** An implementation that lands those and
stops is compliant with this ADR. Concretely, it must have all four of:

1. a **failing** test for the lost update against today's code, landed before
   anything is built, because a fix for a defect nobody demonstrated is
   unfalsifiable;
2. `ledger_entries` with the sign convention, the `entry_type` set, and the
   actor constraint as specified — the three things every later reader depends
   on and none of which can be changed later without rewriting rows;
3. the projection trigger as the **only** writer of `balances`, with the
   write-guard enabled;
4. per-loan parity green, `interest` excluded.

**Everything else is optional to this ADR.** `pending_movements`, the approval
function, `past_due` maker-checker, the waterfall: all of it can be declined,
deferred, or decided differently without contradicting anything decided here.

The columns that are **not** optional in step 2 are `approved_required` and
`approved_at`, because the projection trigger reads them and a trigger cannot
reference a column added afterwards — leaving them out means rewriting the
trigger later on a live money table.

`pending_movement_id` is **not** one of them. An earlier version of this section
claimed it was, on the same "the trigger reads it" reasoning; the trigger shown
above reads `approved_required` and `approved_at` and nothing else. The claim was
wrong, and it was the load-bearing justification for shipping a maker-checker
column in a ledger-only migration. It ships with ADR 0011, alongside the table it
points at and the foreign key that constrains it.

## Non-goals, and the PRs that are required

Not in this ADR, and not in whatever PR implements its first step:

| # | Follow-up PR | Why it is separate | Gate |
|---|---|---|---|
| 1 | The failing lost-update test against today's code | It has to fail before anything is built, or the fix is unfalsifiable | The failure is the correctly-paired race, not a wrong repro |
| 2 | Ledger schema + projection trigger migration | Runnable DDL belongs where it executes and can be tested | Parity per loan, seeded and back-filled, with `interest` excluded (it projects nowhere) |
| 3 | Write-path conversion — `balances` written only by the trigger | Independently revertible; this is the step that touches live money | PR 1's test now passes. **D3 closes** |
| 4 | Maker-checker: `pending_movements`, `resolve_pending_movement()`, adjust and waive | Its own design decision (see above), reviewable on its own merits | Every numbered requirement above, each failing when removed |
| 5 | Payment waterfall | The allocation algorithm is unrelated to how balances are stored | Allocation tests: order, short payments, partial periods |

Explicitly **not** goals of any of the above:

- **Closing D14** — see *What this closes*. The algorithm is PR 5.
- **Reconstructing history.** The back-fill writes one `opening_balance` row per
  loan and admits the past is unavailable. See the migration plan.
- **Changing what a borrower sees.** `balances.balance` keeps its current meaning
  and its current readers; only the writer changes.
- **Interest accrual, fee assessment schedules, or delinquency rules.** The
  ledger records movements; it does not decide when they happen.

## Production rollout and rollback

The steps above say what lands. This says how it lands on a live money table,
because "expand and contract" names a strategy and not a procedure, and the
difference is where this goes wrong.

### The cutover, in order

| # | Action | Why this order |
|---|---|---|
| 1 | Deploy the schema: tables, projection trigger, and the `balances` write-guard **disabled** | The guard is a separate `ALTER TABLE ... ENABLE TRIGGER`, so schema and enforcement land in different deploys and either can be reverted alone |
| 2 | Back-fill `opening_balance` entries, with writes still going to `balances` directly | Nothing reads the ledger yet, so a wrong back-fill is a table to truncate, not an incident |
| 3 | Run parity in report-only mode over every loan | Fails here cost nothing — see below |
| 4 | Deploy `balance.py` writing ledger entries instead of `balances`, then run the **delta pass** | The projection trigger now maintains `balances`; the old path is gone in the same deploy that adds the new one. The delta pass closes the gap the back-fill could not — see below |
| 5 | Enable the write-guard | Last, because until step 4 is everywhere, a straggler pod still writing `balances` directly would start erroring |

**Writes are not paused and not dual-written.** Both were considered:

- *Pausing* means refusing payments during the cutover. On a servicing system
  that is a queue of retries, angry borrowers, and late fees assessed on
  payments we refused to take.
- *Dual-writing* means `balance.py` writing both the ledger and `balances`
  directly for a period, then stopping. It sounds safer and is not: while both
  paths write, the projection trigger and the direct write both target
  `balances`, so they race each other and the ledger's own projection is what
  loses. Dual-writing is right when the two destinations are independent. Here
  the second destination is derived from the first, so writing both is writing
  the same number twice by two rules that can disagree.

What makes the pause unnecessary is that step 4 is atomic per write, not per
system. Every payment either wrote the old way or the new way; none wrote half.
A pod mid-deploy is writing one or the other correctly, and `balances` is right
under both — the old path writes it directly, the new path writes it through the
projection.

### The delta pass, and why the back-fill alone is not enough

Between the back-fill and the last pod finishing step 4, live payments move
`balances` directly and write **no** ledger entry. The ledger is therefore behind
by exactly those payments, and no amount of care in the back-fill fixes it,
because they had not happened yet when it ran. This is the part the "no pause"
decision costs, and it has to be paid rather than argued away.

It is payable exactly, because the movements are already recorded. Servicing's
idempotency guard writes one `payment_applications` row per applied payment, so
after step 4 completes:

> *Illustrative and non-runnable.* Shape of the decision, not the migration —
> the executable version lands in the migration PR where it can be tested. See
> Appendix A.

```sql
-- Every payment applied after its loan's opening entry that has no ledger entry.
SELECT pa.*
  FROM payment_applications pa
  JOIN ledger_entries oe ON oe.loan_id = pa.loan_id
                        AND oe.entry_type = 'opening_balance'
 WHERE pa.applied_at > oe.occurred_at
   AND NOT EXISTS (SELECT 1 FROM ledger_entries le WHERE le.payment_id = pa.payment_id);
```

Those get `payment` entries written **with the projection suppressed**, for the
same reason the opening entries were: `balances` already reflects them. The
delta pass is idempotent by that `NOT EXISTS`, so it can be run repeatedly and
run again if it is interrupted.

Parity is asserted **after** the delta pass. Asserting it before is asserting
something the design says will be false.

Two things this does not cover, stated rather than discovered: adjustments and
fee waivers made through `adjust-balance` / `waive-fee` during the window leave
no `payment_applications` row, so they are invisible to the query above. Both are
staff actions on a specific loan and both are rare. **Freeze them for the
duration** — a CSR waiting an hour is a different order of problem from a
borrower's balance being wrong — and the delta pass covers everything else.

### Locks

Step 5 is the one to watch. The guard is a `BEFORE UPDATE OR DELETE` row trigger
on `balances`, so it takes no table lock at write time — but installing it does:
`CREATE TRIGGER` takes `SHARE ROW EXCLUSIVE`, which waits for in-flight writes to
that table and blocks new ones while it waits. That is a short wait on a healthy
system and an unbounded one behind a long transaction.

So: `SET lock_timeout = '3s'` around the `CREATE TRIGGER`, and retry rather than
queue. A migration that blocks every payment while waiting for a lock it may not
get is worse than one that fails and is run again.

The projection trigger itself locks a single `balances` row per entry, which is
the same row `apply_payment_once` locks today. No new contention shape.

### If parity fails

Where it fails decides what to do, which is why step 3 exists before step 4:

- **After the back-fill (step 3), before the write-path switch.** Nothing reads
  the ledger yet. Truncate `ledger_entries`, fix the back-fill, run it again.
  There is no rollback because there was no cutover. **This is the step whose job
  is to catch the back-fill being wrong, and it is free.**
- **After the write-path switch (step 4 or 5).** Do NOT reconcile by writing to
  `balances`: that is the thing under suspicion, and correcting the projection by
  hand destroys the evidence of how it diverged. Disable the write-guard, revert
  `balance.py` to the direct-write path, and leave the ledger in place accruing
  nothing. The system is then back to today's behaviour, the divergence is still
  measurable, and no borrower balance was touched.

  **What happens to the entries already written.** They stay, and they stay
  authoritative for the period they cover. They are not orphans: every one of
  them already moved `balances` through the projection trigger, so the borrower's
  balance is correct and the entry is the record of why. Nothing needs undoing —
  deleting them would destroy the only history the system has ever had.

  What this costs is that the ledger now has a **hole**: entries up to the
  revert, nothing after it. So a second attempt at cutover must **not** re-run
  the back-fill, which would write a second `opening_balance` on top of history
  that is already there and double every loan that has one. It resumes from the
  last entry instead, with the same delta query, whose `NOT EXISTS` makes it the
  right tool for both jobs. The `opening_balance` entry is the marker that says
  which loans have already been initialised, which is a second reason it is its
  own `entry_type` rather than a flag.

The parity check is `SUM(amount) GROUP BY loan_id, component` against `balances`,
which is the same query the ongoing test uses. A per-loan report, never a single
aggregate: one loan wrong by +$100 and another by -$100 sums to zero, and a
system that reports "in balance" while two borrowers are wrong is worse than one
that reports nothing.

`interest` entries are excluded from the principal comparison, deliberately and
not as an oversight -- see the sign table above. An implementer who includes them
will chase a phantom mismatch on every loan that has ever accrued interest.

## Consequences

**Good.**

- "Who changed this balance, when, why, and who approved it" becomes a `SELECT`.
  D8 answerable for the first time.
- D3 closes without a lock.
- D14 becomes **possible**, not closed (*What this closes*). The ledger gives a payment somewhere to
  record a fees/interest/principal split, which is the prerequisite -- but the
  allocation algorithm (what order, what happens when a payment is short, how
  partial periods behave) is a separate change with its own tests, and this ADR
  neither specifies nor delivers it. Saying D14 closes "without a
  separate feature", which overstated it: a schema that *can* hold a waterfall is
  not a waterfall.
- Maker-checker has somewhere to live, and rejected proposals survive as evidence
  rather than being discarded.
- The projection is a plain `SUM` with no approval filter, so no reader can
  forget one and silently count unapproved money.
- Payment posting is idempotent by unique index, so `payment_applications` stops
  being the only thing standing between a retry and a double-post.

**Costs, stated rather than discovered later.**

- **Two tables instead of one.** Splitting proposals out of the ledger is what
  keeps the append-only guarantee absolute, but it means a money movement can now
  be described in two places, and `pending_movements` is deliberately *not*
  append-only — resolving a proposal mutates it. That mutation is the single
  exception in the whole design, and it is confined to a table the balance is
  never derived from. If that exception ever needs to grow, it is a sign this
  split was drawn in the wrong place.

- `ledger_entries` grows without bound. No retention policy is proposed here, and
  under SOX these are exactly the records that must be kept — so growth is the
  intended behaviour, not an oversight, and archival is a separate decision.
- The projection can drift. A trigger bug means `balances` disagrees with its own
  ledger, and the read path would serve the wrong number silently. The parity
  test is the only thing standing between that and production, which is an
  argument for running it in CI rather than on request.
- Every write path in `balance.py` changes. That file also holds
  `apply_payment_once`, whose `payment_applications` guard becomes redundant —
  redundant is not harmful, but leaving two idempotency mechanisms where one is
  authoritative is its own confusion, and it should be removed deliberately in
  step 3 rather than left.
- The back-fill's opening entries are not real history and never will be.

**Explicitly not claimed.** This is not a general-ledger or double-entry
accounting system: entries are single-sided deltas against one loan, with no
contra account and no trial balance. It answers the servicing audit question Week
6 asked. It is not an accounting system of record, and calling it one later would
be the same category of overclaim as the README's PCI-DSS banner.

## Answered, previously open

**Does `past_due` fold into the ledger?** Yes -- see "`past_due` is in scope"
above. Leaving it out would have put a hole in the audit trail exactly where fee
waivers are, and waivers are one of the two actions Week 6 named as needing a
second approver. It makes step 4 larger and gives `balances` a second derived
column, both recorded in Consequences.

**What remains genuinely undecided** is nothing in this ADR's own scope. The
allocation order for D14's waterfall is deliberately out of scope, and the
retention policy for `ledger_entries` is a separate decision noted in
Consequences.

## Appendix A — why there is no runnable SQL here

The SQL in this document is the **shape of the decision**, not the migration:
table sketches whose constraints each carry an invariant, and — for the two
procedures — signatures with the guarantees they owe.

Partial, unexecuted SQL gets treated as authoritative by whoever implements it,
and then drifts from the migration that actually runs. **The copy nobody applies
is the copy nobody notices is wrong.** So `resolve_pending_movement()` and
`ledger_entry_matches_its_proposal()` are stated as requirements instead — what
each must guarantee, every item of which becomes a test in the PR that writes
it.

Three short trigger bodies survive, deliberately: `project_ledger_entry()`,
`pending_movements_single_transition()` and
`pending_movement_resolution_is_complete()`. Each is one invariant in about a
dozen lines, and the projection trigger in particular *is* the decision — "the
balance is a projection" is a claim about four lines of `UPDATE`. Restating them
in prose would be longer than the code and less precise, which is the failure
mode in the other direction.

The line to hold: **a procedure gets specified, an invariant gets shown.** If a
block grows a branch that needs its own test, it has stopped being an invariant
and belongs in a migration.

The full runnable DDL belongs in the migration files created by steps 2 and 4 of
the plan above, not here. An ADR that grows into a migration stops being read as a
decision -- reviewers noted this one was heading that way at 600+ lines -- and a
schema that lives in two places drifts, with the copy nobody applies quietly
becoming wrong.

What this document is responsible for, and what a later reader should hold it to:

- **the decision** — an append-only ledger with a trigger-maintained projection,
  and why the alternatives were rejected;
- **the invariants** — the seven listed under *Required invariants* above:
  immutability, signed deltas, the projection, the sign convention, the actor
  requirement, per-loan parity and one entry per applied payment. The approval
  invariants — one terminal transition, no self-approval, the actor is the
  approver, an entry matches its proposal — belong to ADR 0011 and are listed
  there;
- **the sequencing** — which step can land before which, and what gate each one
  has to pass;
- **the costs** — two derived columns, unbounded growth, a projection that can
  drift, and the fact that D14 is enabled rather than closed.

## Appendix B — moved

The maker-checker requirements that were here are now
`adr/0011-maker-checker-for-servicing-adjustments.md`, so this ADR's approval
surface matches the decision it actually makes. What stays above is only what the
LEDGER decides about it: proposals need their own table, and `entry_type` has to
separate human-authorised movements from machine-originated ones. Both are
settled here because retrofitting either means migrating the money table twice.

