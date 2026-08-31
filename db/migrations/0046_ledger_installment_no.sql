-- 0046: name the installment a late fee belongs to.
--
-- WHY THIS EXISTS
--
-- The client's late-fee rule of 2026-08-29 (policies/fee_schedule.md, docs/DEBT.md
-- D23) is "at most ONE fee per missed scheduled installment, never reassessed
-- against the same installment". `ledger_entries` could not express that: a
-- `fee_assessed` row carried a free-text `reason` and no period, so nothing in
-- the database could refuse a second fee for one installment, and the
-- application could not check for one without parsing prose.
--
-- D23 named this exact addition as the first part of the smallest change that
-- makes the rule implementable. This migration is that part and only that part.
--
-- WHAT THIS DOES NOT DO
--
-- It does NOT attribute PAYMENTS to installments, and it does not change how any
-- fee is priced. Both are deliberately out of scope: pricing the decided rule
-- needs "unpaid scheduled principal and interest for THAT installment", which in
-- turn needs an allocation order across installments that no spec, ADR or policy
-- in this repository publishes. Inventing that order is the one thing D23 says
-- must not happen, so the column that would record it is not added here either --
-- an unused `installment_no` on a `payment` row would read as though attribution
-- had been captured when it had not.
--
-- HISTORY STAYS EXPLICITLY UNKNOWN
--
-- `installment_no` is nullable with no default and no backfill. Every existing
-- row keeps NULL, and NULL here means "never captured", not "installment 0" and
-- not "unknown but probably the current one". That distinction is the whole
-- point: those rows were written by a rule that had no concept of an
-- installment, so there is no true value to write. Reconstructing one would mean
-- running an allocation policy that did not exist at the time and recording the
-- result as though it had been observed.
BEGIN;

ALTER TABLE ledger_entries ADD COLUMN IF NOT EXISTS installment_no INTEGER;

-- Installments are 1-based, matching `schedule.amortization_from_contract`,
-- whose rows start at n = 1. A zero or negative period is not a period.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_installment_no_positive;
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_installment_no_positive
    CHECK (installment_no IS NULL OR installment_no >= 1);

-- Only a fee assessment may name an installment today, because a fee assessment
-- is the only writer that knows one. Restricting it is not caution for its own
-- sake: leaving the column open to `payment` rows would let a reader assume
-- payment attribution exists somewhere, and it does not. When the allocation
-- order is decided and payment attribution is built, this constraint is the
-- thing that has to be widened deliberately -- which is the point.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_installment_only_on_fee_assessed;
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_installment_only_on_fee_assessed
    CHECK (installment_no IS NULL OR entry_type = 'fee_assessed');

-- ONE fee per installment, enforced by the database rather than by an
-- application check.
--
-- This is what makes the concurrent-assessor and the retry cases safe by
-- construction rather than by a lock somebody has to remember to take: a second
-- assessment for the same (loan, installment) fails on the unique index inside
-- its own transaction, whatever order the two sessions interleave in.
--
-- Keyed on `installment_no IS NOT NULL` rather than on the entry type alone, so
-- a future fee that is NOT installment-scoped -- an NSF charge, say, which
-- policies/fee_schedule.md prices per returned payment rather than per period --
-- writes NULL and does not participate in this index at all. Today
-- `delinquency.assess_late_fee` is the only application writer of
-- `fee_assessed`, so in practice this index covers late fees; it is written so
-- that it still means the right thing when it is not.
CREATE UNIQUE INDEX IF NOT EXISTS ledger_one_late_fee_per_installment
    ON ledger_entries (loan_id, installment_no)
    WHERE entry_type = 'fee_assessed' AND installment_no IS NOT NULL;

-- The installment cited must EXIST on that loan.
--
-- Codex review of PR #143 (FEE-INSTALLMENT-BOUNDS-001): with only
-- `installment_no >= 1`, a 36-month loan accepted `installment_no = 37` or 999.
-- The unique index above then guaranteed "one fee per installment NUMBER", which
-- is not the rule -- the client's rule is one fee per missed scheduled
-- installment, and an installment past the end of the term is not one. A fee
-- could have claimed an identity that was false while satisfying every
-- constraint.
--
-- A CHECK cannot express this: it may not read another table. So it is a trigger,
-- and it is BEFORE INSERT rather than a periodic audit because a fee whose period
-- does not exist must never reach the ledger -- the ledger is append-only, so
-- there is no correcting it afterwards.
--
-- Bounded on `term_months`, which is what the contract's period count IS
-- (`loans_schedule_term_agrees` ties `regular_payment_count + 1` to it). This
-- does not re-derive the schedule: expanding an amortization inside a trigger
-- would put the money path's arithmetic in two places, and `installments.py` is
-- the one place it belongs. The trigger asks the narrower question the database
-- can answer cheaply and exactly -- is this period inside the term.
CREATE OR REPLACE FUNCTION ledger_installment_is_in_the_loan_schedule()
RETURNS TRIGGER AS $$
DECLARE
    loan RECORD;
    term INTEGER;
