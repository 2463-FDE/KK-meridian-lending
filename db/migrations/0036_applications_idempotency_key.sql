-- 0036 -- give intake an idempotency key, so a retry after a KYC failure resumes
-- the same application instead of creating a second one.
--
-- The defect: `submit_application` commits the applicant and application rows,
-- then calls kyc-service. On a 401/403/503 it raised a 503 saying "Please retry"
-- and returned no identifier at all -- so the only thing a client could do was
-- POST again, which created a SECOND applicant and a SECOND application and
-- stranded the first as `kyc_unverified` forever. One person, two borrower
-- records, on a system that has to be able to say who applied.
--
-- Why a key rather than rolling the rows back: an application is a record that
-- somebody applied, and Reg B requires retaining application records -- including
-- INCOMPLETE ones -- for about 25 months (policies/underwriting_guidelines.md,
-- Records retention). Deleting them to tidy up a failed KYC call would destroy
-- exactly the evidence the regulation asks us to keep. So the row stays and the
-- retry is made safe instead.
--
-- Same shape as the payments idempotency key (db/migrations/0012): a nullable
-- column with a PARTIAL unique index, so existing rows and callers that send no
-- key are unaffected. Nullable is deliberate -- making it NOT NULL would break
-- every client that has not been updated, on the intake path, which is the worst
-- possible place to require a flag day.

ALTER TABLE applications ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

-- Partial: many rows legitimately have no key, and NULLs do not conflict in
-- Postgres anyway -- the WHERE clause makes that explicit and keeps the index
-- small.
CREATE UNIQUE INDEX IF NOT EXISTS applications_idempotency_key_uniq
    ON applications (idempotency_key) WHERE idempotency_key IS NOT NULL;

COMMENT ON COLUMN applications.idempotency_key IS
    'Client-supplied key making intake safe to retry. A retry with the same key '
    'resumes THIS application rather than creating a second applicant and a '
    'second application -- see origination-service/app/intake.py.';
