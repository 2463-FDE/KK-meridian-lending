-- 0040 -- record WHEN a payment was captured, not just when the row appeared.
--
-- Reconciliation scoped the ledger side of its comparison by
-- `payments.created_at`. That column is written when the row is INSERTed, which
-- happens at `auth_status = 'pending'` -- BEFORE the processor is called. The
-- flip to 'captured' happens afterwards, and an authorization that is slow, is
-- retried, or recovers from a crash can land on the following day.
--
-- So a capture the processor settles on the 9th could carry a `created_at` of
-- the 8th. The 9th's settlement file is then compared against a ledger window
-- that excludes it, and the loan is reported as a money break. Nothing is
-- actually wrong, and this is the worst kind of false positive for a money
-- control: it burns the reviewer's trust, and a control people learn to
-- disbelieve is a control that has stopped working.
--
-- `captured_at` is written in the SAME UPDATE that sets `auth_status` and
-- `authorization_id`, so a captured row cannot exist without the timestamp that
-- says when it happened.
--
-- The back-fill is deliberate and lossy, and saying so is the point. Rows that
-- were already captured before this column existed have no record of when the
-- processor confirmed them, so `created_at` is the only estimate available. For
-- the overwhelming majority it is right to the second; for the ones that
-- crossed a midnight it is the same approximation reconciliation was already
-- making, so this is no worse than the status quo for historical data and
-- correct for everything after it. Inventing a different value would be worse:
-- it would look like evidence.
--
-- New rows are never back-filled: the application always writes the real value.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ;

-- Historical rows only. `captured_at IS NULL AND auth_status = 'captured'` can
-- only be true for rows written before this migration, because the application
-- now sets both together.
UPDATE payments
   SET captured_at = created_at
 WHERE auth_status = 'captured'
   AND captured_at IS NULL;

COMMENT ON COLUMN payments.captured_at IS
    'When the processor confirmed the capture -- written in the same UPDATE that '
    'sets auth_status to captured. Reconciliation scopes its window on this, not '
    'on created_at, which is stamped at INSERT while the row is still pending '
    'and can therefore fall on the previous day. Rows captured before migration '
    '0040 were back-filled from created_at, which is an approximation and the '
    'same one reconciliation was already making.';

-- The window predicate reads this on every run.
CREATE INDEX IF NOT EXISTS idx_payments_captured_at
    ON payments (captured_at)
 WHERE auth_status = 'captured';
