-- 0038 -- bind a keyed retry to the payload that created it.
--
-- The idempotency key says WHICH application a retry belongs to and the resume
-- token says the caller may recover it. Neither says the retry is the SAME
-- request, and it was being assumed.
--
-- The gap that opened: the browser deliberately keeps the retry credentials
-- after a failed submission so the borrower can correct a mistake and try
-- again. A borrower who fixes a mistyped SSN, address or income and resubmits
-- was handed back the ORIGINAL stored applicant, and KYC and decisioning then
-- ran against the data they had just corrected. That is an identity and
-- underwriting-input integrity failure, not an idempotency nicety -- the
-- corrected value is visible in the request and absent from the decision.
--
-- The fingerprint is a sha256 over the canonical identity and underwriting
-- fields. On a retry the server compares it and refuses a mismatch with 409
-- rather than silently preferring the stored copy. It is NOT a substitute for
-- the resume token: the token authorises recovery, the fingerprint proves the
-- retry is the same request.
--
-- Nullable, because rows written before this migration have no fingerprint and
-- must stay resumable -- an upgrade must not strand an in-flight application.
-- A NULL is treated as "cannot verify, so accept", which is exactly the
-- pre-migration behaviour and no worse than it. New rows always carry one.

ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS request_fingerprint TEXT;

COMMENT ON COLUMN applications.request_fingerprint IS
    'sha256 of the canonical identity + underwriting payload that created this '
    'application. A retry presenting the same idempotency key and resume token '
    'but a different fingerprint is rejected with 409 rather than being served '
    'the stored data. NULL on rows created before migration 0038.';
