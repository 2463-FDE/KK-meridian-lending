# ADR 0010: An append-only ledger for servicing balances

- **Status:** Proposed
- **Date:** 2026-08-11
- **Author:** In-house team
- **Closes:** the ADR Week 6 owed and never produced (`docs/ROADMAP.md`, G-ADR-0010)
- **Bears on:** `docs/DEBT.md` D3, D8, D14 — see *What this closes* below for which of the three this actually closes

## What this ADR decides

**It approves one thing: servicing balances become a projection of an
append-only ledger.** That is the decision under review, and it is
implementable on its own — PR-1 to PR-3 of the migration plan close D3 without
any maker-checker work existing.

**Maker-checker is described here but not approved here.** It appears because it
is the reason the ledger is shaped the way it is: a separate `pending_movements`
table rather than approval columns on the ledger, and an `entry_type` set that
distinguishes human-authorised movements from machine-originated ones. Those are
ledger decisions, and they are the ones that need approving now — if the ledger
shipped without them, retrofitting maker-checker would mean migrating the money
table a second time.

So what the maker-checker section below commits to is the **requirements** PR-4
must satisfy and the schema the ledger has to expose for it. The design itself
gets its own PR, its own tests, and its own review. A reviewer who disagrees with
the approval workflow can still approve this ADR; a reviewer who disagrees that
`pending_movements` should be a separate table cannot, because that is a ledger
decision.

## Two sequences, two label spaces

These are different things and an earlier revision numbered both 1-5, which made
"step 3" ambiguous between "the third PR" and "the third deploy action".

- **PR-1 … PR-5** — the **implementation sequence**: what lands, in which pull
  request, and the gate each must pass before the next is written.
- **R1 … R5** — the **production rollout**: the order operations performs on a
  live system, once the code exists. R1 does not correspond to PR-1.
- **G1 … G5** — **gates**: conditions checked between rollout actions. G1 and G2
  make the mixed-deploy safe; G3 to G5 are the cutover freeze.

Nothing reuses a label across the three.

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
3. **`balances` is a projection, not a record.** `SUM(ledger_entries)` is the
   auditable truth.

   **This becomes true at PR-5, and not before.** Earlier revisions asserted
   "written by the projection and nothing else" as though it held from PR-3,
   while also saying `adjust_balance` and `waive_fee` keep writing directly until
   ADR 0011 lands. Both cannot be true, and an implementer following the first
   sentence would attach the guard while two writers were still bypassing it,
   breaking every staff adjustment and waiver in production.

   The sequence is therefore fixed, and every other number in this document
   follows from it:

   | Step | What converts | Why it is here |
   |---|---|---|
   | PR-3 | `apply_payment`, `apply_payment_once`, `assess_late_fee` | The three machine paths. No approval semantics, so nothing blocks them. Closes D3. |
   | PR-4 | **ADR 0011 maker-checker** | Must precede the staff paths: converting them first would write unapproved staff money movements into an append-only table that cannot be corrected. |
   | PR-5 | `adjust_balance`, `waive_fee`, then the write-guard | Only now are all five writers converted, so only now can the guard be attached and the "only writer" claim made. |

   Until PR-5 completes, `balances` has **two** writers -- the projection and the
   two staff paths -- and this document says so rather than claiming the
   invariant early.

   "Nothing else" is a claim about FIVE call sites, so all five are named — the
   invariant is useless if an implementer converts three of them:

   | Writer | Column | Becomes |
   |---|---|---|
   | `balance.py::apply_payment` | `balance` | a `payment` entry |
   | `balance.py::apply_payment_once` | `balance` | a `payment` entry (the idempotent path; same entry, guarded by the unique index) |
   | `balance.py::adjust_balance` | `balance` | an `adjustment` entry — **requires ADR 0011** (see the interim rule below) |
   | `balance.py::waive_fee` | `past_due` | a `fee_waived` entry — **requires ADR 0011** (see the interim rule below) |
   | **`delinquency.py::assess_late_fee`** | `past_due` | a `fee_assessed` entry |

   The last one is the one to miss: it lives outside the balance module, so a
   conversion that works through `balance.py` and stops leaves late fees writing
   `past_due` directly — and PR-5's write-guard then makes every late-fee
   assessment raise, in a nightly job, after the guard is on. The step-3 gate
   enumerates writers **from source** (`grep 'UPDATE balances'` across
   `services/`) rather than from this table, because a table is a list and lists
   go stale.
   **The interim rule for `adjust_balance` and `waive_fee`, stated once because
   three places disagreed about it.** Earlier revisions said all five writers move
   in PR-3, that these two arrive "via ADR 0011", and that maker-checker is
   optional. Those cannot all be true. The rule:

   > **ADR 0011 is REQUIRED before the write-guard is enabled.** Until it lands,
   > `adjust_balance` and `waive_fee` keep writing `balances` directly and are
   > frozen during the cutover window (gate G3). PR-5 does not run until 0011
   > has converted them.

   So maker-checker is not optional *to this plan* even though it is a separate
   decision: the ledger can ship and run without it (PR-1 to PR-3 close D3), and the
   guard cannot be turned on without it. That is the honest shape — the ledger
   does not depend on maker-checker, and the *contract* attached to it does.

   The alternative — let these two write `adjustment` entries directly, with an
   actor and a reason and no approver — was rejected. It would put unapproved
   staff money movements into an append-only table that cannot be corrected,
   which is a worse permanent record than the mutable column they write today.

