-- 0029 -- drop payments.pan and payments.cvv for good.
--
-- Week 1-4 client review, the "delete the log file" item: "the payments.pan /
-- payments.cvv columns are dropped rather than waiting on the week-five PR."
--
-- This could not run until PR #8 landed, because both payment-service and
-- servicing-service still INSERTed those columns on main -- dropping them
-- first would have broken every charge. That work is merged now: no code path
-- writes either column, and new rows carry `last4`/`brand` from the
-- processor's token response instead (ADR 0008, supersedes ADR 0003).
--
-- CVV storage after authorization is a flat PCI-DSS prohibition regardless of
-- encryption, and a stored PAN is the single largest driver of PCI scope. The
-- columns being unused is not the same as the data being gone: every
-- pre-tokenization row still holds a real PAN and CVV in cleartext until this
-- runs.
--
-- ORDER MATTERS. Back-fill first, drop second.
--
-- servicing-service's payment history masks a card as "**** 1234", and for a
-- legacy row it derived those four digits from `pan` (routers/loans.py::
-- _display_last4). Dropping `pan` without back-filling would silently blank
-- the card column for every historical payment. Last four digits are
-- explicitly permitted to be stored and displayed under PCI-DSS, so the
-- back-fill preserves the display while destroying the sensitive part.
--
-- Irreversible by design. There is no keeping a copy "just in case": the whole
-- point is that the full PAN and the CVV stop existing in this database.

-- Steps 1 and 2 are wrapped in a column-existence check and run through
-- EXECUTE: on a replay, or on a database built from a db/init that no longer
-- declares these columns, static SQL referencing `pan` would abort the
-- migration. db/tests/test_migration_paths_converge.py replays every migration
-- twice and would catch exactly that.
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
        RAISE NOTICE '0029: payments.pan/cvv already absent; nothing to do.';
        RETURN;
    END IF;

    -- 1. Preserve what is allowed to survive: the last four digits, for
    --    display. Without this, dropping `pan` silently blanks the card column
    --    on every historical payment in servicing's payment history.
    EXECUTE 'UPDATE payments SET last4 = right(pan, 4) '
            'WHERE last4 IS NULL AND pan IS NOT NULL AND length(pan) >= 4';

    -- 2. Report what is about to be destroyed, so it appears in the run log
    --    rather than being discovered later.
    EXECUTE 'SELECT count(*) FROM payments WHERE pan IS NOT NULL' INTO pan_rows;
    EXECUTE 'SELECT count(*) FROM payments WHERE cvv IS NOT NULL' INTO cvv_rows;
    IF pan_rows = 0 AND cvv_rows = 0 THEN
        RAISE NOTICE '0029: no stored PAN/CVV values to remove.';
    ELSE
        RAISE WARNING '0029: destroying % stored PAN value(s) and % stored CVV value(s). last4 preserved for display.', pan_rows, cvv_rows;
    END IF;
END $$;

-- 3. Drop. IF EXISTS so a replay, or a database built from a db/init that
--    already omits them, is a no-op rather than an abort.
ALTER TABLE payments DROP COLUMN IF EXISTS pan;
ALTER TABLE payments DROP COLUMN IF EXISTS cvv;
