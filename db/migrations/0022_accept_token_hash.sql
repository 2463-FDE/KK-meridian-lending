-- 0022 -- security fix: the acceptance token (the one-time proof of
-- ownership an anonymous, no-account borrower uses to accept their own
-- offer -- see routers/applications.py accept_offer) was stored in plain
-- text (applications.accept_token, added by 0015) and never expired. A
-- plaintext bearer credential sitting in the database is a live secret any
-- reader of that table (a backup, a read replica, a future SQL-injection
-- bug elsewhere) could use directly; with no expiry it also stayed valid
-- forever, including after a rerun or staff correction changed the
-- decision away from APPROVE (application code now revokes it in that
-- case -- see decision_state.issue_accept_token/revoke_accept_token -- but
-- the schema itself should not be ABLE to hold a bearer secret at rest).
--
-- Replaces the single plaintext column with:
--   accept_token_hash          -- sha256(raw token), never the raw value
--   accept_token_expires_at    -- server-clock (Postgres now()) expiry;
--                                  never trust a client-supplied timestamp
--   accept_token_consumed_at   -- set the moment the token boards a loan;
--                                  a consumed token can never be replayed,
--                                  independent of the hash/expiry checks
--
-- No production environment exists for this application (local Docker only
-- -- see repo README/ARCHITECTURE.md). Any existing plaintext accept_token
-- issued locally is intentionally invalidated by dropping the column
-- outright, rather than attempting to "migrate" it into a hash (hashing an
-- already-plaintext-exposed value doesn't undo the exposure, and pretending
-- it does would be dishonest). A borrower with an in-flight accept must
-- re-request their decision (POST /applications/{id}/decision) to receive a
-- fresh, hashed, expiring token. Forward-fix, not a preserve-and-migrate.

ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS accept_token_hash TEXT,
    ADD COLUMN IF NOT EXISTS accept_token_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS accept_token_consumed_at TIMESTAMPTZ;

ALTER TABLE applications
    DROP COLUMN IF EXISTS accept_token;

-- Point lookup by hash (accept_offer's ownership check) -- partial index
-- since most rows have no live token (never approved, already consumed, or
-- revoked).
CREATE INDEX IF NOT EXISTS idx_applications_accept_token_hash
    ON applications (accept_token_hash)
    WHERE accept_token_hash IS NOT NULL;
