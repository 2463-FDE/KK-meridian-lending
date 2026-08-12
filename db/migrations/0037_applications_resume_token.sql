-- 0037 -- separate "which retry is this" from "may this caller recover it".
--
-- 0036 made intake retry-safe with a client-supplied idempotency key, and then
-- let that key MINT A FRESH ACCESS TOKEN on resume. That turned the key into a
-- credential: anyone who learned or guessed it -- and a client-chosen key is not
-- a secret, it travels in request bodies, proxy logs and client-side code --
-- could present it, receive a live access token, and from there request a
-- decision, read the application and trigger a credit pull. Application
-- takeover, through the retry path added to make retries safe.
--
-- The flawed reasoning was written down in the docstring: "it is safe because
-- the caller has just proved it owns this application by presenting the key that
-- created it". Presenting an identifier is not proof of ownership. That is the
-- whole distinction this migration exists to restore:
--
--   idempotency_key  -- identifies WHICH application a retry belongs to
--   resume_token     -- authorises the caller to recover it
--
-- One is a name, the other is a secret, and only the second is server-generated.
--
-- Stored as a sha256 hash, like access_token_hash and for the same reason: the
-- raw value exists in the response body that issued it and nowhere else, so a
-- database read cannot recover a usable credential.

ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS resume_token_hash       TEXT,
    ADD COLUMN IF NOT EXISTS resume_token_expires_at TIMESTAMPTZ,
    -- Single-use. A recovered application issues a NEW resume token and consumes
    -- the old one, so a token captured from a log or a proxy cannot be replayed
    -- after the legitimate client has used it.
    ADD COLUMN IF NOT EXISTS resume_token_consumed_at TIMESTAMPTZ;

COMMENT ON COLUMN applications.resume_token_hash IS
    'sha256 of the server-generated resume token. Recovery requires the '
    'idempotency key AND this token: the key says which application, the token '
    'authorises the caller. Never store the raw value.';
