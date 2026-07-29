-- 0010 — review fix: the 0007 unique constraint on offers.decision_id never
-- fired for legacy rows, since decision_id was NULL for every offer created
-- before the W4 decision-link feature existed, and Postgres lets any number
-- of NULLs coexist under a UNIQUE constraint. A repeat POST /offers for one
-- of those applications could still create a second canonical offer, and
-- read paths (ORDER BY id DESC) would silently start serving the newer one.
--
-- Three steps: resolve any existing duplicates explicitly first (there
-- shouldn't be many, if any -- this schema only recently started allowing
-- them), then backfill decision_id + fee_pct_used on the surviving legacy
-- rows, then constrain on app_id itself, which -- unlike decision_id -- has
-- been populated on every offer row since this table's very first migration.
--
-- Review fix (ordering): this used to backfill decision_id BEFORE resolving
-- duplicates. Backfilling sets decision_id = app_id, so two duplicate rows
-- for the same app_id got the SAME decision_id -- which immediately violated
-- 0007's offers_decision_id_key UNIQUE constraint and aborted the whole
-- migration, on exactly the data this migration exists to repair. Resolving
-- duplicates first leaves at most one row per app_id, so the backfill below
-- can never collide.

-- 1. Resolve duplicates explicitly: keep the newest row per app_id (matches
-- every existing read path's own ORDER BY id DESC convention for "which
-- offer is authoritative"), delete the rest. On a database that never had a
-- duplicate, this deletes nothing.
DELETE FROM offers a
USING offers b
WHERE a.app_id = b.app_id
  AND a.id < b.id;

-- 2. Backfill decision_id: legal only where a matching decision actually
-- exists (decision_id has an FK to decisions.app_id) -- an offer somehow
-- created without ANY decision on record is left alone; step 4's constraint
-- doesn't depend on decision_id, so that's not a blocker. Safe against
-- offers_decision_id_key now -- step 1 guarantees at most one row per app_id,
-- so decision_id = app_id can't collide across rows.
UPDATE offers o
SET decision_id = o.app_id
WHERE o.decision_id IS NULL
  AND o.app_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM decisions d WHERE d.app_id = o.app_id);

-- 3. Backfill fee_pct_used: the column didn't exist before W4, so there is no
-- true historical value to recover for these rows. Best-effort, not a
-- guarantee -- assumes the fee rule was always the current 3.0% default
-- (fees.py's ORIGINATION_FEE_PCT) before the per-offer snapshot began, which
-- is true unless that constant itself changed prior to this migration. Flag
-- for manual review if that's ever not the case.
UPDATE offers
SET fee_pct_used = 0.030
WHERE fee_pct_used IS NULL;

-- 4. The real "one canonical offer per application" guarantee -- app_id has
-- been populated on every offer row since this table's first migration,
-- unlike decision_id, so this constraint (unlike 0007's) has no NULL gap to
-- exploit.
ALTER TABLE offers
    ADD CONSTRAINT offers_app_id_key UNIQUE (app_id);
