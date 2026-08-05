-- 0027 -- close a fresh-install vs. upgrade divergence found by the extended
-- migration-parity tests (PR #6 review).
--
-- db/init/001_schema.sql creates five indexes that NO migration ever created:
--
--     idx_applications_status     ON applications(status)
--     idx_applications_applicant  ON applications(applicant_id)
--     idx_offers_app              ON offers(app_id)
--     idx_loans_status            ON loans(status)
--     idx_payments_loan           ON payments(loan_id)
--
-- So a new operator (fresh volume) had them and an existing operator (legacy
-- schema + migrations) did not. Both are on real query paths -- origination's
-- staff application list filters and orders on `status`, and the
-- applicant->application traversal in kg.py joins on `applicant_id` -- so the
-- upgraded database was quietly doing sequential scans the fresh one was not.
--
-- This is a divergence only a shape comparison catches: every column, UNIQUE
-- constraint and CHECK already matched. The parity suite now compares indexes,
-- foreign keys, defaults and CHECK validation state as well, on the
-- legacy-upgrade path specifically (db/tests/test_migration_paths_converge.py).
--
-- IF NOT EXISTS: a fresh volume already has both, and a replay must be a no-op.

CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_applicant ON applications(applicant_id);
CREATE INDEX IF NOT EXISTS idx_offers_app ON offers(app_id);
CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status);
CREATE INDEX IF NOT EXISTS idx_payments_loan ON payments(loan_id);
