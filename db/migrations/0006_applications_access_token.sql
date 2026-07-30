-- 0006 — review fix: POST /applications/{app_id}/decision ran fully
-- anonymously for the FIRST decision on an application (the rerun guard only
-- checked once a decisions row already existed) -- app_id is a sequential,
-- guessable integer, so an unauthenticated stranger could pull a real
-- applicant's credit report using their stored SSN just by guessing an id.
--
-- access_token is minted once, at submission (intake.create_application),
-- and returned to the caller in POST /applications' own response -- the
-- borrower's browser holds it for the rest of the session (no account exists
-- yet at this point in the flow). The first decision call must present it
-- (or a staff session); see routers/applications.py run_decision.

ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS access_token TEXT;
