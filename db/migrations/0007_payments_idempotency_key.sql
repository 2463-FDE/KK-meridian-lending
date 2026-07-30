-- 0007 — review fix: a timeout retry or a double-click on POST /payments
-- inserted a second payments row and applied the balance twice via
-- servicing-service -- there was no idempotency key at all. Caller-supplied,
-- required at the API boundary (see payment-service/app/schemas.PaymentIn);
-- the partial unique index below is what makes a retry with the same key a
-- safe no-op (INSERT ... ON CONFLICT DO NOTHING) instead of a second charge.
-- NULL stays legal so any pre-existing row from before this column existed
-- is never treated as colliding with anything.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS payments_idempotency_key_key
    ON payments (idempotency_key) WHERE idempotency_key IS NOT NULL;
