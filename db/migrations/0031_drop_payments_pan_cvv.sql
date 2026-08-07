-- 0031 -- CONTRACT step: drop payments.pan and payments.cvv.
--
-- Second half of the expand/contract pair started in
-- db/migrations/0029_payments_backfill_last4.sql. Do not run this until the
-- release containing 0029 is fully deployed and no instance reads either
-- column: servicing-service's payment history used to mask legacy rows from
-- `pan`, and an instance still doing that will fail `/loans/{loan_id}/payments`
-- the moment these columns disappear (PR #11 review).
--
-- 0029 is what makes this safe. It back-filled `last4` for every card row, so
-- the display those readers wanted survives the drop -- last four digits are
-- explicitly permitted to be stored and displayed under PCI-DSS.
--
-- What this closes: CVV storage after authorization is a flat PCI-DSS
-- prohibition regardless of encryption, and a stored PAN is the largest single
-- driver of PCI scope. No code path has written either column since the Week 5
-- tokenization work (ADR 0008, supersedes ADR 0003), but unused is not the same
-- as gone -- every pre-tokenization row still held a real card number and
-- security code in cleartext until this ran.
--
-- Irreversible by design. There is no keeping a copy "just in case": the whole
-- point is that the full PAN and the CVV stop existing in this database.

-- Report what is about to be destroyed, so it lands in the run log rather than
-- being discovered later. Guarded, because a replay finds no columns at all.
DO $$
DECLARE
    pan_rows INTEGER;
    cvv_rows INTEGER;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'payments'
           AND column_name = 'pan'
    ) THEN
        RAISE NOTICE '0031: payments.pan/cvv already absent; nothing to do.';
        RETURN;
    END IF;

    EXECUTE 'SELECT count(*) FROM payments WHERE pan IS NOT NULL' INTO pan_rows;
    EXECUTE 'SELECT count(*) FROM payments WHERE cvv IS NOT NULL' INTO cvv_rows;
    IF pan_rows = 0 AND cvv_rows = 0 THEN
        RAISE NOTICE '0031: no stored PAN/CVV values to remove.';
    ELSE
        RAISE WARNING '0031: destroying % stored PAN value(s) and % stored CVV value(s). last4 preserved by 0029.', pan_rows, cvv_rows;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- DEPLOYMENT GATE -- this migration refuses to run unless both hold
-- ---------------------------------------------------------------------------
-- Review finding, high: the repository's migration runner applies every *.sql
-- in filename order, so an ordinary deploy of a branch containing both 0029 and
-- 0031 would back-fill last4 and then immediately drop `pan`/`cvv`. Any
-- servicing instance still running the previous image selects `payment.pan` in
-- /loans/{loan_id}/payments and starts failing the moment these ALTERs commit.
--
-- The review's suggested remedy was to remove 0031 from the branch. That part
-- of the premise does not hold here: this branch is BASED ON PR #11's branch,
-- not on main, so 0031 is already a separate release step rather than something
-- riding along with the expand migration. What was genuinely missing is that
-- nothing MECHANICALLY enforced the ordering -- the separation existed only in
-- branch topology and prose, and a merge to main would erase both.
--
-- So the ordering is enforced here, in the migration, where it cannot be
-- skipped by anyone who did not read the runbook:
--
--   1. The expand step must be complete. Every row that still holds a `pan`
--      must already have its `last4` back-filled by 0029. If the back-fill has
--      not run, dropping the column destroys the only copy of that data.
--
--   2. The operator must explicitly acknowledge that no live service version
--      still reads these columns, by setting a session GUC. This is a
--      deliberate human gate: no SQL can inspect which application images are
--      currently serving traffic, so the check that answers that question is
--      db/tools/check_no_pan_readers.py, and this is the acknowledgement that
--      it was run and passed. See docs/RUNBOOK-pan-cvv-contract.md.
--
--      psql -v ON_ERROR_STOP=1 --           -c "SET meridian.pan_drop_acknowledged = 'yes'" --           -f db/migrations/0031_drop_payments_pan_cvv.sql
--
-- A gate that is merely documented is a gate that gets skipped during the
-- incident it exists to prevent.

DO $$
DECLARE
    unbackfilled INTEGER;
    ack TEXT;
    still_present BOOLEAN;
BEGIN
    -- Already dropped? Then this is a replay and there is nothing to gate.
    -- Without this the gate itself references a column that no longer exists
    -- and the migration fails on its second run -- which the runner is entitled
    -- to do, and which the idempotency test caught.
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'payments'
           AND column_name = 'pan'
    ) INTO still_present;
    IF NOT still_present THEN
        RAISE NOTICE '0031: payments.pan is already gone -- nothing to do.';
        RETURN;
    END IF;

    EXECUTE 'SELECT count(*) FROM payments WHERE pan IS NOT NULL AND last4 IS NULL'
       INTO unbackfilled;
    IF unbackfilled > 0 THEN
        RAISE EXCEPTION
          '0031 refused: % payment row(s) still hold a pan with no last4. The '
          '0029 back-fill has not completed, so dropping these columns would '
          'destroy the only record of the card used. Run 0029 first.',
          unbackfilled;
    END IF;

    ack := coalesce(current_setting('meridian.pan_drop_acknowledged', true), '');
    IF ack <> 'yes' THEN
        RAISE EXCEPTION
          '0031 refused: this migration destroys data and can break servicing '
          'instances that still read payments.pan. Confirm no live service '
          'version reads pan/cvv (python db/tools/check_no_pan_readers.py), '
          'then re-run with: SET meridian.pan_drop_acknowledged = ''yes''; '
          'See docs/RUNBOOK-pan-cvv-contract.md.';
    END IF;

    RAISE NOTICE '0031: gate satisfied -- back-fill complete and drop acknowledged.';
END $$;

ALTER TABLE payments DROP COLUMN IF EXISTS pan;
ALTER TABLE payments DROP COLUMN IF EXISTS cvv;