4. **The sign of an entry is keyed to what the borrower owes**, per the component
   table below: `balance` for `principal`, `past_due` for `fees`, and `interest`
   projects nowhere.
5. **Every entry that a human directed carries that human.** `actor_id` and
   `actor_role` are required except for machine-originated types, enforced by a
   CHECK rather than by convention.
6. **Parity holds per loan, not in aggregate**: `balances.balance` equals the sum
   of that loan's `principal` entries, and `past_due` the sum of its `fees`
   entries, with `interest` excluded from both.
7. **Exactly one entry per payment/component pair.** The unique index is
   `(payment_id, component)`, so an apply may write one `principal` row, one
   `interest` row and one `fees` row for the same payment — which is what makes
   the D14 waterfall expressible — and a retry cannot duplicate any of them.

   **This invariant needs a `payment_id` to hold, and one write path has none.**
   `balance.py::apply_payment()` takes `(loan_id, amount)`, and
   `payments.py::charge()` — the legacy `POST /payments` on servicing —
   `INSERT`s the payment without `RETURNING id` and then calls it. An entry from
   that path would carry `payment_id = NULL`, and **NULLs do not collide in a
   unique index**, so a retried charge would post the balance twice with nothing
   stopping it. The index would look like protection and provide none.

   **The rule: that path is converted, not excluded.** PR-3 changes its
   `INSERT` to `INSERT ... RETURNING id` and passes the real id into
   `apply_payment_once()`, which is the idempotent writer the modern path already
   uses. `apply_payment()` is then deleted rather than left beside it — a
   non-idempotent writer kept "for the legacy route" is how the route stays
   legacy.

   Freezing the route was the alternative and it is worse: `POST /payments` on
   servicing is a duplicate of payment-service's own endpoint (D2), so freezing
   it leaves a live money route that cannot be reconciled. Excluding it from the
   invariant is worse still — an invariant with a documented exception on the one
   path that lacks the key is not an invariant.

   Stated this way because "exactly one entry per payment" is the version that
   contradicts both the index and D14: a waterfall splits one payment across
   components by definition, so a single-row rule would forbid the thing the
   ledger is being built to allow. Idempotency is unaffected — the retry
   protection comes from the pair being unique, not from the payment appearing
   once.

## Every invariant, its step, and how it will be checked

An invariant with no step never lands, and one with no check is a sentence. This
is the table a reviewer should hold the implementation to, and each check is
mechanical rather than a judgement.

