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

COMMIT;
