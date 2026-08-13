-- 0039 -- keep the previous access token valid across one rotation.
--
-- The defect this closes is a composition of two changes that were each
-- correct alone.
--
-- `resume_application` rotates `access_token_hash` on every authorised retry,
-- while the recovery secret stays reusable by design (0037 -- rotating it
-- rebuilds the lost-response hole). Two overlapping retries therefore both pass
-- the resume check and both rotate, and the later UPDATE invalidates the access
-- token already returned to the earlier caller.
--
-- That was tolerable while the earlier caller could simply resume again. It
-- stopped being tolerable when the browser began clearing the retry credentials
-- the moment intake succeeds: the earlier caller received a 200, discarded its
-- credentials, and then found its access token dead with nothing left to
-- recover with. Locked out of decisioning, and the obvious next move is to
-- resubmit -- creating the duplicate application this whole contract exists to
-- prevent.
--
-- The fix keeps ONE previous token alive across a single rotation, so two
-- concurrent resumes both leave their caller holding something that works.
--
-- What is deliberately NOT weakened:
--
--   * the token is still server-minted from `secrets`, not client-supplied. The
--     alternative design -- a client-originated access credential -- would make
--     the thing that authorises decisioning depend on browser entropy, which is
--     a real weakening and not one this defect justifies;
--   * single use is unchanged. `access_token_consumed_at` stays a single column
--     covering BOTH slots, so consuming either one kills both. A decision can
--     still be authorised exactly once;
--   * the previous slot carries its own expiry, so it dies on schedule rather
--     than inheriting the new token's fresh window.
--
-- The bound is one rotation, and that is the honest limit: three overlapping
-- resumes still strand the oldest. Two is the case that actually occurs -- a
-- double-submitted browser -- and an unbounded chain of live tokens would be a
-- worse security position than the bug.

ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS prev_access_token_hash       TEXT,
    ADD COLUMN IF NOT EXISTS prev_access_token_expires_at TIMESTAMPTZ;

COMMENT ON COLUMN applications.prev_access_token_hash IS
    'The access token displaced by the most recent resume rotation. Accepted by '
    'verify_access_token until it expires, so two overlapping resumes both leave '
    'a usable credential. Killed by access_token_consumed_at along with the '
    'current slot -- single use covers both.';
