-- 0032 -- tie a CIP result to the application it was run for.
--
-- kyc_checks recorded applicant_id and nothing else, so "has this application
-- been identity-verified?" was not a question the schema could answer. The
-- closest available answer was "has this APPLICANT ever been verified?", and a
-- gate built on it passes a repeat applicant whose current application's KYC
-- call failed or never ran -- while the logs record that intake was blocked.
-- The application reaches underwriting on evidence belonging to a different
-- application (PR #18 review).
--
-- That is a compliance gap rather than a tidiness one: the evidence a regulator
-- would ask for is "the CIP result for THIS application", and it did not exist.
--
-- Nullable, because rows written before this migration genuinely do not know
-- which application they belonged to. Inventing that linkage would be worse than
-- admitting it: a fabricated application_id is indistinguishable from a real one
-- and would be exactly the forged CIP evidence the rest of PR #18 exists to
-- prevent. The back-fill below therefore fills in only the unambiguous case and
-- leaves the rest NULL.

ALTER TABLE kyc_checks ADD COLUMN IF NOT EXISTS application_id INTEGER REFERENCES applications(id);

-- Back-fill ONLY where the applicant has exactly one application, so the link is
-- a fact rather than a guess. An applicant with two applications and one CIP row
-- is genuinely ambiguous -- there is no evidence saying which one it was run for
-- -- and those rows stay NULL.
UPDATE kyc_checks k
   SET application_id = a.id
  FROM applications a
 WHERE k.application_id IS NULL
   AND a.applicant_id = k.applicant_id
   AND (SELECT count(*) FROM applications a2 WHERE a2.applicant_id = k.applicant_id) = 1;

CREATE INDEX IF NOT EXISTS idx_kyc_checks_application_id ON kyc_checks(application_id);

-- Deliberately NOT NOT-NULL. Making it required would force the ambiguous
-- historical rows to be deleted or given an invented value, and both destroy
-- information: the honest state of those rows is "a CIP result exists for this
-- applicant, and which application it belonged to was never recorded".
--
-- The decision gate reads application_id and refuses when no row matches, so a
-- NULL row simply does not satisfy it. A rerun of an old application whose
-- linkage is ambiguous will be refused -- correct, because we cannot show CIP
-- ran for it, and refusing is the direction that does not underwrite someone on
-- unproven identity evidence.
DO $$
DECLARE
    linked   INTEGER;
    unlinked INTEGER;
BEGIN
    SELECT count(*) INTO linked   FROM kyc_checks WHERE application_id IS NOT NULL;
    SELECT count(*) INTO unlinked FROM kyc_checks WHERE application_id IS NULL;
    RAISE NOTICE '0032: kyc_checks linked to an application: %, left ambiguous: %',
                 linked, unlinked;
END $$;
