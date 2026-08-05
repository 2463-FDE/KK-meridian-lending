-- 0024 -- PR #6 review, Gap A: make the credit-bureau pull idempotent across
-- an ambiguous-timeout retry.
--
-- Origination cannot distinguish "the bureau never ran" from "the bureau ran
-- and we lost the response" when its own HTTP client times out. Its only safe
-- move is to release the attempt and let the borrower retry -- but with no
-- idempotency key at the bureau boundary that retry started a SECOND,
-- independently-billed hard credit inquiry against a real applicant.
--
-- bureau_request_key is origination's stable idempotency key for one LOGICAL
-- decision request. start_decision_attempt reuses it when the immediately
-- preceding attempt for this application ended ambiguously (state='failed',
-- failure_code='timeout') and mints a fresh one otherwise -- so a retry
-- collapses onto the original bureau operation, while a genuinely new
-- decision request (a staff rerun, say) always performs a real new pull and
-- can never be served stale credit data.
--
-- bureau_reference_id is the provider's own non-sensitive handle for the
-- operation, recorded so a future real-provider implementation can look it up
-- by reference instead of re-pulling. Deliberately NOT the SSN and NOT the raw
-- provider response -- neither is ever persisted here.
--
-- Both columns are nullable: attempts created before this migration have no
-- key, and the append-only decision_events rows they produced are untouched.

ALTER TABLE decision_attempts
    ADD COLUMN IF NOT EXISTS bureau_request_key TEXT,
    ADD COLUMN IF NOT EXISTS bureau_reference_id TEXT;

-- Recovering the key for a retry looks up the most recent attempt for one
-- application; this index keeps that a point lookup rather than a scan as
-- the attempt history grows.
CREATE INDEX IF NOT EXISTS idx_decision_attempts_app_id_id
    ON decision_attempts (app_id, id DESC);
