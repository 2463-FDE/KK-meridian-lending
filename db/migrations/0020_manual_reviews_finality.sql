-- 0020 -- requirement: once a staff member approves or denies an
-- application, that decision must be final -- no staff member (not even a
-- different one) may change it afterward, and the original decision,
-- reason, staff member, and timestamp must be provable on request.
--
-- manual_reviews had no identity of WHO decided beyond reviewer_role (a
-- role, not a person -- "underwriter" tells you nothing about which
-- underwriter), and nothing stopped more than one row per app_id -- the
-- prior design (review_application could resolve a refer, or later be
-- widened to override an existing approve/deny) relied entirely on
-- application-layer checks to keep it to one row, not a real constraint.
--
-- Same three-step shape as 0011/0015's cleanup-then-constrain: dedupe first
-- (some existing rows are dirty test data from that widened-override
-- design, several manual_reviews rows for the same app_id), then add the
-- real guarantee. "Original decision" means the FIRST one ever recorded --
-- keep the oldest (lowest id) row per app_id, not the newest (this is the
-- opposite of 0011/0015's "keep newest" rule, on purpose: those fixed a
-- duplicate-insert race where the newest row is the real one; this fixes a
-- since-reverted override feature where the FIRST row is the one that must
-- have been final all along).

ALTER TABLE manual_reviews
    ADD COLUMN IF NOT EXISTS reviewer_name TEXT;

-- 1. Resolve duplicates: keep the oldest row per app_id, delete the rest.
-- On a database that never had more than one manual review per
-- application (true for every deployment except this session's own
-- dev/test data), this deletes nothing.
DELETE FROM manual_reviews a
USING manual_reviews b
WHERE a.app_id = b.app_id
  AND a.id > b.id;

-- 2. The real "one final staff decision per application, ever" guarantee.
-- Gap D (PR #6 review): idempotent. db/init/005_manual_reviews.sql declares this same
-- uniqueness INLINE on manual_reviews.app_id, which Postgres auto-names
-- "manual_reviews_app_id_key" -- the exact name below. A bare ADD CONSTRAINT therefore
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
        WHERE t.relname = 'manual_reviews'
          AND n.nspname = current_schema()
          AND c.contype = 'u'
          AND c.conkey = ARRAY[
              (SELECT a.attnum FROM pg_attribute a
                WHERE a.attrelid = t.oid AND a.attname = 'app_id')
          ]::smallint[]
    ) THEN
        RAISE NOTICE '0020: manual_reviews.app_id is already UNIQUE; leaving it as-is.';
    ELSE
        ALTER TABLE manual_reviews ADD CONSTRAINT manual_reviews_app_id_key UNIQUE (app_id);
        RAISE NOTICE '0020: added manual_reviews_app_id_key.';
    END IF;
END $$;
