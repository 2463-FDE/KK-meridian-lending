-- 0009 — Week 4 review fix: make offer creation idempotent per decision.
-- Hand-tracked, as usual. Authoritative DDL lives in db/init/001_schema.sql.
--
-- Without this, a retried/duplicated create_offer call (timeout retry, double
-- click) inserted a second offer row for the same decision, and every read path
-- (ORDER BY id DESC) silently treated the newest one as authoritative -- with no
-- record of why two offers existed for one decision. NULL is left legal (a
-- Postgres UNIQUE constraint allows any number of NULLs) so legacy offers that
-- predate the decision_id column aren't touched by this migration.

-- Gap D (PR #6 review): idempotent. db/init/001_schema.sql declares this same
-- uniqueness INLINE on offers.decision_id, which Postgres auto-names
-- "offers_decision_id_key" -- the exact name below. A bare ADD CONSTRAINT therefore
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
        WHERE t.relname = 'offers'
          AND n.nspname = current_schema()
          AND c.contype = 'u'
          AND c.conkey = ARRAY[
              (SELECT a.attnum FROM pg_attribute a
                WHERE a.attrelid = t.oid AND a.attname = 'decision_id')
          ]::smallint[]
    ) THEN
        RAISE NOTICE '0009: offers.decision_id is already UNIQUE; leaving it as-is.';
    ELSE
        ALTER TABLE offers ADD CONSTRAINT offers_decision_id_key UNIQUE (decision_id);
        RAISE NOTICE '0009: added offers_decision_id_key.';
    END IF;
END $$;
