-- 0042 -- say who captured a payment, so reconciliation compares the right rows.
--
-- Migration 0041 gave reconciliation a join key and made a captured payment with
-- no `processor_ref` a reported break. That was correct for payment-service's
-- rows and wrong for everyone else's, because `payments` has a second live
-- writer: servicing-service's legacy `POST /payments`
-- (services/servicing-service/app/payments.py). It inserts
--
--     INSERT INTO payments (loan_id, last4, brand, amount, method)
--
-- and nothing more, so `auth_status` takes its column default of 'captured'
-- while `captured_at` and `processor_ref` stay NULL. Every call through that
-- route therefore produced a permanent `unreferenced_capture` break: the control
-- breaching on our own writes, forever, for money it was never able to
-- corroborate in the first place.
--
-- **And it could not be corroborated, by construction.** That route calls no
-- processor at all -- it is the vendor prototype path that predates ADR 0008,
-- recorded as D2 -- so no settlement file has ever contained a line for it.
-- Comparing it against one is not a strict control, it is a category error, and
-- the breaks it produced were the kind that teach an operator to stop reading
-- them.
--
-- So provenance becomes a stored fact rather than something inferred from which
-- columns happen to be NULL. Reconciliation's ledger side is exactly
-- `capture_source = 'processor'`; the other values are counted in the run and
-- reported, never silently dropped.
--
-- ## Why the back-fill is safe, and where it is deliberately pessimistic
--
-- * `authorization_id` is only ever written by payment-service, in the same
--   statement that flips `auth_status` to 'captured' after the processor
--   returned (db/migrations/0019). Its presence is evidence a processor
--   authorized the charge, so those rows are back-filled to 'processor'.
-- * Everything else becomes **'unknown'**, not 'processor'. Rows captured before
--   0019 really may have been processor-backed and there is no record proving
--   it, and guessing in the direction that puts them INSIDE a money comparison
--   would manufacture breaks out of missing evidence. Guessing the other way
--   leaves them outside it and counted, which is visible.
-- * The default is 'unknown' for the same reason. A future writer that forgets
--   to set this column is excluded from the comparison and counted, rather than
--   admitted to it as though a processor had vouched for the row.
--
-- The column is additive with a constant default -- catalogue-only in PostgreSQL
-- 11+, no table rewrite on ADD. The two back-fill UPDATEs do rewrite the rows
-- they touch, which is the cost of not guessing.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS capture_source TEXT NOT NULL DEFAULT 'unknown';

-- Processor-backed: payment-service wrote an authorization id, which only ever
-- comes from a real (or explicitly stubbed) processor authorization.
UPDATE payments
   SET capture_source = 'processor'
 WHERE authorization_id IS NOT NULL
   AND capture_source = 'unknown';

ALTER TABLE payments
    DROP CONSTRAINT IF EXISTS payments_capture_source_known;
ALTER TABLE payments
    ADD CONSTRAINT payments_capture_source_known
    CHECK (capture_source IN ('processor', 'servicing_legacy', 'unknown'));

COMMENT ON COLUMN payments.capture_source IS
    'Who captured this payment. ''processor'' means payment-service obtained a '
    'real authorization and the row must appear in a settlement file -- these are '
    'the only rows reconciliation compares. ''servicing_legacy'' is '
    'servicing-service''s prototype POST /payments (D2), which calls no processor, '
    'so no settlement line exists for it. ''unknown'' is the default and covers '
    'rows written before this column: they are counted by reconciliation and '
    'excluded from the comparison, because admitting them would manufacture '
    'breaks out of missing evidence.';

-- Reconciliation's ledger-side predicate is (auth_status, capture_source) with
-- the capture-time window on top, so the index carries all three.
DROP INDEX IF EXISTS idx_payments_captured_at;
CREATE INDEX IF NOT EXISTS idx_payments_captured_at
    ON payments (capture_source, captured_at)
 WHERE auth_status = 'captured';
