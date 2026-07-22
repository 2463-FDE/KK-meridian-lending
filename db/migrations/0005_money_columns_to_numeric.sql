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
    ALTER COLUMN principal TYPE NUMERIC(14,2),
    ALTER COLUMN apr TYPE NUMERIC(7,3);

ALTER TABLE balances
    ALTER COLUMN balance TYPE NUMERIC(14,2),
    ALTER COLUMN past_due TYPE NUMERIC(14,2);

ALTER TABLE payments
    ALTER COLUMN amount TYPE NUMERIC(14,2);

ALTER TABLE decision_events
    ALTER COLUMN requested_amount TYPE NUMERIC(14,2),
    ALTER COLUMN annual_income TYPE NUMERIC(14,2);
