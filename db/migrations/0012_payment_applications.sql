-- 0012 — review fix, other half of 0011: servicing-service's apply-payment
-- endpoint applied the balance change unconditionally every time it was
-- called, with no idempotency of its own -- it trusted payment-service to
-- never call it twice for the same payment. Once payment-service started
-- retrying a pending apply (0011), that assumption had to become a real
-- guarantee: apply-payment must be safe to call more than once for the same
-- payment_id (a payment-service retry, or two requests racing) and only
-- ever move the balance once.
--
-- payment_applications is that guard: an atomic INSERT ... ON CONFLICT
-- DO NOTHING keyed on payment_id runs before the balance is touched -- the
-- balance update only happens for the caller whose INSERT actually landed a
-- new row. See services/servicing-service/app/balance.py.

CREATE TABLE IF NOT EXISTS payment_applications (
    payment_id  INTEGER PRIMARY KEY,
    loan_id     INTEGER NOT NULL,
    amount      NUMERIC(14,2) NOT NULL,
    applied_at  TIMESTAMPTZ DEFAULT now()
);
