# Runbook: dropping `loans.apr`

The **contract** half of an expand-and-contract migration
(`db/migrations/0039_drop_loans_apr.sql`). It destroys data and can break
running services. It is not a migration to run because it is next in the folder.

It follows the shape of `docs/RUNBOOK-pan-cvv-contract.md`, deliberately — that
was the previous contract step on a money table, and reusing its sequencing is
cheaper than inventing a second one.

## What is actually wrong, and why a rename is not cosmetic

`loans.apr` has held **two different regulated figures**, depending on which
boarding path created the row:

| Boarded by | `apr` holds | For the reference contract |
|---|---|---|
| the current path | the contractual **note rate** | 7.99% |
| the pre-change path | the **disclosed APR** | 5.196% |

They are not interchangeable and never were. The disclosed APR additionally
carries the prepaid origination fee, so it is the higher figure whenever a fee
exists. Servicing **amortizes this column** — so billing a row that holds the
disclosed APR charges the borrower above their own disclosure.

The money has been right since PR #10: the API serializes `note_rate_pct` and
the UI labels it "Interest rate". What remained was the **name**, which is what
anyone reading SQL, a dump or `db/init` meets. That is D19.

## Order of operations

| # | Step | Verified by |
|---|---|---|
| 1 | `0038` merged and applied: `loans.note_rate_pct` added, back-filled only where provable, both boarding paths dual-write, both readers prefer it | `db/tests/test_0038_loans_note_rate_expand.py`, `db/tests/test_note_rate_readers_agree.py` |
| 2 | Every unproven row resolved — see below. **This is the step that needs a human decision** | the migration's own gate 1 |
| 3 | The readers deployed **everywhere**. Merging is not sufficient; the image must be running | operator; see below |
| 4 | Run `0039` with the acknowledgement set | the migration's own gate 2 |

## Step 2 — the rows the migration refuses over

Gate 1 refuses while **any** loan has `note_rate_pct IS NULL`, and names up to
ten of them:

```
0039 refused: 3 loan(s) have no proven note_rate_pct (ids: 41, 58, 77). ...
```

For those rows `apr` is the only rate the loan carries, and the legacy schedule
reconstruction is built from it. Dropping it does not rename anything — it
removes a borrower's ability to see what they owe.

**Do not copy `apr` across to clear the gate.** For a pre-change loan that value
is the disclosed APR, and recording it as `note_rate_pct` states a contractual
term the borrower never agreed to — permanently, and in the column servicing
bills from. That is the exact conflation this work exists to end, and it would
be indistinguishable afterwards from a correct value.

Resolving them is a **servicing decision, not a migration decision**. The
options, in order of preference:

1. **The signed disclosure.** If the offer for that application records a
   `note_rate_pct`, that is the contractual rate as disclosed. (0038 already
   back-filled every row where this agreed with `apr`; a row still NULL means
   the offer had no proven note rate either, or the two disagreed — and a
   disagreement is itself worth investigating before it is resolved.)
2. **The servicing history.** What the borrower has actually been billed and has
   paid is evidence of the rate the schedule was built on.
3. **Accept that the schedule is unavailable for those loans**, and do not run
   `0039` until somebody owns that.

Whoever decides records the value in `note_rate_pct` and re-runs. There is no
supported way to make the gate pass without a decision, and that is intentional.

To find them:

```sql
SELECT l.id, l.app_id, l.apr, o.note_rate_pct AS offer_note_rate, o.apr AS offer_apr,
       l.schedule_version
  FROM loans l LEFT JOIN offers o ON o.app_id = l.app_id
 WHERE l.note_rate_pct IS NULL
 ORDER BY l.id;
```

## Step 3 — the deploy check

`db/tests/test_note_rate_readers_agree.py` enumerates the readers **from source**
rather than from a hand-maintained list, so it stays true as readers are added.
It tells you what this revision does.

It **cannot** tell you which images are serving traffic. Nothing in this
repository can. That is the human half:

- list the running service versions (image tag or build SHA);
- confirm each is at or after the revision where the `apr` reads were removed —
  `services/servicing-service/app/routers/loans.py`,
  `services/gateway/app/main.py`, `services/origination-service/app/intake.py`;
- confirm no autoscaler, canary or paused rollout can still start an older one.

An instance still running a pre-`0038` image starts erroring the moment the
`ALTER` commits, and during a rolling restart those instances are exactly what
is serving traffic.

## Step 4 — running the migration

```
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -c "SET meridian.loans_apr_drop_acknowledged = 'yes'" \
  -f db/migrations/0039_drop_loans_apr.sql
```

Without it the migration raises and changes nothing:

```
0039 refused: this migration destroys data and breaks any running instance
that still reads loans.apr. ...
```

The value must be exactly `yes`. `true`, `YES` and `y` are refused — a
half-remembered value fails closed rather than authorising a destructive
migration (`db/tests/test_0039_drop_loans_apr.py`).

The GUC is a deliberate human gate rather than an automated one. No SQL can
inspect which application images are currently serving traffic, so the
acknowledgement is a statement by the person who checked — which is why it must
be typed at the moment of running and cannot be set in a config file.

The migration also makes `note_rate_pct NOT NULL`, which gate 1 has just
established is true of the data. That turns "every loan has a known rate" from a
fact about today's rows into a property of the schema: a future boarding path
that forgets the column fails at the INSERT instead of silently creating a loan
whose contractual rate is unknown.

## What this does not do

- **`offers.apr` is untouched and stays.** That column holds a real disclosed
  APR and is correctly named. The scope here is the loan row.
- **It does not change any money.** No balance, payment or schedule is
  recomputed. Servicing amortized `note_rate_pct` before this migration and
  amortizes it afterwards; only the column that no longer exists has changed.
- **It does not prove the disclosed APR is right.** That is `apr.py` and the
  Reg Z work (D6, D16), a different concern that happens to share a word.

## Rollback

There is none for the data. `apr` is destroyed by design — that is the point of
the contract step, and `note_rate_pct` is what survives, carrying the figure the
column should have held all along.

If services break after the drop, roll **forward**: deploy the image that reads
`note_rate_pct`. Restoring the column would leave it empty, so a rolled-back
image would still fail, and would now also be reading a column that exists and
holds nothing — a worse state than the one it was rolled back from.
