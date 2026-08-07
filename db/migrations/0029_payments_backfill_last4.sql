-- 0029 -- EXPAND step: give every payment a `last4` so nothing needs `pan`.
--
-- First half of an expand/contract pair. It deliberately does NOT drop
-- `payments.pan` / `payments.cvv`; `db/migrations/0031` does that, in a later
-- release.
--
-- Why split (PR #11 review). Dropping the columns in the same release as the
-- code that stops reading them is only safe if every instance restarts at the
-- same instant. It does not: servicing-service's payment history reads
-- `payment.pan` to mask legacy rows, so a migration that lands before the new
-- code -- or one old instance still serving traffic during a rolling restart --
-- issues a SELECT for a column that is already gone and fails
-- `/loans/{loan_id}/payments`.
--
-- Honest scope note: this repository is a local training build with no
-- production deployment and no rolling restart, so the concrete blast radius
-- today is a developer's `docker compose up`. The ordering is still worth
-- getting right, because a destructive migration is exactly the change where
-- "it happened to work here" is not a reason.
--
-- After this migration the database is compatible with BOTH the old code (the
-- columns are still present) and the new code (which needs only `last4`). That
-- overlap is the point: it is the window in which instances may restart in any
-- order.
--
-- Last four digits are explicitly permitted to be stored and displayed under
-- PCI-DSS. The full PAN and the CVV are what must go, and 0031 removes them.

-- Guarded and run through EXECUTE: once 0031 has removed `pan`, a replay of
-- this file would otherwise abort on a column that no longer exists, and
-- db/tests/test_migration_paths_converge.py replays the whole chain twice.
DO $$
DECLARE
    still_blank INTEGER;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'payments'
           AND column_name = 'pan'
    ) THEN
        RAISE NOTICE '0029: pan already removed (0031 has run); nothing to back-fill.';
        RETURN;
    END IF;

    EXECUTE 'UPDATE payments SET last4 = right(pan, 4) '
            'WHERE last4 IS NULL AND pan IS NOT NULL AND length(pan) >= 4';

    SELECT count(*) INTO still_blank
      FROM payments
     WHERE last4 IS NULL AND method = 'card';
    IF still_blank = 0 THEN
        RAISE NOTICE '0029: every card payment can be displayed without pan. Safe to run 0031.';
    ELSE
        -- Not fatal: a card row with neither last4 nor a usable pan has nothing
        -- to display and never did. Surfaced so the operator sees it before the
        -- contract step makes it permanent.
        RAISE WARNING '0029: % card payment(s) have no last4 and no recoverable pan -- they will display no card after 0031.', still_blank;
    END IF;
END $$;
