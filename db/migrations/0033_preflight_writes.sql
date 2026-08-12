-- 0033 -- a table whose only job is to prove servicing can still write.
--
-- payment-service preflights servicing before authorizing a card, because a
-- capture that cannot be credited is the worst outcome in this system. That
-- preflight used to run two SELECTs, which proved the database was reachable and
-- nothing more: a read-only replica, a revoked INSERT grant, a read-only
-- transaction or a full disk all let reads pass while the INSERT INTO
-- payment_applications and UPDATE balances that apply_payment_once performs
-- fail. A 200 therefore still greenlit a charge that could not be credited
-- (PR #22 review).
--
-- The preflight now performs a real INSERT and rolls it back. It needs somewhere
-- to write that is not a business table: writing to payment_applications or
-- balances would burn sequence values on every card authorization and put
-- rollback traffic through the tables the money actually lives in.
CREATE TABLE IF NOT EXISTS preflight_writes (
    id         BIGSERIAL PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Deliberately never read and expected to stay empty: every insert is rolled
-- back. A row surviving here means a preflight committed when it should not
-- have, which is worth noticing rather than tidying away.
COMMENT ON TABLE preflight_writes IS
    'Write-path probe for payment-service preflight. Always rolled back; a '
    'surviving row means a preflight committed and should be investigated.';
