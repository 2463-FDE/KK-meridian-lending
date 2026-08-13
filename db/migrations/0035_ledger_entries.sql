-- 0035 -- ADR 0010 step 2: the append-only ledger and its projection.
--
-- Expand/cutover schema. This migration does NOT move application writers onto
-- the ledger -- that is step 3 (PR E) -- and does NOT enable the guard that
-- rejects direct writes to `balances` -- that is step 5. It does install a
-- compatibility capture trigger: old direct writers keep their API behaviour,
-- but every committed delta is mirrored into the immutable ledger so the live
-- backfill cannot be born correct and drift immediately after commit.
--
-- That is deliberate and it is the whole point of expand/contract on a money
-- table: the schema lands before writers are converted, and the parity test plus
-- transitional capture bridge prove no committed balance movement is omitted.
--
-- Invariants this migration is responsible for (ADR 0010's binding list):
--   1. entries are immutable                       -- ledger_entries_immutable
--   2. an entry is a signed delta, never a total   -- amount is signed; CHECK <> 0
--   3. balances is written by the projection alone -- trigger here, guard in step 5
--   4. the sign is keyed to what the borrower owes -- ledger_sign_matches_type
--   5. a human-directed entry names the human      -- ledger_actor_required
--   7. one entry per (payment_id, component)       -- ledger_entries_payment_component,
--                                                     ledger_payment_provenance,
--                                                     ledger_entries_payment_loan_fk

BEGIN;

CREATE TABLE IF NOT EXISTS ledger_entries (
    id           BIGSERIAL   PRIMARY KEY,
    loan_id      INTEGER     NOT NULL REFERENCES loans(id),

    -- What moved. A signed delta, never a total: an entry says "this much
    -- changed", so two concurrent entries compose instead of racing. This is
    -- what closes D3, and it is why `amount` may be negative.
    component    TEXT        NOT NULL CHECK (component IN ('principal','interest','fees')),
    amount       NUMERIC(14,2) NOT NULL CHECK (amount <> 0),

    -- Why it moved. 'opening_balance' is the back-fill's marker and nothing else
    -- may use it: it means "this loan's balance as it stood when the ledger
    -- began, with no record of how it got there". A distinct type rather than a
    -- boolean, because it cannot be defaulted, cannot be forgotten on insert,
    -- and every query that means "real money movements" already filters on
    -- entry_type.
    entry_type   TEXT        NOT NULL CHECK (entry_type IN
                   ('opening_balance','legacy_direct_write','disbursement','payment',
                    'fee_assessed','fee_waived','adjustment')),
    reason       TEXT,

    -- Who moved it.
    actor_id     INTEGER,
    actor_role   TEXT,

    -- Provenance. payment_id makes an apply idempotent by construction.
    --
    -- Deliberately no single-column REFERENCES here: the foreign key is the
    -- COMPOSITE one added below, which ties the payment to the same loan the
    -- entry moves. A plain reference to payments(id) would let an entry cite a
    -- payment captured for one borrower while moving another borrower's
    -- balance -- and ledger rows are immutable, so that movement could never be
    -- corrected by an update. It would sit on the wrong balance permanently.
    payment_id   INTEGER,

    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One transactionally closed initialization gate. It is data, not a session
-- flag: ordinary callers cannot re-open it because the guard trigger below
-- rejects UPDATE/DELETE after the row exists.
CREATE TABLE IF NOT EXISTS ledger_control (
    singleton           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    initialization_open BOOLEAN NOT NULL DEFAULT TRUE
);
INSERT INTO ledger_control(singleton, initialization_open)
VALUES (TRUE, TRUE) ON CONFLICT (singleton) DO NOTHING;

CREATE OR REPLACE FUNCTION ledger_control_cannot_reopen() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.initialization_open = FALSE
       OR NEW.initialization_open = TRUE THEN
        RAISE EXCEPTION 'ledger initialization gate cannot be reopened or deleted';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS ledger_control_one_way ON ledger_control;
CREATE TRIGGER ledger_control_one_way
    BEFORE UPDATE OR DELETE ON ledger_control
    FOR EACH ROW EXECUTE FUNCTION ledger_control_cannot_reopen();

CREATE OR REPLACE FUNCTION ledger_system_entry_is_authorized() RETURNS trigger AS $$
BEGIN
    IF NEW.entry_type = 'legacy_direct_write' AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'legacy_direct_write may only come from the balances capture trigger';
    END IF;
    IF NEW.entry_type = 'opening_balance' AND NOT EXISTS (
        SELECT 1 FROM ledger_control WHERE singleton AND initialization_open
    ) THEN
        RAISE EXCEPTION 'opening_balance is closed after ledger initialization';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS ledger_entries_system_type_guard ON ledger_entries;
CREATE TRIGGER ledger_entries_system_type_guard
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_system_entry_is_authorized();

-- Invariant 7: exactly one entry per payment/component pair. NOT per payment --
-- a waterfall (D14) splits one payment across components by definition, so a
-- single-row rule would forbid the thing the ledger is being built to allow.
-- Idempotency comes from the pair being unique, not from the payment appearing
-- once.
CREATE UNIQUE INDEX IF NOT EXISTS ledger_entries_payment_component
    ON ledger_entries (payment_id, component) WHERE payment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ledger_entries_loan ON ledger_entries (loan_id, occurred_at);

-- Invariant 7, the half the unique index cannot express: a payment entry must
-- HAVE a payment, and nothing else may have one.
--
-- The index above is `UNIQUE (payment_id, component) WHERE payment_id IS NOT
-- NULL`, and NULLs are excluded from it. So without this constraint a
-- ledger-writing path that passed no payment_id would create entries that never
-- collide -- a retried apply posting the balance twice, which is exactly the
-- idempotency the pair is supposed to provide. ADR 0010's invariant 7 says so in
-- words; this is the words made enforceable.
--
-- And the other direction: a non-payment entry may not consume a
-- (payment_id, component) pair, which would block the real payment entry from
-- ever being written. An adjustment's provenance is its proposal (ADR 0011), not
-- a payment.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_payment_provenance;
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_payment_provenance CHECK (
    (entry_type = 'payment') = (payment_id IS NOT NULL)
);

-- The payment must belong to the SAME loan the entry moves.
--
-- `loan_id` and `payment_id` were independent, so a row could cite a payment
-- captured for loan A while projecting the movement onto loan B. Nothing in the
-- schema said otherwise, and the ledger is immutable: a wrong movement cannot be
-- corrected by updating the row, so it stays on that borrower's balance for
-- good.
--
-- Enforced by a COMPOSITE foreign key rather than a trigger, because it is a
-- referential fact and a declarative constraint cannot be forgotten, bypassed by
-- a session flag, or dropped independently of the column it protects. It needs a
-- unique key on the referenced pair; `payments.id` is already the primary key, so
-- `(id, loan_id)` is unique by construction and the index costs only space.
--
-- MATCH SIMPLE (the default) is what makes this work alongside the CHECK above:
-- when `payment_id` is NULL the constraint does not apply at all, which is
-- exactly right for the non-payment entry types. When it is present, both columns
-- must match a real payments row -- so an entry may not cite a payment that is
-- not attached to any loan either.
-- Dropped in dependency order and re-added in the reverse, so a replay of this
-- file is idempotent. The unique key cannot be dropped while the foreign key
-- depends on its index, and this migration is replayed by
-- db/tests/test_migration_paths_converge.py precisely to catch that.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_entries_payment_loan_fk;
ALTER TABLE payments       DROP CONSTRAINT IF EXISTS payments_id_loan_uniq;

ALTER TABLE payments ADD CONSTRAINT payments_id_loan_uniq UNIQUE (id, loan_id);
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_entries_payment_loan_fk
    FOREIGN KEY (payment_id, loan_id) REFERENCES payments (id, loan_id);

-- The same rule again, BEFORE the row is inserted.
--
-- Not redundancy for its own sake. The composite key is the guarantee; this is
-- what makes the failure legible and makes it happen FIRST. A referential
-- constraint is checked by an internal AFTER-row trigger, so without this the
-- projection trigger can run before it -- and if the wrongly-named loan has no
-- `balances` row, the error a developer sees is the projection's row-count
-- complaint about loan B rather than the fact that the entry cited loan A's
-- payment. Same rollback either way, entirely different diagnosis, and the
-- misleading one arrives on the path most likely to be hit.
CREATE OR REPLACE FUNCTION ledger_entry_payment_matches_loan() RETURNS trigger AS $$
DECLARE
    payment_loan INTEGER;
BEGIN
    IF NEW.payment_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT loan_id INTO payment_loan FROM payments WHERE id = NEW.payment_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger entry names payment % which does not exist',
                        NEW.payment_id;
    END IF;
    IF payment_loan IS DISTINCT FROM NEW.loan_id THEN
        RAISE EXCEPTION 'ledger entry moves loan % but cites payment %, which '
                        'was captured for loan %',
                        NEW.loan_id, NEW.payment_id, payment_loan;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_payment_belongs_to_loan ON ledger_entries;
CREATE TRIGGER ledger_entries_payment_belongs_to_loan
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_payment_matches_loan();

-- Invariant 4: the sign is keyed to the effect on what the borrower owes.
-- A payment reduces; a fee assessment increases; only an adjustment may go
-- either way, which is exactly why it is the type that needs an approver.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_sign_matches_type;
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_sign_matches_type CHECK (
       (entry_type = 'payment'         AND amount < 0)
    OR (entry_type = 'fee_waived'      AND amount < 0)
    OR (entry_type = 'fee_assessed'    AND amount > 0)
    OR (entry_type = 'disbursement'    AND amount > 0)
    OR (entry_type = 'opening_balance' AND amount <> 0)
    OR (entry_type = 'legacy_direct_write' AND amount <> 0)
    OR (entry_type = 'adjustment')
);

-- Invariant 5: a human-directed entry names the human.
-- 'payment' is exempt: servicing's apply-payment receives an amount and a
-- payment_id and no actor, because the borrower is not "acting" on the balance
-- in the sense this column means. Requiring one here would fail every real
-- payment on insert. Its provenance is stronger than an actor string anyway --
-- payment_id points at the row carrying the idempotency key and the capture.
-- 'opening_balance' is exempt because no one authored it.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_actor_required;
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_actor_required CHECK (
    entry_type IN ('disbursement','fee_assessed','payment','opening_balance',
                   'legacy_direct_write')
    OR (actor_id IS NOT NULL AND actor_role IS NOT NULL)
);

-- Invariant 1: append-only. A trigger rather than a REVOKE, for the reason
-- ADR 0002/0006 already established for decision_events: every service connects
-- as the schema-owning role, so a revoke from the owner does not stick.
CREATE OR REPLACE FUNCTION ledger_entries_are_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entries is append-only (attempted % on id %)',
                    TG_OP, COALESCE(OLD.id, NEW.id);
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_immutable ON ledger_entries;
CREATE TRIGGER ledger_entries_immutable
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_are_immutable();

-- Invariant 3: `balances` is a projection. This trigger is what maintains it.
--
-- Opening-state and legacy-capture entries describe a balance mutation that has
-- already happened, so those two explicit types do not project. No session flag
-- can suppress a normal entry: a caller-controlled GUC would be a permanent
-- ledger/projection bypass.
CREATE OR REPLACE FUNCTION project_ledger_entry() RETURNS trigger AS $$
DECLARE
    projected INTEGER;
BEGIN
    IF NEW.entry_type IN ('opening_balance', 'legacy_direct_write') THEN
        RETURN NEW;
    END IF;

    PERFORM set_config('meridian.projecting', 'on', true);

    IF NEW.component = 'principal' THEN
        UPDATE balances SET balance  = balance  + NEW.amount, updated_at = now()
         WHERE loan_id = NEW.loan_id;
        GET DIAGNOSTICS projected = ROW_COUNT;
    ELSIF NEW.component = 'fees' THEN
        UPDATE balances SET past_due = past_due + NEW.amount, updated_at = now()
         WHERE loan_id = NEW.loan_id;
        GET DIAGNOSTICS projected = ROW_COUNT;
    ELSE
        -- 'interest' projects nowhere: it is owed within a payment, not a
        -- separate balance the borrower carries. See ADR 0010. Nothing was
        -- updated and nothing should have been, so the check below is skipped.
        projected := 1;
    END IF;

    -- Exactly one row, or the entry does not exist.
    --
    -- Without this the UPDATE silently matches zero rows when a loan has no
    -- `balances` row, and the insert still succeeds: the ledger records that
    -- money moved and no balance moves with it. That is the projection claiming
    -- to be maintained while it is not, which is the one failure this design
    -- cannot tolerate -- `balances` is derived, so a divergence is invisible
    -- until a parity run, and the entry is immutable so it cannot be corrected
    -- afterwards.
    --
    -- Raising rolls back the INSERT with it. An entry that could not be
    -- projected must not be retained: it would be a permanent, uncorrectable
    -- record of a movement that never reached the borrower's balance.
    IF projected <> 1 THEN
        RAISE EXCEPTION 'ledger entry for loan % projected onto % balance rows '
                        '(expected exactly 1)', NEW.loan_id, projected;
    END IF;

    PERFORM set_config('meridian.projecting', 'off', true);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_project ON ledger_entries;
CREATE TRIGGER ledger_entries_project
    AFTER INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION project_ledger_entry();

-- The write-guard function ships now and the TRIGGER IS NOT CREATED. Step 5
-- enables it, once every writer has been converted (step 3) and verified. Having
-- the function present makes that step one statement, and makes reverting it one
-- statement too.
CREATE OR REPLACE FUNCTION balances_are_trigger_maintained() RETURNS trigger AS $$
BEGIN
    IF current_setting('meridian.projecting', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'balances is maintained by the ledger projection; '
                        'write a ledger entry instead';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

COMMENT ON FUNCTION balances_are_trigger_maintained() IS
    'ADR 0010 step 5. Not attached to a trigger yet -- every writer must be '
    'converted first, or the guard turns working code into exceptions. Enable '
    'with: CREATE TRIGGER balances_guard BEFORE UPDATE OR DELETE ON balances '
    'FOR EACH ROW EXECUTE FUNCTION balances_are_trigger_maintained();';

-- Expand/cutover bridge. Existing application versions still UPDATE balances
-- directly, so a lock held only for the snapshot would cease protecting parity
-- the instant this transaction commits. Install the capture trigger in the SAME
-- transaction as the backfill: a writer already in flight waits for our table
-- lock, then sees this trigger when it resumes after commit. Every direct delta
-- therefore has an immutable entry even before step 3 converts the writers.
--
-- This transitional type is intentionally honest about limited provenance. A
-- database trigger can prove what changed and in which transaction; it cannot
-- invent a payment id or human actor the legacy UPDATE did not carry.
CREATE OR REPLACE FUNCTION capture_legacy_balance_delta() RETURNS trigger AS $$
BEGIN
    IF current_setting('meridian.projecting', true) = 'on' THEN
        RETURN NEW;
    END IF;

    IF TG_OP = 'INSERT' AND NEW.balance <> 0 THEN
        INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason)
        VALUES (NEW.loan_id, 'principal', NEW.balance,
                'legacy_direct_write',
                'captured from a balances insert during ledger cutover');
    ELSIF TG_OP = 'UPDATE' AND NEW.balance IS DISTINCT FROM OLD.balance THEN
        INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason)
        VALUES (NEW.loan_id, 'principal', NEW.balance - OLD.balance,
                'legacy_direct_write',
                'captured from a direct balances update during ledger cutover');
    END IF;
    IF TG_OP = 'INSERT' AND COALESCE(NEW.past_due, 0) <> 0 THEN
        INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason)
        VALUES (NEW.loan_id, 'fees', NEW.past_due,
                'legacy_direct_write',
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
-- Blocks every current UPDATE/INSERT/DELETE writer before the snapshot. The
-- lock is held through trigger installation, parity validation and COMMIT.
LOCK TABLE balances IN SHARE ROW EXCLUSIVE MODE;

-- --------------------------------------------------------------------------
-- Back-fill: one opening_balance entry per loan, equal to that loan's current
-- balance, WITH THE PROJECTION SUPPRESSED.
--
-- Not "zero the balance and reproject". That needs a pause to be safe -- a live
-- payment landing on a zeroed balance is lost -- and this system does not pause.
-- `balances` already holds the number; the entry records it.
--
-- Idempotent: a loan that already has an opening_balance entry is skipped, so a
-- re-run cannot double it. That also makes this safe to run again after an
-- interrupted migration, and it is why `opening_balance` is a distinct type --
-- it is the marker that says which loans are already initialised.
-- --------------------------------------------------------------------------
DO $$
DECLARE
    seeded   INTEGER;
    skipped  INTEGER;
    zero_bal INTEGER;
BEGIN
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

    -- A migration that silently seeds nothing looks identical to one that seeded
    -- everything, so both counts are reported.
    RAISE NOTICE '0035: opened % principal entry(ies); % loan(s) now have an '
                 'opening balance; % loan(s) at zero were skipped (an entry of '
                 'amount 0 is not a movement and the CHECK forbids it)',
                 seeded, skipped, zero_bal;
END $$;

UPDATE ledger_control SET initialization_open = FALSE
 WHERE singleton AND initialization_open;

-- Installed after the snapshot, before the transaction commits. A writer that
-- was already in flight has waited behind the lock and resumes through this
-- trigger; there is no gap between snapshot and delta capture.
CREATE TRIGGER balances_capture_legacy_delta
    AFTER INSERT OR UPDATE ON balances
    FOR EACH ROW EXECUTE FUNCTION capture_legacy_balance_delta();

-- Do not publish a ledger born out of balance. This is per loan/component so
-- equal and opposite errors on different borrowers cannot net to zero.
DO $$
DECLARE
    mismatch RECORD;
BEGIN
    SELECT b.loan_id, b.balance, b.past_due,
           COALESCE(SUM(le.amount) FILTER (WHERE le.component = 'principal'), 0) AS principal_ledger,
           COALESCE(SUM(le.amount) FILTER (WHERE le.component = 'fees'), 0) AS fees_ledger
      INTO mismatch
      FROM balances b
      LEFT JOIN ledger_entries le ON le.loan_id = b.loan_id
     GROUP BY b.loan_id, b.balance, b.past_due
    HAVING b.balance <> COALESCE(SUM(le.amount) FILTER (WHERE le.component = 'principal'), 0)
        OR COALESCE(b.past_due, 0) <> COALESCE(SUM(le.amount) FILTER (WHERE le.component = 'fees'), 0)
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION '0035 parity failed for loan %: balance % vs ledger %, past_due % vs fees %',
                        mismatch.loan_id, mismatch.balance, mismatch.principal_ledger,
                        mismatch.past_due, mismatch.fees_ledger;
    END IF;
END $$;

COMMENT ON TABLE ledger_entries IS
    'ADR 0010: append-only record of every balance movement. balances is a '
    'projection of this table, maintained by project_ledger_entry(). '
    'SUM(amount) per loan and component is the auditable truth.';

COMMIT;
