-- 0010 — review fix: payments can double-charge on retry.
-- Hand-tracked, as usual. Authoritative DDL lives in db/init/001_schema.sql.
--
-- Redundant with 0007_payments_idempotency_key.sql (same column, same index) --
-- both branches independently added this column before merging; every
-- statement here is IF NOT EXISTS, so running both in sequence is a safe
-- no-op the second time, not a real conflict.
--
-- A timeout retry or a double-click on submit used to insert a second payments
-- row and apply the balance twice via servicing-service -- there was no
-- idempotency key at all. The unique index is PARTIAL (WHERE idempotency_key
-- IS NOT NULL) so it never fires for existing rows, which predate the caller-
-- supplied key and are legitimately distinct charges, not duplicates to merge.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS payments_idempotency_key_key
    ON payments (idempotency_key) WHERE idempotency_key IS NOT NULL;
