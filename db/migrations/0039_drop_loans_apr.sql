-- 0039 -- D19 contract: drop `loans.apr`, the column whose name was the defect.
--
-- 0038 added `loans.note_rate_pct` and back-filled it only where the value could
-- be proven. This removes the old name, so nobody reading SQL, a dump or
-- `db/init` meets a column called `apr` that holds a note rate.
--
-- **It destroys data and it is gated twice**, on the same pattern as 0031 (the
-- PAN/CVV drop), for the same reason: a gate that is merely documented is a gate
-- that gets skipped during the incident it exists to prevent.
--
--   psql -v ON_ERROR_STOP=1 \
--        -c "SET meridian.loans_apr_drop_acknowledged = 'yes'" \
--        -f db/migrations/0039_drop_loans_apr.sql
--
-- **Gate 1 is the one that matters.** For a loan whose rate was never proven,
-- `apr` is the ONLY rate on the row -- it is what the legacy schedule
-- reconstruction is built from. Dropping it there does not merely rename
-- something: it removes a borrower's ability to see what they owe. So this
-- migration refuses while any such row exists, and names them.
--
-- That refusal is deliberate and is not a bug to work around. If it fires,
-- somebody has to decide what those loans' rates are -- from the signed
-- disclosure, from the servicing history, or by accepting that the schedule for
-- them is unavailable. **That is a decision for whoever owns servicing, not for
-- this migration**, and inventing an answer here would be the exact
-- APR/note-rate conflation D19 exists to end.

BEGIN;

DO $$
DECLARE
    unproven INTEGER;
    examples TEXT;
    ack      TEXT;
BEGIN
    -- Gate 1: every loan must already carry a proven note rate.
    SELECT count(*) INTO unproven FROM loans WHERE note_rate_pct IS NULL;

    IF unproven > 0 THEN
        SELECT string_agg(id::text, ', ' ORDER BY id)
          INTO examples
          FROM (SELECT id FROM loans WHERE note_rate_pct IS NULL ORDER BY id LIMIT 10) t;

        RAISE EXCEPTION
          '0039 refused: % loan(s) have no proven note_rate_pct (ids: %). '
          '`apr` is the only rate those rows carry, and the legacy schedule '
          'reconstruction is built from it -- dropping it would leave those '
          'borrowers unable to see what they owe. Decide what their contractual '
          'rate is (from the signed disclosure or the servicing history), record '
          'it in note_rate_pct, and re-run. Do NOT copy `apr` across blindly: for '
          'a pre-change loan it holds the DISCLOSED APR, and recording that as a '
          'contractual term states something the borrower never agreed to.',
          unproven, examples;
    END IF;

    -- Gate 2: an operator has confirmed no deployed reader still needs the
    -- column. `db/tests/test_note_rate_readers_agree.py` enumerates the readers
    -- from source; this is the acknowledgement that the deployed images match.
    ack := coalesce(current_setting('meridian.loans_apr_drop_acknowledged', true), '');
    IF ack <> 'yes' THEN
        RAISE EXCEPTION
          '0039 refused: this migration destroys data and breaks any running '
          'instance that still reads loans.apr. Confirm every deployed image '
          'reads note_rate_pct, then re-run with '
          'SET meridian.loans_apr_drop_acknowledged = ''yes''. See '
          'docs/RUNBOOK-loans-apr-contract.md.';
    END IF;
END $$;

ALTER TABLE loans DROP COLUMN IF EXISTS apr;

-- `note_rate_pct` is now the only rate on the row, and every row has one -- gate
-- 1 established that. Making it NOT NULL states that in the schema rather than
-- leaving it as a fact about today's data: a future boarding path that forgets
-- the column now fails at the INSERT instead of silently creating a loan whose
-- rate is unknown, which is the state this whole pair of migrations exists to
-- clear up.
ALTER TABLE loans ALTER COLUMN note_rate_pct SET NOT NULL;

COMMENT ON COLUMN loans.note_rate_pct IS
    'The contractual interest rate the payment stream is priced at and servicing '
    'amortizes. Formerly `apr`, which was the wrong name: the disclosed APR is a '
    'different regulated figure that additionally carries the prepaid origination '
    'fee (D19, db/migrations/0038 and 0039).';

COMMIT;
