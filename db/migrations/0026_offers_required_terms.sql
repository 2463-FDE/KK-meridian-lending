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
-- REMEDIATION (corrected -- the first version of this note named a procedure
-- that did not work). Regenerating the offer is right, but `POST /offers` used
-- to be a no-op on an existing row: ON CONFLICT (decision_id) DO NOTHING, then
-- read back the row already there -- the same incomplete row. The endpoint now
-- repairs an incomplete row in place, and only that:
--
--   * UNACCEPTED (offers.accepted_at IS NULL) and still missing a term:
--       POST /offers {"application_id": <id>}   (X-Internal-Token required)
--     recomputes the five terms from the application's own principal/term,
--     stamps the CURRENT fee rule into fee_pct_used, and writes an
--     `offer.incomplete_terms_repaired` row to audit_logs in the same
--     statement. A complete offer is never rewritten by this call.
--
--   * ACCEPTED (offers.accepted_at IS NOT NULL): refused with a 409, by design.
--     The borrower is already bound to that offer, so replacing its terms is
--     worse than leaving it broken. These rows need a human decision (rescind
--     and re-disclose, or honour the loan as boarded) -- there is no automated
--     answer, and this migration will not invent one.
--
-- Do not back-fill invented terms by hand. Once no offending rows remain,
-- re-running this migration promotes the constraint to fully validated by
-- itself (see the final block); no manual ALTER is needed.

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
        RAISE WARNING '0026: repair UNACCEPTED rows with POST /offers for their application_id (audited); ACCEPTED rows are refused and need a human decision. Do not back-fill invented terms.';
    END IF;
END $$;

-- NOT VALID: enforce on new/updated rows without failing the migration on
-- pre-existing damage the operator has just been told about above.
--
-- Added only if absent, matched on THIS schema's offers table. Review finding:
-- the guard used to match on conname alone -- pg_constraint.conname is unique
-- only per table, so any constraint of that name anywhere in the database made
-- this migration skip silently, leaving offers unprotected. The other guarded
-- migrations (0009/0011/0015/0020) already filter by current_schema(); this one
-- now does too.
--
-- A fresh volume (db/init/001_schema.sql) already
-- declares this constraint inline and fully VALIDATED; dropping and re-adding
-- it here would silently downgrade that database to NOT VALID, so fresh-init
-- and fresh-init-then-replay would no longer agree. Idempotent either way.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'offers'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'offers_canonical_terms_present'
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

-- Promote to fully validated as soon as it is honest to do so. Review finding
-- (extended parity tests): leaving the constraint NOT VALID unconditionally
-- meant a clean upgraded database ended up strictly weaker than a fresh one
-- while looking identical at the column level -- and it stayed weaker forever,
-- because the manual VALIDATE step below was documentation an operator had to
-- notice and run. If no row violates it, validate it here; if some do, leave it
-- NOT VALID and say so. Re-running this migration after the operator has
-- remediated those rows (via POST /offers, above) then validates it with no
-- further action.
DO $$
DECLARE
    bad_count INTEGER;
    already_valid BOOLEAN;
BEGIN
    SELECT count(*) INTO bad_count FROM offers
    WHERE apr IS NULL OR finance_charge IS NULL OR monthly_payment IS NULL
       OR amount_financed IS NULL OR total_of_payments IS NULL;

    SELECT c.convalidated INTO already_valid
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE t.relname = 'offers'
      AND n.nspname = current_schema()
      AND c.conname = 'offers_canonical_terms_present';

    IF already_valid THEN
        RAISE NOTICE '0026: offers_canonical_terms_present is already validated.';
    ELSIF bad_count = 0 THEN
        ALTER TABLE offers VALIDATE CONSTRAINT offers_canonical_terms_present;
        RAISE NOTICE '0026: no offending rows -- offers_canonical_terms_present validated.';
    ELSE
        RAISE WARNING '0026: leaving offers_canonical_terms_present NOT VALID -- % row(s) still incomplete. Repair them, then re-run this migration.', bad_count;
    END IF;
END $$;
