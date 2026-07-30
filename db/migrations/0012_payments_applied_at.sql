-- 0012 — review fix: a charged payment could silently never reach the loan
-- balance. payment-service's charge() commits the payments row, then calls
-- servicing-service to apply it; if that call times out or errors, the
-- exception was swallowed and charge() still returned status "captured" --
-- the card was charged, the row exists, but the balance never moved, with no
-- record that anything was left undone. A retry with the same
-- idempotency_key hit the ON CONFLICT DO NOTHING path and returned the same
-- "captured" result without ever calling servicing-service again, so the
-- balance stayed wrong forever.
--
-- applied_at is the reconciliation flag: NULL means "captured, not yet
-- applied to the loan balance" (a pending/outbox record), set once
-- servicing-service confirms the apply succeeded. A retry on the same
-- idempotency_key now checks this column and retries the apply instead of
-- blindly reporting success -- see services/payment-service/app/payments.py.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ;
