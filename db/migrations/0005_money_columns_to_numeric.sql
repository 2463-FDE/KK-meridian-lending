-- 0005 — D12 fix: convert every money column from DOUBLE PRECISION to NUMERIC.
-- Hand-tracked, as usual. Authoritative DDL lives in db/init/001_schema.sql and
-- db/init/004_decision_events.sql.
--
-- Needed on any existing database whose Postgres volume already existed before this
-- change -- db/init/*.sql only runs automatically on a FRESH volume's first boot, so
-- a persistent-volume deployment created before this fix never picks it up on its
-- own (same reasoning as db/migrations/0004_add_decision_events.sql).
--
-- Safe, non-destructive: every existing DOUBLE PRECISION value converts cleanly to
-- NUMERIC (no precision is lost going float -> exact; the reverse would lose
-- precision, this direction doesn't). No data migration/backfill needed beyond the
-- type change itself.
--
-- Dollar-amount columns -> NUMERIC(14,2). Percentage/rate columns (apr) ->
-- NUMERIC(7,3), matching the app's own round(apr, 3) convention. model_score on
-- decision_events is a scoring value, not money -- left as DOUBLE PRECISION.

ALTER TABLE applications
    ALTER COLUMN amount TYPE NUMERIC(14,2),
    ALTER COLUMN income TYPE NUMERIC(14,2);

ALTER TABLE offers
    ALTER COLUMN apr TYPE NUMERIC(7,3),
    ALTER COLUMN finance_charge TYPE NUMERIC(14,2),
    ALTER COLUMN monthly_payment TYPE NUMERIC(14,2),
    ALTER COLUMN amount_financed TYPE NUMERIC(14,2),
    ALTER COLUMN total_of_payments TYPE NUMERIC(14,2);

ALTER TABLE loans
    ALTER COLUMN principal TYPE NUMERIC(14,2);

-- `loans.apr` is altered separately and guarded: 0039 drops it, so a replay of
-- the chain -- or a fresh `db/init` database run through the migration runner,
-- which `db/tests/test_migration_paths_converge.py` exercises as path 3 --
-- meets a column that is legitimately absent. Same treatment 0029 needed once
-- 0031 dropped `payments.pan`.
--
-- The column keeps its old name here on purpose. This file records a type
-- change applied to `apr` in 2026; `note_rate_pct` did not exist yet, and
-- saying otherwise would date the rename to the wrong migration. Where the
-- column still exists, its type must still be widened -- so this is a guard,
-- not a skip.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'loans'
           AND column_name = 'apr'
    ) THEN
        EXECUTE 'ALTER TABLE loans ALTER COLUMN apr TYPE NUMERIC(7,3)';
    ELSE
        RAISE NOTICE '0005: loans.apr already removed (0039 has run); the rate column is note_rate_pct and db/init creates it as NUMERIC(7,3).';
    END IF;
END $$;

ALTER TABLE balances
    ALTER COLUMN balance TYPE NUMERIC(14,2),
    ALTER COLUMN past_due TYPE NUMERIC(14,2);

ALTER TABLE payments
    ALTER COLUMN amount TYPE NUMERIC(14,2);

ALTER TABLE decision_events
    ALTER COLUMN requested_amount TYPE NUMERIC(14,2),
    ALTER COLUMN annual_income TYPE NUMERIC(14,2);
