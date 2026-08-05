-- 0019 -- review fix: a crash between the processor approving a charge and
-- payment-service's own auth_status UPDATE running (db/migrations/0017) left
-- a payment row stuck 'pending' with a real authorization already issued at
-- the processor, but no local record of THAT authorization existed at all --
-- a same-key retry had nothing to check before calling
-- processor.authorize_charge() again. authorization_id is now written in the
-- SAME UPDATE that flips auth_status to 'captured' (one atomic write, not
-- two), and a pending retry calls processor.get_authorization() to ask the
-- processor for its own record of the idempotency_key before ever charging
-- again. See services/payment-service/app/payments.py, processor.py.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS authorization_id TEXT;
