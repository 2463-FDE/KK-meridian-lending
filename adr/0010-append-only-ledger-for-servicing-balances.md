# ADR 0010: An append-only ledger for servicing balances

- **Status:** Proposed
- **Date:** 2026-08-11
- **Author:** In-house team
- **Closes:** the ADR Week 6 owed and never produced (`docs/ROADMAP.md`, G-ADR-0010)
- **Bears on:** `DEBT.md` D3, D8, D14

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

```sql
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

    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ledger_entries_payment_component
    ON ledger_entries (payment_id, component) WHERE payment_id IS NOT NULL;

CREATE INDEX ledger_entries_loan ON ledger_entries (loan_id, occurred_at);

-- A human-DIRECTED entry must name the human. Machine-originated ones must not
-- be forced to invent one.
--
-- Review fix: this first exempted only 'disbursement' and 'fee_assessed', which
-- made actor_id/actor_role mandatory for 'payment' -- and servicing's
-- apply-payment carries neither. It receives an amount and a payment_id, because
-- the borrower is not "acting" on the balance in the sense this column means;
-- the processor captured a payment and servicing is posting it. The constraint
-- as written would have failed every real payment on insert, which is the kind
-- of defect that only shows up once the migration is live.
--
-- 'payment' is exempt, and its provenance is stronger than an actor string
-- anyway: payment_id points at the row carrying the idempotency key, the amount
-- and the capture. 'opening_balance' is exempt because no one authored it.
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_actor_required CHECK (
    entry_type IN ('disbursement','fee_assessed','payment','opening_balance')
    OR (actor_id IS NOT NULL AND actor_role IS NOT NULL)
);
```

`'opening_balance'` is the back-fill's marker, and it is a distinct `entry_type`
rather than an `is_reconstructed` boolean. Review fix again: the migration plan
said opening entries would be "marked as reconstructed" and the schema had no
such field, so the marking existed only in the prose describing it. A separate
boolean would have worked; a distinct `entry_type` is better here because it
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
work, and D14 does too: a payment writes one row per component, so the waterfall
is the data model instead of a calculation layered on top.

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

### Maker-checker: a proposal is not a movement

> **Revised after review.** The first version of this section added
> `approved_by`/`approved_at` columns to `ledger_entries` and had the approver
> `UPDATE` the pending row. **That cannot work** — the append-only trigger two
> sections up rejects every `UPDATE` on that table, so the approval path was
> blocked by the ADR's own central guarantee. Recorded rather than quietly
> rewritten because it is a good illustration of the failure it describes: the
> immutability claim and the approval claim were each checked on their own and
> never against each other.

The fix is not to weaken the trigger. It is to stop putting proposals in the
ledger at all.

**A row in `ledger_entries` means money moved.** A proposed adjustment is not a
movement — it is a request that may never become one. Those are different facts
and they get different tables:

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
    -- An approval must produce exactly one ledger entry; a rejection must produce none.
    CONSTRAINT approved_has_entry CHECK (
        (resolution = 'approved' AND ledger_entry_id IS NOT NULL)
     OR (resolution IS DISTINCT FROM 'approved' AND ledger_entry_id IS NULL)
    )
);
```

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
D8 and D14 open, and leaves maker-checker with nowhere to live. It is strictly
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
| 2 | `ledger_entries`, triggers, back-fill from `payments` + current `balances` | Parity test green: projection == `SUM` for every loan, seeded and back-filled |
| 3 | Writes move to the ledger; `balances` written only by the trigger | Step 1's test now passes. D3 and D14 close |
| 4 | `pending_movements`, maker-checker on adjust and waive | A test proving self-approval is refused, and one proving an approval writes exactly one ledger entry |

**The back-fill is the risky step, and it is lossy in one direction that must be
stated rather than discovered.** Historical rows have a balance but no history:
there is no record of which past movements produced today's number, because that
is precisely what D8 says was never kept. So the back-fill writes one
`entry_type = 'opening_balance'` row per loan carrying the current balance. It is
not a reconstruction of the past — **it is an explicit admission that the past is
unavailable**, and the distinct type is what keeps that admission legible, so no
one later mistakes an opening balance for an audited movement.

*Review fix: this said the entry would be a `disbursement` "marked as
reconstructed", and no marker existed anywhere in the schema — the distinction
lived only in this sentence. Worse, `disbursement` is a real event type, so a
reconstructed opening balance would have been indistinguishable from an actual
loan disbursement by any query. `opening_balance` is now its own type in the
`entry_type` CHECK.*

## Consequences

**Good.**

- "Who changed this balance, when, why, and who approved it" becomes a `SELECT`.
  D8 answerable for the first time.
- D3 closes without a lock, and D14 without a separate feature.
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

## Open question for the reviewer

`past_due` is left alone by this ADR. `waive_fee` writes it, `assess_late_fee`
writes it, and both are money-affecting actions that arguably belong in the
ledger as `fees` component entries — at which point `past_due` becomes a second
projection and the `balances` table has two derived columns rather than one.

That is defensible and it is more change. The alternative is a ledger that covers
`balance` movements only, leaving fee accrual on the old mutable path, which
means the audit trail has a hole exactly where fee waivers are — and fee waivers
are one of the two actions Week 6's brief named as needing a second approver.

**Recommendation: fold `past_due` in at step 4**, when maker-checker lands, since
waivers are the reason maker-checker exists. Flagged rather than decided, because
it changes the size of step 4 materially.