| # | Invariant | Lands in | Machine-checkable acceptance criterion |
|---|---|---|---|
| 1 | Entries are immutable | PR-2 | `UPDATE` and `DELETE` on `ledger_entries` both raise, asserted against real Postgres |
| 2 | A signed delta, never a total | PR-2 | `amount <> 0` CHECK; two entries inserted in one statement both apply, so the balance moves by their sum |
| 3 | `balances` written only by the projection | PR-5 (see the rule below — not PR-3) | `grep 'UPDATE balances'` across `services/` returns only the projection's own statement; with the guard on, a direct `UPDATE` raises |
| 4 | The sign is keyed to what the borrower owes | PR-2 | a `payment` with a positive amount, and a `fee_assessed` with a negative one, are both refused by CHECK |
| 5 | A human-directed entry names the human | PR-2 | an `adjustment` or `fee_waived` with a NULL `actor_id` is refused; a `payment` without one is accepted |
| 6 | Per-loan parity, excluding `interest` | PR-2 gate, re-run after the PR-3 delta pass | for every loan: `balances.balance = SUM(principal)` and `past_due = SUM(fees)`; reported per loan, never as one total |
| 7 | One entry per `(payment_id, component)` | PR-2 (index), PR-3 (the id) | the same `payment_id` twice for one component raises `UniqueViolation`; and **no ledger-writing path may pass a NULL `payment_id` for a `payment` entry** — the check that catches the legacy route |

The last cell is the one that matters most, because it is the invariant that
looks satisfied by the index alone. A test asserting only that duplicates are
rejected would pass on a system where every `payment` entry has a NULL id and
nothing is deduplicated at all.

## What this closes

One statement, referred to rather than repeated:

| Debt | This ADR | Why |
|---|---|---|
| **D3** — lost update on `balances` | **Closes** at PR-3 | The read-modify-write disappears; entries are appended and the projection is the trigger's job |
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
    -- unique index on (payment_id, component) means a retry cannot create a
    -- second entry FOR THE SAME COMPONENT, so servicing stops needing
    -- payment_applications to tell it whether it already ran. One payment may
    -- still write one row per component -- that is the waterfall (D14), and it is
    -- why the index is on the pair rather than on payment_id alone.
    payment_id   INTEGER     REFERENCES payments(id),

    -- No approval columns. approved_required, approved_at and
    -- pending_movement_id all belong to ADR 0011, which ships them with the
    -- pending_movements table that gives them meaning. A ledger-only migration
    -- has nothing to approve: in PR-1 to PR-3 every entry is machine-originated
    -- or a direct staff write, and the concept of an entry awaiting approval
    -- does not exist yet.
    --
    -- Keeping them here would put approval state in two places -- a boolean and
    -- a timestamp on the entry, and a resolution on the proposal -- which is a
    -- second approval model competing with the real one, and the kind of
    -- duplication that ends with the two disagreeing about the same movement.

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

### `past_due` is in scope, and it ships in PR-1 to PR-3

**Fee movements go in the ledger, and the `past_due` projection lands with the
ledger — PR-1 to PR-3, not with maker-checker.** The ordering matters in the
direction that is easy to get wrong: `waive_fee` is one of the two actions
maker-checker governs, so the fees component has to exist *before* anything
approves a waiver, or PR-4 would be approving movements the ledger cannot
represent.

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
    -- No approval check here. ADR 0011 adds one, keyed on the proposal rather
    -- than on a column of this table: an 'adjustment' or 'fee_waived' entry must
    -- name an approved pending_movement. That is single-sourced and strictly
    -- stronger than a boolean the inserting statement sets for itself.
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

  Not zeroed and reprojected: that needs a pause to be safe, because a live
  payment can land on a zeroed balance, and the rollout below does not pause.
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

