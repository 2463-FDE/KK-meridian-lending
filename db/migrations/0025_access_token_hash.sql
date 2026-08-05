-- 0025 -- security fix (PR #6 review, Gap B): the SUBMISSION token was still a
-- plaintext, non-expiring, reusable bearer credential at rest.
--
-- applications.access_token (added by 0006) is minted at submission and proves
-- ownership on the first decision call, for a borrower who has no account yet.
-- It had every problem migration 0022 fixed for the acceptance token and none
-- of the fixes:
--   * stored in plain text -- anyone who can read the table (a backup, a read
--     replica, a future SQL-injection bug elsewhere) can replay it directly;
--   * no expiry -- valid forever;
--   * never consumed -- reusable after the decision it authorised;
--   * compared with a plain `==` in application code, not constant-time.
--
-- Same shape as 0022, for the same reasons:
--   access_token_hash          -- sha256(raw), never the raw value
--   access_token_expires_at    -- server-clock (Postgres now()) expiry
--   access_token_consumed_at   -- stamped by the decision that used it
--
-- Upgrade path for existing local records: there is no production environment
-- for this application (local Docker only), and a plaintext token that has
-- already been exposed cannot be made safe by hashing it after the fact --
-- pretending otherwise would be dishonest. Existing plaintext values are
-- therefore dropped, not migrated. Any borrower mid-flow re-submits to get a
-- fresh hashed, expiring token. Rows keep their applications row and every
-- decision/offer/loan already attached to it; only the dead credential goes.

ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS access_token_hash TEXT,
    ADD COLUMN IF NOT EXISTS access_token_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS access_token_consumed_at TIMESTAMPTZ;

ALTER TABLE applications
    DROP COLUMN IF EXISTS access_token;

-- Point lookup by hash. Partial: most rows have no live submission token
-- (already consumed, expired, or the application predates this column).
CREATE INDEX IF NOT EXISTS idx_applications_access_token_hash
    ON applications (access_token_hash)
    WHERE access_token_hash IS NOT NULL;
