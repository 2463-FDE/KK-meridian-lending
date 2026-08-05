-- 0030 -- separate the contractual note rate from the disclosed APR.
--
-- PR #10 review, high. Making `compute_apr` return the true actuarial APR
-- changed what `offers.apr` MEANS without changing who reads it. Origination's
-- accept path takes `offers.apr` as `rate` and hands it to
-- `board_to_servicing_tx`, which writes it to `loans.apr`; servicing then
-- amortizes the loan at that value
-- (`servicing-service/app/routers/loans.py::loan_schedule`).
--
-- Before the APR fix that was already wrong -- it amortized at the old add-on
-- ratio, 5.196%, and produced payments BELOW the disclosed 439.35. The APR fix
-- flipped the error to 9.584%, which produces 452.94, i.e. a borrower billed
-- MORE per month than the disclosure they signed. Same defect, worse direction:
-- 13.59 a month, 652 over a 48-month term.
--
-- The two numbers are not interchangeable and never were:
--
--   note rate  -- what the payment schedule is calculated on. 7.99%.
--   APR        -- the all-in cost solved against the amount financed, which is
--                 higher than the note rate whenever a prepaid fee exists.
--
-- So `offers.apr` keeps its correct new meaning (the disclosed APR, which is
-- what Reg Z requires on the disclosure) and the contractual rate gets its own
-- column, which is what boarding reads.

ALTER TABLE offers
    ADD COLUMN IF NOT EXISTS note_rate_pct NUMERIC(7,3);

-- Back-fill. Every offer this system has ever written was built at a single
-- hard-coded 7.99% -- it was a literal in disclosure-service's create_offer and
-- there has never been a second value, so this is recovering a known constant
-- rather than guessing a rate per row. (It is now
-- `disclosure-service/app/fees.py::NOTE_RATE_PCT`, for the same
-- one-source-of-truth reason the origination fee was consolidated after D6.)
--
-- Deliberately NOT derived from each row's stored payment: solving the rate per
-- row would be more impressive and less honest, since it would silently invent
-- plausible per-row rates for any row whose payment was itself written wrong.
UPDATE offers SET note_rate_pct = 7.99 WHERE note_rate_pct IS NULL;

DO $$
DECLARE
    unset_rows INTEGER;
BEGIN
    SELECT count(*) INTO unset_rows FROM offers WHERE note_rate_pct IS NULL;
    IF unset_rows = 0 THEN
        RAISE NOTICE '0030: every offer now records the note rate it was written at.';
    ELSE
        RAISE WARNING '0030: % offer(s) still have no note_rate_pct -- accept will refuse to board them.', unset_rows;
    END IF;
END $$;