| PR | What lands | Gate before the next PR |
|---|---|---|
| **PR-1** | The failing test for the lost update, against today's code | It fails, and the failure is the correctly-paired race — not the client's wrong repro |
| **PR-2** | `ledger_entries` (no approval columns — see ADR 0011), the triggers, and the back-fill | Parity green PER LOAN, not in aggregate, and **excluding `interest`**, for every loan with no balance movement since its opening entry. Loans that did move are expected to differ — see the delta pass |
| **PR-3** | The three MACHINE writers move to the ledger — `apply_payment`, `apply_payment_once` and `delinquency.assess_late_fee` outside the balance module, and the legacy `POST /payments` path converted to `INSERT ... RETURNING id` + `apply_payment_once()` so its entries carry a real `payment_id` | PR-1's test now passes. **D3 closes.** Gate: `grep 'UPDATE balances'` across `services/` returns the projection trigger's own statement **and the two staff paths** (`adjust_balance`, `waive_fee`), which are still direct writers until PR-5. The zero-direct-writer form of this check belongs to PR-5 alone -- requiring it at PR-3 would either block PR-3 for ever or force the staff paths to convert before an approval path exists, which is the unapproved-movement risk this ADR is avoiding |
| **PR-4** | *(ADR 0011)* `pending_movements`, `ledger_entries.pending_movement_id` and the `ALTER` closing the cycle, `resolve_pending_movement()`, maker-checker on adjust and waive | Tests: self-approval refused; a resolved proposal cannot be re-resolved; an approval writes exactly one ledger entry whose loan, component, amount and entry_type match the proposal; a rejection writes none; two concurrent approvers produce one entry |
| **PR-5** | The two STAFF writers move to the ledger — `balance.py::adjust_balance` and `balance.py::waive_fee`, each now raising a `pending_movements` proposal and writing its entry only on approval (PR-4) — then the direct-write guard is attached to `balances` | Gate: `grep 'UPDATE balances'` across `services/` now returns the projection trigger's statement and **nothing else**; a direct `UPDATE balances` raises; invariant 3 becomes true here and not before; per-loan parity green after the cutover freeze is released (G4, G5) |
| **PR-6** | *(separate change, not this ADR)* the payment waterfall — D14 | Allocation tests: order, short payments, partial periods |

**The back-fill is the risky step, and it is lossy in one direction that must be
stated rather than discovered.** Historical rows have a balance but no history:
there is no record of which past movements produced today's number, because that
is precisely what D8 says was never kept. So the back-fill writes one
`entry_type = 'opening_balance'` row per loan carrying the current balance. It is
not a reconstruction of the past — **it is an explicit admission that the past is
unavailable**, and the distinct type is what keeps that admission legible, so no
one later mistakes an opening balance for an audited movement.

`opening_balance` is its own value in the `entry_type` CHECK rather than a
`disbursement` flagged as reconstructed: reusing a real event type would make a
reconstructed balance indistinguishable from an actual disbursement to every
query that asks.

## The minimum slice that is ADR-compliant

A reader should not have to infer where the first required boundary sits, so:

**PR-1 to PR-3 are the whole obligation.** An implementation that lands those and
stops is compliant with this ADR. Concretely, it must have all four of:

1. a **failing** test for the lost update against today's code, landed before
   anything is built, because a fix for a defect nobody demonstrated is
   unfalsifiable;
2. `ledger_entries` with the sign convention, the `entry_type` set, and the
   actor constraint as specified — the three things every later reader depends
   on and none of which can be changed later without rewriting rows;
3. the projection trigger as the only writer of the **machine** paths — the
   legacy `POST /payments` route and `delinquency.assess_late_fee` included.
   `adjust_balance` and `waive_fee` are explicitly **excluded** and still write
   `balances` directly at this point, so the "only writer" claim is NOT yet
   true and this ADR does not make it;
4. per-loan parity green, `interest` excluded.

**The write-guard is NOT in the minimum slice.** It is PR-5, which depends on
PR-4 (ADR 0011): `adjust_balance` and `waive_fee` cannot be converted without an
approval path, and cannot keep writing directly once the guard is on. Converting
them BEFORE maker-checker exists would write unapproved staff money movements
into an append-only table that cannot be corrected — a worse permanent record
than the mutable column they write today.

A system that stops at PR-3 is compliant with this ADR: the ledger is
authoritative for the machine paths, D3 is closed, and `balances` still has two
unguarded doors — `adjust_balance` and `waive_fee` — which the cutover freeze
(G3) and the per-loan parity check cover in the interim.

**Everything else is optional to this ADR, with one dependency.** The approval
function, `past_due` maker-checker and the waterfall can be declined, deferred or
decided differently without contradicting anything here. The single exception is
the one above: **enabling the write-guard requires ADR 0011.** Optional to the
decision, required for the last step of the rollout.