BEGIN
    IF NEW.installment_no IS NULL THEN
        RETURN NEW;
    END IF;

    -- LOCKED while validating. Without this the read is time-of-check/
    -- time-of-use: a concurrent transaction could shorten the term or clear the
    -- schedule between this check and the ledger insert committing, leaving a fee
    -- citing an installment that no longer exists.
    --
    -- FOR SHARE, not FOR UPDATE: the requirement is that the contract must not
    -- CHANGE while this commits, not that this transaction owns the loan. Two
    -- fees on different installments of the same loan can validate together.
    SELECT term_months, schedule_version, regular_payment,
           regular_payment_count, final_payment
      INTO loan
      FROM loans WHERE id = NEW.loan_id FOR SHARE;

    -- The CONTRACT must exist before a fee may cite one of its periods.
    --
    -- Codex review of PR #143 (SCHEDULELESS-INSTALLMENT-002): checking
    -- `term_months` alone was not enough. `loans_schedule_all_or_nothing` permits
    -- a loan with a term and NO stored schedule -- the four schedule columns are
    -- all-or-nothing, and all-NULL is a legal state for a legacy loan boarded
    -- before db/migrations/0030. The database would then accept
    -- `fee_assessed + installment_no = 1` for a loan whose servicing layer
    -- REFUSES to derive any installment at all (`installments.ScheduleNotAvailable`),
    -- producing a ledger row whose claimed identity nothing can resolve.
    --
    -- The columns checked here are exactly `installments._CONTRACT_FIELDS` plus
    -- `schedule_version`, which is what `installments_for()` requires before it
    -- will expand a contract. The two must agree: if the database accepts a
    -- period the application cannot derive, the fee is a false identity, and if
    -- the application derives one the database rejects, the fee cannot be written.
    --
    -- No schedule is invented. The fee is refused.
    IF loan.schedule_version IS NULL
       OR loan.regular_payment IS NULL
       OR loan.regular_payment_count IS NULL
       OR loan.final_payment IS NULL THEN
        RAISE EXCEPTION
            'loan % has no stored contractual schedule, so installment % cannot '
            'be cited by a fee', NEW.loan_id, NEW.installment_no;
    END IF;

    term := loan.term_months;

    -- Defensive: `ledger_entries_loan_id_fkey` makes a missing loan
    -- unreachable, and `loans.term_months` is NOT NULL. Refusing rather than
    -- passing keeps the failure mode "no fee" instead of "unvalidated fee" if
    -- either of those ever changes.
    IF term IS NULL THEN
        RAISE EXCEPTION
            'loan % has no term, so installment % cannot be validated',
            NEW.loan_id, NEW.installment_no;
    END IF;

    IF NEW.installment_no > term THEN
        RAISE EXCEPTION
            'installment % is past the end of loan %''s %-month term',
            NEW.installment_no, NEW.loan_id, term;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_installment_in_schedule ON ledger_entries;
CREATE TRIGGER ledger_entries_installment_in_schedule
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_installment_is_in_the_loan_schedule();

-- ---------------------------------------------------------------------------
-- The contract cannot change underneath a fee that cites one of its installments.
-- ---------------------------------------------------------------------------
--
-- The trigger above proves installment N is real AT INSERT TIME. That is only
-- half the guarantee: `loans` has no immutability trigger of any kind, and the
-- schedule-defining columns are plain updatable columns. A fee written against
-- installment 36 of a 36-month loan stays on an append-only ledger forever, so if
-- the term were later changed to 24 the ledger would hold an installment identity
-- that is no longer valid against the contract -- and nothing would have noticed.
--
-- Locking the row during the insert closes the concurrent case and does nothing
-- about the later one, so both are needed.
--
-- SCOPED TO LOANS THAT ACTUALLY CARRY AN INSTALLMENT-SCOPED FEE, deliberately.
-- This does NOT make boarded contracts immutable in general: `db/init/003_seed_bulk.sql`
-- legitimately back-fills these columns from the accepted offer, and correcting a
-- contract on a loan nothing has cited is a different question that this change has
-- no authority to answer. What is protected is exactly the invariant the ledger
-- relies on -- no more, and stated rather than assumed.
--
-- `note_rate_pct` and `opened_at` are included because installment identity is
-- derived from them too: `installments.py` anchors due dates on `opened_at` and
-- splits interest at the note rate, so changing either silently redefines which
-- period a fee refers to.
CREATE OR REPLACE FUNCTION loans_contract_is_frozen_once_cited() RETURNS trigger AS $$
DECLARE
    cited INTEGER;
    before_row JSONB := to_jsonb(OLD);
    after_row  JSONB := to_jsonb(NEW);
    col TEXT;
    changed BOOLEAN := FALSE;
BEGIN
    -- Compared through `to_jsonb` rather than as `NEW.column`, and that is a fix
    -- rather than a flourish. `NEW.note_rate_pct` RAISES on a loans table that
    -- predates `db/migrations/0038` -- the migration that adds the column -- so
    -- naming the fields directly made this trigger explode inside 0038's own
    -- expand test, which builds exactly that older shape. A missing key reads as
    -- NULL on both sides here, so a column that does not exist yet simply cannot
    -- have changed.
    FOREACH col IN ARRAY ARRAY['term_months', 'schedule_version', 'regular_payment',
                               'regular_payment_count', 'final_payment',
                               'note_rate_pct', 'opened_at'] LOOP
        IF (before_row -> col) IS DISTINCT FROM (after_row -> col) THEN
            changed := TRUE;
            EXIT;
        END IF;
    END LOOP;

    IF NOT changed THEN
        RETURN NEW;                       -- nothing that defines an installment moved
    END IF;

    -- Same reasoning one level up: a schema with `loans` but no `ledger_entries`
    -- is a legitimate intermediate state for a migration test, and nothing can
    -- have cited an installment there.
    IF to_regclass('ledger_entries') IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT count(*) INTO cited
      FROM ledger_entries
     WHERE loan_id = OLD.id AND installment_no IS NOT NULL;

    IF cited > 0 THEN
        RAISE EXCEPTION
            'loan % has % ledger entr(y/ies) citing an installment; its '
            'contractual schedule cannot change underneath them',
            OLD.id, cited;
    END IF;

    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS loans_contract_frozen_once_cited ON loans;
CREATE TRIGGER loans_contract_frozen_once_cited
    BEFORE UPDATE ON loans
    FOR EACH ROW EXECUTE FUNCTION loans_contract_is_frozen_once_cited();

COMMIT;
