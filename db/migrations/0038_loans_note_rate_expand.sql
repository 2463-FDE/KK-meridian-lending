-- 0038 -- D19 expand: give the note rate a column that says what it holds.
--
-- `loans.apr` has held two different things, and only one of them is an APR:
--
--   * boarded by the CURRENT path -> the contractual NOTE RATE (7.99%)
--   * boarded by the PRE-CHANGE path -> the DISCLOSED APR (5.196% for the same
--     contract, because the disclosed figure carries the prepaid fee)
--
-- Servicing amortizes that column, so billing the disclosed APR would charge the
-- borrower above their own disclosure. The money is already right -- the API
-- serializes `note_rate_pct` and the UI labels it "Interest rate" -- but the
-- column is still called `apr`, so anyone reading SQL, a dump or `db/init` meets
-- the wrong name (D19).
--
-- **This is the EXPAND half. `loans.apr` is not dropped here**, and nothing
-- outside this file changes meaning: the new column is added, back-filled only
-- where the value can be PROVEN, and both are written by the boarding path.
-- Dropping the old one is the contract step, on its own PR, after every deployed
-- reader is proven to use the new name.
--
-- **The back-fill refuses to guess.** A row whose rate cannot be shown to be a
-- note rate is left NULL, because a NULL a reader must handle is safer than a
-- number that is quietly the wrong regulated figure. That is the same decision
-- migration 0030 made for `offers.note_rate_pct`, and for the same reason: an
-- indiscriminate copy would record 5.196% as the contractual fact the UI
-- displays, which is precisely the conflation this work exists to end.

BEGIN;

ALTER TABLE loans ADD COLUMN IF NOT EXISTS note_rate_pct NUMERIC(7,3);

COMMENT ON COLUMN loans.note_rate_pct IS
    'The contractual interest rate the payment stream is priced at and servicing '
    'amortizes. NULL where it could not be proven for a legacy row -- unknown '
    'stays unknown rather than being guessed from `apr`, which held the '
    'DISCLOSED APR under the pre-change boarding path.';

-- --- the back-fill, by evidence, strongest first ------------------------------
--
-- 1. THE OFFER SAYS SO. `offers.note_rate_pct` is the contractual rate as
--    disclosed, and migration 0030 populated it only where IT could be proven --
--    so a value there is already evidence, not a copy. Where the loan's own
--    `apr` agrees with it, the loan was boarded by a path that copied the note
--    rate, and both agree on what that rate was.
UPDATE loans l
   SET note_rate_pct = o.note_rate_pct
  FROM offers o
 WHERE o.app_id = l.app_id
   AND l.note_rate_pct IS NULL
   AND o.note_rate_pct IS NOT NULL
   AND round(o.note_rate_pct, 3) = round(l.apr, 3);

-- 2. THE SCHEDULE SAYS SO. `schedule_version` is set only by the current
--    boarding path, which writes the contractual rate into `apr`. Its presence
--    is the structural evidence that the value means what the API calls it --
--    the same rule `servicing-service/app/routers/loans.py::_proven_note_rate`
--    and the gateway's borrower query already apply at read time. This moves
--    that inference into the data, once, instead of re-deriving it per request.
UPDATE loans
   SET note_rate_pct = apr
 WHERE note_rate_pct IS NULL
   AND schedule_version IS NOT NULL
   AND apr IS NOT NULL;

-- 3. EVERYTHING ELSE STAYS NULL. Deliberately: a legacy row with no schedule and
--    no agreeing offer may hold either figure, and there is no way to tell from
--    the row. Reporting a number for it would be inventing a contractual term
--    the borrower was never quoted.

DO $$
DECLARE
    proven   INTEGER;
    unproven INTEGER;
BEGIN
    SELECT count(*) FILTER (WHERE note_rate_pct IS NOT NULL),
           count(*) FILTER (WHERE note_rate_pct IS NULL)
      INTO proven, unproven
      FROM loans;

    RAISE NOTICE '0038: note_rate_pct proven for % loan(s); % left NULL because '
                 'nothing in the row shows whether `apr` held a note rate or a '
                 'disclosed APR. Those report "not recorded" rather than a '
                 'number, exactly as they did before this migration.',
                 proven, unproven;

    -- A back-fill that proved nothing is a back-fill that silently did not run.
    -- Only assert on a database that HAS loans: a fresh install legitimately has
    -- none, and failing there would make an empty database unbootable.
    IF (SELECT count(*) FROM loans WHERE schedule_version IS NOT NULL) > 0
       AND proven = 0 THEN
        RAISE EXCEPTION '0038: loans exist with a schedule_version but none were '
                        'back-filled -- the evidence rules matched nothing, so '
                        'this migration would leave every rate unknown';
    END IF;
END $$;

-- The two figures are different regulated concepts and must never be conflated
-- again. Where both are known for a loan whose offer discloses an APR, the note
-- rate is the lower of the two once a prepaid fee exists -- but that is a
-- property of the offer, not an invariant of this row, so it is asserted in
-- tests rather than as a CHECK that would refuse a zero-fee loan where they are
-- legitimately equal.

COMMIT;