**PR-2 ships no maker-checker columns at all.** Not `pending_movement_id`, and
not the `approved_required` / `approved_at` pair that an earlier draft of this
plan proposed.

ADR 0011 adds **`pending_movement_id` only**, together with the
`pending_movements` table that gives it meaning and the trigger that enforces
it. It deliberately does **not** add `approved_required` or `approved_at` --
approval state lives on the proposal, so there is no denormalised copy on
`ledger_entries` that can drift out of step with it.

An earlier revision of this paragraph listed all three columns as arriving with
ADR 0011. That contradicted 0011 -- which says the other two exist in neither
document -- and would have led an implementer to build exactly the duplicated
state both ADRs reject.

The rule that keeps this honest: a column belongs in the migration that can
enforce its invariant. `approved_required` in a ledger-only schema is a boolean
the inserting statement sets for itself, which enforces nothing; the same column
next to `pending_movements` is checkable against a resolution a second person
made.

## What is binding, and what is illustration

Every SQL block here is labelled *illustrative and non-runnable*, and a label is
easy to read past. So, explicitly:

**Binding.** An implementation that does not do these is not this decision:

1. the seven *Required invariants* above;
2. the `entry_type` set and the sign convention in the component table — later
   readers depend on both, and neither can be changed afterwards without
   rewriting rows;
3. the unique index on `(payment_id, component)`;
4. `balances` written only by the projection trigger, with the write-guard
   enabled;
5. immutability enforced by a trigger, not by convention or privilege;
6. the migration step order and the gates on each step, including the freeze
   gates G3-G5;
7. per-loan parity excluding `interest`, asserted after the delta pass.

**Illustration.** Column types and lengths, index names, constraint names, the
exact text of an exception, the shape of the DO-block, and anything in a function
body. The migration PR is free to differ, and where the SQL here and a migration
disagree on any of those, **the migration is right** — it is the one that runs.

If an implementer finds the two disagreeing on something in the binding list,
that is a defect in this document and it should be fixed here first.

## Non-goals, and the PRs that are required

Not in this ADR, and not in whatever PR implements its first step:

| PR | Concern | Why it is separate | Gate |
|---|---|---|---|
| **PR-1** | The failing lost-update test against today's code | It has to fail before anything is built, or the fix is unfalsifiable | The failure is the correctly-paired race, not a wrong repro |
| **PR-2** | Ledger schema + projection trigger migration | Runnable DDL belongs where it executes and can be tested | Parity per loan, seeded and back-filled, with `interest` excluded (it projects nowhere) |
| **PR-3** | Write-path conversion, machine paths only — the staff pair waits for PR-4 | Independently revertible; this is the step that touches live money | PR-1's test now passes. **D3 closes** |
| **PR-4** | Maker-checker: `pending_movements`, `resolve_pending_movement()`, adjust and waive | Its own design decision (see above), reviewable on its own merits | Every numbered requirement above, each failing when removed |
| **PR-5** | Staff-path conversion + write-guard | Cannot precede PR-4: converting `adjust_balance` and `waive_fee` before an approval path exists would write unapproved staff money movements into a table that cannot be corrected | A direct `UPDATE balances` raises; zero direct writers remain outside the projection; parity green per loan |
| **PR-6** | Payment waterfall | The allocation algorithm is unrelated to how balances are stored | Allocation tests: order, short payments, partial periods |

Explicitly **not** goals of any of the above:

- **Closing D14** — see *What this closes*. The algorithm is PR-6.
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

| Action | What is done | Why this order |
|---|---|---|
| **R1** | Deploy the schema: tables, projection trigger, and the `balances` write-guard **disabled** | The guard is a separate `ALTER TABLE ... ENABLE TRIGGER`, so schema and enforcement land in different deploys and either can be reverted alone |
| **R2** | Back-fill `opening_balance` entries, with writes still going to `balances` directly | Nothing reads the ledger yet, so a wrong back-fill is a table to truncate, not an incident |
| **R3** | Run parity in report-only mode over every loan | Fails here cost nothing — see below |
| **R4** | Deploy `balance.py` writing ledger entries instead of `balances`, then run the **delta pass** | The projection trigger now maintains `balances`; the old path is gone in the same deploy that adds the new one. The delta pass closes the gap the back-fill could not — see below |
| **R5** | Enable the write-guard | Last, because until PR-4 is everywhere, a straggler pod still writing `balances` directly would start erroring |

