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

ALTER TABLE payments DROP COLUMN IF EXISTS pan;
ALTER TABLE payments DROP COLUMN IF EXISTS cvv;
