-- 0026 -- data-integrity fix (PR #6 review, Gap F): the five canonical TILA
-- amounts on `offers` were all nullable, and every read path papered over a
-- NULL with a default -- `offer.apr or 7.99`, `offer.finance_charge or 0`.
-- A corrupt or half-written offer row was therefore rendered as a real,
-- plausible-looking disclosure with invented terms, and could be accepted and
-- boarded. Application code now refuses incomplete rows outright; this
-- migration makes the database enforce the same rule for new rows.
--
-- Deliberately NOT a blanket `SET NOT NULL`. That would abort the whole
-- migration on the first pre-existing incomplete row and tell the operator
-- nothing about what is wrong. Instead:
--
--   1. DIAGNOSE  -- raise a NOTICE naming every offending offer id and the
--                   columns it is missing, so the operator can see the actual
--                   damage before anything is enforced.
--   2. ENFORCE   -- add a CHECK constraint NOT VALID. New and updated rows are
--                   checked immediately; existing rows are left alone rather
--                   than silently deleted or back-filled with invented numbers
--                   (there is no honest value to back-fill an APR with).
--
-- Remediating historical rows is a deliberate operator decision, not something
-- a migration should do unattended: the safe remediation is to regenerate the
-- offer from its decision (POST /offer is idempotent per decision), not to
-- guess terms. Once no offending rows remain, run:
--     ALTER TABLE offers VALIDATE CONSTRAINT offers_canonical_terms_present;
-- to promote the constraint to fully validated.

DO $$
DECLARE
    bad_count INTEGER;
    r RECORD;
BEGIN
    SELECT count(*) INTO bad_count FROM offers
    WHERE apr IS NULL OR finance_charge IS NULL OR monthly_payment IS NULL
       OR amount_financed IS NULL OR total_of_payments IS NULL;

    IF bad_count = 0 THEN
        RAISE NOTICE '0026: all offers rows carry the five canonical terms.';
    ELSE
        RAISE WARNING '0026: % offers row(s) are INCOMPLETE and will be rejected by the read/accept paths.', bad_count;
        FOR r IN
            SELECT id, app_id,
                   concat_ws(',',
                       CASE WHEN apr               IS NULL THEN 'apr' END,
                       CASE WHEN finance_charge    IS NULL THEN 'finance_charge' END,
                       CASE WHEN monthly_payment   IS NULL THEN 'monthly_payment' END,
                       CASE WHEN amount_financed   IS NULL THEN 'amount_financed' END,
                       CASE WHEN total_of_payments IS NULL THEN 'total_of_payments' END
                   ) AS missing
            FROM offers
            WHERE apr IS NULL OR finance_charge IS NULL OR monthly_payment IS NULL
               OR amount_financed IS NULL OR total_of_payments IS NULL
            ORDER BY id
        LOOP
            RAISE WARNING '0026:   offer id=% app_id=% missing=%', r.id, r.app_id, r.missing;
        END LOOP;
        RAISE WARNING '0026: regenerate these from their decision (POST /offer is idempotent per decision); do not back-fill invented terms.';
    END IF;
END $$;

-- NOT VALID: enforce on new/updated rows without failing the migration on
-- pre-existing damage the operator has just been told about above.
--
-- Added only if absent. A fresh volume (db/init/001_schema.sql) already
-- declares this constraint inline and fully VALIDATED; dropping and re-adding
-- it here would silently downgrade that database to NOT VALID, so fresh-init
-- and fresh-init-then-replay would no longer agree. Idempotent either way.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'offers_canonical_terms_present'
    ) THEN
        ALTER TABLE offers
            ADD CONSTRAINT offers_canonical_terms_present
            CHECK (
                apr IS NOT NULL
                AND finance_charge IS NOT NULL
                AND monthly_payment IS NOT NULL
                AND amount_financed IS NOT NULL
                AND total_of_payments IS NOT NULL
            ) NOT VALID;
        RAISE NOTICE '0026: added offers_canonical_terms_present (NOT VALID).';
    ELSE
        RAISE NOTICE '0026: offers_canonical_terms_present already present; left as-is.';
    END IF;
END $$;
