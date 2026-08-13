-- 0041 -- store the processor's OWN settlement reference on a captured payment.
--
-- Reconciliation compared net totals per loan. Two wrong transactions on the
-- same loan therefore cancelled: an unrecorded capture of 99.99 and a missing
-- refund of 99.99 on loan 4471 produce the same per-loan total as a correct
-- day, so the run recorded `outcome = 'ok'` while real money movement was
-- wrong. A control that nets its own errors away is worse than no control,
-- because it publishes a success timestamp for the netting.
--
-- The reason it netted was that there was no join key. `authorization_id`
-- (migration 0019) is minted by OUR authorization call; the settlement file
-- identifies each line by the PROCESSOR's reference (`processor_ref`, e.g.
-- PR-100231). The two are different identifiers, so a payment row could not be
-- matched to a settlement line and per-loan totals were all that was left.
--
-- This column is that key. With it, reconciliation compares transaction by
-- transaction, and two offsetting defects surface as two breaks instead of
-- none.
--
-- ## Why this migration is safe to run on a live payments table
--
-- * Additive only. `ADD COLUMN ... TEXT` with no default and no NOT NULL is a
--   catalogue-only change in PostgreSQL 11+ -- no table rewrite, no long
--   ACCESS EXCLUSIVE hold on the busiest table in the system.
-- * No back-fill, and that is deliberate. There is nothing to back-fill FROM:
--   historical rows never recorded the processor's reference, and deriving one
--   from `authorization_id` would invent a join key that matches nothing in
--   any settlement file. A fabricated reference is worse than a NULL, because
--   a NULL is visibly missing and a wrong reference looks like evidence.
-- * No CHECK constraint requiring the reference on captured rows. That was
--   considered and rejected: the capture UPDATE runs AFTER the processor has
--   already taken the money, so a constraint violation there would leave a
--   real charge unrecorded. Money safety wins over schema tidiness. The gap is
--   made visible in the control instead -- a captured payment inside the
--   reconciliation window with no `processor_ref` is reported as a break
--   (`unreferenced_capture`), not silently skipped, so unreferenced rows cost
--   an operator's attention rather than hiding.
--
-- Legacy rows therefore surface as breaks for the days they fall in, and only
-- for those days: the window moves, and every capture written after this
-- migration carries the reference.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS processor_ref TEXT;

COMMENT ON COLUMN payments.processor_ref IS
    'The processor''s own settlement reference for this capture (e.g. PR-100231), '
    'written in the same UPDATE that sets auth_status to captured. This is the '
    'join key to the settlement file; authorization_id (migration 0019) is a '
    'different identifier minted by our own authorization call and appears in no '
    'settlement file. NULL on rows captured before this migration -- there was '
    'nothing to back-fill from, and reconciliation reports such rows as '
    'unreferenced_capture breaks rather than skipping them.';

-- One settlement line, one capture. A processor reference identifies a single
-- movement of money, so two payment rows claiming the same reference is either
-- a double-recorded capture or a mis-keyed one -- and either would make the
-- transaction-level comparison ambiguous exactly where it has to be exact.
--
-- Partial, so the unreferenced legacy rows above do not collide with each
-- other. NULLs would not collide in a plain unique index either; the predicate
-- says so explicitly rather than relying on that.
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_processor_ref
    ON payments (processor_ref)
 WHERE processor_ref IS NOT NULL;