**Writes are not paused and not dual-written — conditional on gate G1.** That
condition is load-bearing and was missing from an earlier revision, which claimed
the cutover was safe on the strength of "PR-4 is atomic per write". It is not
safe without G1: until every pod writes `balances` relatively, an old pod's
absolute write can overwrite a new pod's projection, and the ledger keeps both
entries while the balance loses one. **If G1 is not fully deployed, pause payment
applies for the cutover window** — the pause is the fallback, not the plan, and
saying so is the difference between a plan and an assumption.

Both alternatives were considered:

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

### The mixed-deploy race, and the gate that closes it

"PR-4 is atomic per write" is true and it is not sufficient, which an earlier
version of this section missed. During the deploy both versions run, and they do
not compose:

- the **old** path reads `balances`, subtracts in the application, and writes an
  **absolute** value back — `UPDATE balances SET balance = %s`
  (`balance.py::apply_payment_once`);
- the **new** path writes a ledger entry, and the projection trigger applies a
  **relative** change — `SET balance = balance + NEW.amount`.

Interleave them and the old pod's write, computed from a read taken before the
new pod's entry landed, overwrites it. The ledger keeps both entries, `balances`
loses one payment, and the delta pass cannot repair it: the ledger is right, so
nothing looks missing. **Silently wrong customer balances**, which is the exact
failure this whole ADR exists to remove.

**The gate: gate G1 — make the old path's write relative before any ledger entry
is ever written.**

```sql
-- Illustrative and non-runnable.
-- The old path, converted from "compute and set" to "let the database subtract".
UPDATE balances
   SET balance = balance - %s, updated_at = now()
 WHERE loan_id = %s;
```

That single change makes the two paths **commutative**: both are now deltas
applied by Postgres under its own row lock, so any interleaving of old and new
pods produces the same balance. It ships and fully deploys *before* PR-4 —
which is why it is a numbered gate and not a note.

| Gate | Before | Check |
|---|---|---|
| **G1** | before PR-3 (the first ledger write) | every `balances` write in `servicing-service` is relative (`balance = balance ± …`) or is a frozen route. Enumerated from source, not listed here — see the writer inventory below |
| **G2** | before PR-3 | G1 is deployed to **every** pod. A single old pod still doing read-modify-write reintroduces the race by itself |

Three alternatives were considered and rejected. A **version fence** (new pods
refuse to write until all old ones are gone) needs coordination this system has
nowhere to keep. A **per-loan advisory lock** across both code paths means the
old path taking a lock it has no other reason to take, shipped in the same
release that is being replaced. **Pausing payment applies** works and costs the
most: refused payments, retries, and late fees assessed on payments we declined.
The atomic decrement costs one statement.

**It also partly fixes D3 on its own**, and that is worth naming rather than
discovering: the lost update *is* the read-modify-write, so making it relative
removes the interleaving D3 describes. It does not make the ledger unnecessary —
D3's fix has to survive `adjust_balance`, which genuinely sets an absolute value,
and the audit trail is the other half of why the ledger exists. But gate G1 is
the cheapest real improvement in this plan, and it can land first.

### The delta pass, and why the back-fill alone is not enough

Between the back-fill and the last pod finishing PR-4, live payments move
`balances` directly and write **no** ledger entry. The ledger is therefore behind
by exactly those payments, and no amount of care in the back-fill fixes it,
because they had not happened yet when it ran. This is the part the "no pause"
decision costs, and it has to be paid rather than argued away.

It is payable exactly, because the movements are already recorded. Servicing's
idempotency guard writes one `payment_applications` row per applied payment, so
after PR-4 completes:

> *Illustrative and non-runnable.* Shape of the decision, not the migration —
> the executable version lands in the migration PR where it can be tested. See
> Appendix A.

