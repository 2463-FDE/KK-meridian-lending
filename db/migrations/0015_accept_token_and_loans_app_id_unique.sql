-- 0015 — review fix: POST /applications/{app_id}/accept ran fully
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
--
-- Review fix (ordering): this used to add the constraint with no duplicate
-- cleanup at all -- on any environment that actually hit the race this
-- branch documents (two loans boarded for one application), the ALTER TABLE
-- itself would fail on exactly the rows it exists to guard against, blocking
-- deploy. Same three-step shape as 0011's offers cleanup: resolve duplicates
-- deterministically first, then constrain -- except a loan (unlike an offer)
-- has child rows (balances, payments) that reference it directly, so the
-- losing duplicate's children must be reassigned/dropped before the loan
-- row itself can be deleted.

-- 1. Pick one canonical loan per app_id. A payment already applied against a
-- loan is real, external evidence of which one the app/borrower actually
-- used -- that loan wins regardless of id. If neither duplicate has a
-- payment (the common case: the race's loser was never returned to any
-- caller and nothing ever touched it again), fall back to the oldest id --
-- the first one boarded, and so the one most likely to be the same loan_id
-- any earlier response/log/support ticket already refers to.
CREATE TEMP TABLE loans_survivor AS
SELECT DISTINCT ON (l.app_id) l.app_id, l.id AS survivor_id
FROM loans l
WHERE l.app_id IS NOT NULL
ORDER BY l.app_id,
         (SELECT count(*) FROM payments p WHERE p.loan_id = l.id) DESC,
         l.id ASC;

-- 2. Reassign any payments recorded against a losing duplicate onto the
-- survivor -- a payment is a money-movement record and must never just
-- disappear, even for a loan_id nothing else will ever reference again.
UPDATE payments p
SET loan_id = s.survivor_id
FROM loans l
JOIN loans_survivor s ON s.app_id = l.app_id
WHERE p.loan_id = l.id
  AND l.id <> s.survivor_id;

-- 3. Drop the losing duplicates' own balances rows -- board_to_servicing
-- creates one balances row per loan at boarding time, so every duplicate has
-- one. The survivor's own balances row (same principal, since both loans in
-- a duplicate pair were boarded from the SAME application/amount) already
-- reflects the correct balance; the loser's is now genuinely orphaned.
DELETE FROM balances b
USING loans l
JOIN loans_survivor s ON s.app_id = l.app_id
WHERE b.loan_id = l.id
  AND l.id <> s.survivor_id;

-- 4. Delete the losing duplicate loan rows themselves -- safe now that
-- nothing still references them (step 2 moved any payments, step 3 dropped
-- the balances row).
DELETE FROM loans l
USING loans_survivor s
WHERE l.app_id = s.app_id
  AND l.id <> s.survivor_id;

DROP TABLE loans_survivor;

-- 5. The real "one loan per application" guarantee -- safe to add now that
-- step 1-4 leaves at most one non-NULL app_id per loan.
-- Gap D (PR #6 review): idempotent. db/init/001_schema.sql declares this same
-- uniqueness INLINE on loans.app_id, which Postgres auto-names
-- "loans_app_id_key" -- the exact name below. A bare ADD CONSTRAINT therefore
-- aborted on any database built from db/init and then run through the
-- migrations, which is why CI could not replay them. The guard checks for a
-- UNIQUE constraint on the COLUMN rather than trusting that auto-generated
-- name, so it holds even if the name ever differs. Existing rows and history
-- are untouched either way.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'loans'
          AND n.nspname = current_schema()
          AND c.contype = 'u'
          AND c.conkey = ARRAY[
              (SELECT a.attnum FROM pg_attribute a
                WHERE a.attrelid = t.oid AND a.attname = 'app_id')
          ]::smallint[]
    ) THEN
        RAISE NOTICE '0015: loans.app_id is already UNIQUE; leaving it as-is.';
    ELSE
        ALTER TABLE loans ADD CONSTRAINT loans_app_id_key UNIQUE (app_id);
        RAISE NOTICE '0015: added loans_app_id_key.';
    END IF;
END $$;
