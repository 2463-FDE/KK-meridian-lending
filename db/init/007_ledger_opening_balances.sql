-- 007 -- ADR 0010: open the ledger for every seeded loan.
--
-- Runs after 002/003 because it reads `balances`, which they populate. The
-- projection is suppressed for the same reason db/migrations/0035 suppresses it:
-- `balances` already holds these amounts, so applying them would double every
-- seeded loan.
--
-- Identical logic to that migration, deliberately -- a fresh volume and a
-- migrated database must produce the same ledger, and
-- db/tests/test_migration_paths_converge.py compares them.

DO $$
DECLARE
    seeded   INTEGER;
    skipped  INTEGER;
    zero_bal INTEGER;
BEGIN
    LOCK TABLE balances IN SHARE ROW EXCLUSIVE MODE;

    INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason)
    SELECT b.loan_id, 'principal', b.balance, 'opening_balance',
           'balance as it stood when the ledger began (db/migrations/0035)'
      FROM balances b
     WHERE b.balance <> 0
       AND NOT EXISTS (SELECT 1 FROM ledger_entries le
                        WHERE le.loan_id = b.loan_id
                          AND le.component = 'principal');
    GET DIAGNOSTICS seeded = ROW_COUNT;

    INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason)
    SELECT b.loan_id, 'fees', b.past_due, 'opening_balance',
           'past_due as it stood when the ledger began (db/migrations/0035)'
      FROM balances b
     WHERE COALESCE(b.past_due, 0) <> 0
       AND NOT EXISTS (SELECT 1 FROM ledger_entries le
                        WHERE le.loan_id = b.loan_id
                          AND le.component = 'fees');

    SELECT count(*) INTO skipped  FROM balances b
     WHERE EXISTS (SELECT 1 FROM ledger_entries le
                    WHERE le.loan_id = b.loan_id AND le.component = 'principal');
    SELECT count(*) INTO zero_bal FROM balances WHERE balance = 0;

    IF EXISTS (
        SELECT 1
          FROM balances b
          LEFT JOIN ledger_entries le ON le.loan_id = b.loan_id
         GROUP BY b.loan_id, b.balance, b.past_due
        HAVING b.balance <> COALESCE(SUM(le.amount) FILTER (WHERE le.component = 'principal'), 0)
            OR COALESCE(b.past_due, 0) <> COALESCE(SUM(le.amount) FILTER (WHERE le.component = 'fees'), 0)
    ) THEN
        RAISE EXCEPTION '007 ledger opening-balance parity validation failed';
    END IF;

    -- A migration that silently seeds nothing looks identical to one that seeded
    -- everything, so both counts are reported.
    RAISE NOTICE '0035: opened % principal entry(ies); % loan(s) now have an '
                 'opening balance; % loan(s) at zero were skipped (an entry of '
                 'amount 0 is not a movement and the CHECK forbids it)',
                 seeded, skipped, zero_bal;
END $$;

UPDATE ledger_control SET initialization_open = FALSE
 WHERE singleton AND initialization_open;

-- Attach the compatibility bridge only after seeded balances have canonical
-- opening_balance entries. New loans boarded after initialization are captured
-- as legacy_direct_write until application writers move to the ledger.
CREATE OR REPLACE FUNCTION capture_legacy_balance_delta() RETURNS trigger AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.balance <> 0 THEN
        INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason)
        VALUES (NEW.loan_id, 'principal', NEW.balance, 'legacy_direct_write',
                'captured from a balances insert during ledger cutover');
    ELSIF TG_OP = 'UPDATE' AND NEW.balance IS DISTINCT FROM OLD.balance THEN
        INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason)
        VALUES (NEW.loan_id, 'principal', NEW.balance - OLD.balance,
                'legacy_direct_write',
                'captured from a direct balances update during ledger cutover');
    END IF;
    IF TG_OP = 'INSERT' AND COALESCE(NEW.past_due, 0) <> 0 THEN
        INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason)
        VALUES (NEW.loan_id, 'fees', NEW.past_due, 'legacy_direct_write',
                'captured from a balances insert during ledger cutover');
    ELSIF TG_OP = 'UPDATE' AND NEW.past_due IS DISTINCT FROM OLD.past_due THEN
        INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason)
        VALUES (NEW.loan_id, 'fees', NEW.past_due - OLD.past_due,
                'legacy_direct_write',
                'captured from a direct balances update during ledger cutover');
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS balances_capture_legacy_delta ON balances;
CREATE TRIGGER balances_capture_legacy_delta
    AFTER INSERT OR UPDATE ON balances
    FOR EACH ROW EXECUTE FUNCTION capture_legacy_balance_delta();

CREATE OR REPLACE FUNCTION balances_cannot_be_deleted_during_cutover() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'balances rows cannot be deleted during ledger cutover';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS balances_reject_delete_during_cutover ON balances;
CREATE TRIGGER balances_reject_delete_during_cutover
    BEFORE DELETE ON balances
    FOR EACH ROW EXECUTE FUNCTION balances_cannot_be_deleted_during_cutover();