```sql
-- Every payment applied after its loan's opening entry with no PRINCIPAL entry.
-- Component-qualified deliberately -- see the scope note below.
SELECT pa.*
  FROM payment_applications pa
  JOIN ledger_entries oe ON oe.loan_id = pa.loan_id
                        AND oe.entry_type = 'opening_balance'
 WHERE pa.applied_at > oe.occurred_at
   AND NOT EXISTS (
         SELECT 1 FROM ledger_entries le
          WHERE le.payment_id = pa.payment_id
            AND le.component = 'principal');
```

Those get `payment` entries written **with the projection suppressed**, for the
same reason the opening entries were: `balances` already reflects them. The delta
pass is idempotent by that `NOT EXISTS`, so it can be run repeatedly and run
again if it is interrupted.

**Scope, because the unqualified version of this query is wrong.** An earlier
form matched on `le.payment_id = pa.payment_id` alone, which contradicts
invariant 7: with one row per component, a payment that already has its
`principal` entry would be skipped while its `fees` entry was still missing, and
the balance would be quietly short. The `component = 'principal'` clause is what
makes the check agree with the index.

And the pass can only ever reconstruct principal. `payment_applications` stores
one `amount` per payment with no component breakdown — the split does not exist
in that table because the waterfall does not exist yet (D14, PR-6). So:

- **the delta pass is for pre-waterfall, principal-only payments, and that is
  all it is for.** It runs during cutover, when `apply_payment_once` writes
  principal alone, and it is correct for exactly that window;
- **after the waterfall lands it must not be reused as-is.** A payment split
  across components cannot be rebuilt from a single stored amount by any query.
  Once D14 ships, the ledger is the only place the split exists, which is
  precisely why the waterfall comes after the cutover rather than during it.

Parity is asserted **after** the delta pass. Asserting it before is asserting
something the design says will be false.

Two things this does not cover: adjustments and fee waivers made through
`adjust-balance` / `waive-fee` during the window leave no `payment_applications`
row, so they are invisible to the query above. Both are staff actions on a
specific loan and both are rare.

**They are frozen for the duration, and that freeze is a gate rather than a
note.** Prose asking an operator to remember something during a cutover is not a
control -- the delta pass would knowingly miss those movements, and the balance
would be wrong with nothing failing:

| Gate | Before | Check |
|---|---|---|
| **G3** | PR-2 (back-fill) | `adjust-balance` and `waive-fee` return 503 for the duration of the cutover, from a flag the deploy sets — not from an operator's memory |
| **G4** | PR-5 (convert the staff pair, then enable the write-guard) | zero `balances` rows have `updated_at` inside the cutover window without a matching ledger entry. This is the assertion that the freeze actually held; if it fails, a staff write got through and has to be reconstructed by hand before the guard goes on |
| **G5** | releasing the freeze | G4 green, and per-loan parity green after the delta pass |

G4 is the one that matters. A freeze nobody verified is indistinguishable from a
freeze that leaked, and the leak is silent: the projection and the ledger simply
disagree by one adjustment nobody remembers making.

### Locks

PR-5 is the one to watch. The guard is a `BEFORE UPDATE OR DELETE` row trigger
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

Where it fails decides what to do, which is why PR-3 exists before PR-4:

- **After the back-fill (PR-3), before the write-path switch.** Nothing reads
  the ledger yet. Truncate `ledger_entries`, fix the back-fill, run it again.
  There is no rollback because there was no cutover. **This is the step whose job
  is to catch the back-fill being wrong, and it is free.**
- **After the write-path switch (PR-4 or 5).** Do NOT reconcile by writing to
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
  right tool for both jobs -- subject to the same principal-only scope: a revert
  after D14 has shipped cannot be repaired by this query, and the rollback plan
  for that world is ADR 0011's and D14's own, not this one's. The `opening_balance` entry is the marker that says
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
  PR-3 rather than left.
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
second approver. It makes PR-4 larger and gives `balances` a second derived
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
is the copy nobody notices is wrong.** So the two procedures are stated as
requirements instead — what each must guarantee, every item of which becomes a
test in the PR that writes it.

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

