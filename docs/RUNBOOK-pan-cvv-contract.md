# Runbook: dropping `payments.pan` and `payments.cvv`

This is the **contract** half of an expand-and-contract migration. It destroys
data and can break running services. It is not a migration to run because it is
next in the folder.

## Why this is dangerous, specifically

`db/migrations/0031_drop_payments_pan_cvv.sql` drops two columns. Servicing's
payment-history endpoint reads `payment.pan` as a fallback for rows written
before tokenization, so **any instance still running the pre-cutover image
starts returning errors the moment the `ALTER` commits**. During a rolling
restart, instances running the previous image are exactly what is serving
traffic.

The migration runner applies every `*.sql` in filename order. So if the expand
migration (`0029`) and this one reach the same database in the same deploy,
there is no overlap window at all — the fallback is removed in the same breath
as the data it falls back to.

Automated review raised this as high severity and recommended removing `0031`
from the branch. That part of the premise does not apply here: PR #15 is based
on PR #11's branch rather than on `main`, so `0031` is already a separate
release step. What was genuinely missing is that **nothing mechanically enforced
it** — the separation existed only in branch topology and in prose, both of
which a merge to `main` erases. Hence the gate below.

## Order of operations

| # | Step | Verified by |
|---|---|---|
| 1 | PR #11 merged: `last4` back-filled, servicing reads `last4` first and falls back to `pan` only for pre-`0029` rows | `db/tests/test_expand_contract_pan_cvv.py`, `services/servicing-service/tests/test_pan_mask.py` |
| 2 | PR #11 **deployed everywhere** — merging it is not sufficient, the image must be running. No instance may still be serving the pre-cutover build | operator; see below |
| 3 | ~~Seed writers removed~~ — **already done, no action.** `db/init/002_seed.sql` and `db/init/003_seed_bulk.sql` insert only `last4`/`brand`, and the seeded audit row reads `charge req last4=1111`. A fresh database no longer reintroduces card data | read the two seed files; `db/tests/test_expand_contract_pan_cvv.py` |
| 4 | Source check passes | `python db/tools/check_no_pan_readers.py` |
| 5 | Run `0031` with the acknowledgement set | the migration's own gate |

Step 3 is kept in the table rather than deleted so an operator working from an
older copy of this runbook can see it was retired deliberately, and why. It
described the tree as it was before the expand step landed; asserting it still
holds sent operators to wait for cleanup that had already happened, while the
card data this migration removes stayed in the database. Reviewed on PR #15.

Step 4 remains independent of it: the checker reads service source, and the
seeds are not service source — so a green checker never proved anything about
the seeds either way.

## Step 4 — the source check

```
python db/tools/check_no_pan_readers.py --verbose
```

Exit 0 means no service source **at this revision** maps or reads
`payments.pan` / `payments.cvv`. It scans ORM mappings, raw SQL and attribute
reads, and deliberately ignores docstrings, tests and migrations.

It **cannot** tell you which images are serving traffic. Nothing in this
repository can. That is step 2, and it is a human check:

- list the running service versions (image tag or build SHA);
- confirm each is at or after the revision where the `pan` reads were removed;
- confirm no autoscaler, canary or paused rollout can still start an older one.

A green checker run plus an unverified step 2 is the exact failure this whole
sequence exists to prevent.

## Step 5 — running the migration

The migration refuses to run unless two conditions hold. Both are checked
inside `0031` itself, so neither can be skipped by someone who did not read this
page:

1. **The back-fill is complete.** No `payments` row may hold a `pan` with a
   `NULL` `last4`. Otherwise the drop destroys the only record of the card used.
2. **The operator acknowledges the deploy check.** A session GUC, set
   explicitly:

```
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -c "SET meridian.pan_drop_acknowledged = 'yes'" \
  -f db/migrations/0031_drop_payments_pan_cvv.sql
```

Without it the migration raises and changes nothing:

```
0031 refused: this migration destroys data and can break servicing
instances that still read payments.pan. ...
```

The GUC is a deliberate human gate rather than an automated one. No SQL can
inspect which application images are currently serving traffic, so the
acknowledgement is a statement by the person who checked — which is why it must
be typed at the moment of running, and cannot be set in a config file.

## Rollback

There is none for the data. `pan` and `cvv` are destroyed by design — that is
the point of the contract step, and `last4` is what survives.

If services break after the drop, roll **forward**: deploy the image that reads
`last4`. Restoring the columns would leave them empty, so a rolled-back image
would still fail, and would now also be reading a column that exists but holds
nothing — a worse state than the one it was rolled back from.
