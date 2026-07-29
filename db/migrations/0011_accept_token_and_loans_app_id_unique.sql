-- 0011 — review fix: POST /applications/{app_id}/accept ran fully
-- anonymously for a not-yet-funded application (the legitimate no-account
-- borrower flow needs this), but app_id is a sequential, guessable integer --
-- so anyone could accept/fund a STRANGER's approved application, not just
-- their own. Fixed at the application layer (see
-- services/origination-service/app/routers/applications.py accept_offer):
-- a fresh accept now requires either a staff session or this one-time
-- accept_token, minted onto the application the moment it's approved
-- (run_decision) and cleared the moment it's spent. NULL means "no token
-- issued" (never approved, or already spent) -- never valid to accept with.
ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS accept_token TEXT;

-- Second half of the same review finding: two concurrent accept calls on the
-- same not-yet-funded application both used to pass the (stale-read)
-- status-is-not-funded check and both board a loan. accept_offer's atomic
-- `UPDATE applications SET status = 'funded' WHERE status <> 'funded'`
-- closes that race at the application-status layer; this constraint is the
-- second, database-level backstop -- one canonical loan per application, no
-- matter what code path ever inserts into loans. NULL stays legal (a
-- Postgres UNIQUE constraint allows any number of NULLs) for the legacy
-- direct-insert /board endpoint's loans that predate an app_id link.
ALTER TABLE loans
    ADD CONSTRAINT loans_app_id_key UNIQUE (app_id);
