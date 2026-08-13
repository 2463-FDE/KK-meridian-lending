-- 0033 — review fix (PR #18, round 6): record the CIP VERDICT, not only the
-- four factors it was computed from.
--
-- `kyc_checks` stored name/dob/address/ssn_verified and nothing else, so every
-- reader that wanted to know "was this applicant identified?" had to recompute
-- the answer. Origination's decision gate therefore could not ask it at all: it
-- checked that a row EXISTED and advanced, which meant a recorded CIP failure
-- had no consequence anywhere in the system. The comment on that gate said a
-- failed CIP was "a real, recorded outcome the deny path is entitled to act on"
-- -- and no deny path read the column, because there was no column to read.
--
-- The alternative was to have origination apply kyc-service's pass rule itself.
-- That rule is applicant-type aware (an entity clears on name and address; a
-- natural person needs date of birth and SSN too), so a second copy would be a
-- second thing to keep in step, and the copy that drifts is always the one that
-- decides whether an unidentified person gets underwritten.
--
-- Nullable, because rows written before this migration never recorded a verdict.

ALTER TABLE kyc_checks ADD COLUMN IF NOT EXISTS cip_passed BOOLEAN;

-- Back-fill only where the verdict follows from what was actually recorded,
-- using the same applicant-type rule kyc-service applies. Everything else stays
-- NULL: a NULL here means "this row does not say", which is the truth, and the
-- decision gate treats it as not-established rather than guessing.
UPDATE kyc_checks k
   SET cip_passed = CASE
        WHEN COALESCE(a.is_entity, false) THEN
            COALESCE(k.name_verified, false) AND COALESCE(k.address_verified, false)
        ELSE
            COALESCE(k.name_verified, false) AND COALESCE(k.address_verified, false)
            AND COALESCE(k.dob_verified, false) AND COALESCE(k.ssn_verified, false)
       END
  FROM applicants a
 WHERE k.applicant_id = a.id
   AND k.cip_passed IS NULL
   AND k.name_verified IS NOT NULL;

-- Report what the back-fill could and could not settle, the same way 0032 does.
-- A migration that silently leaves NULLs looks identical to one that filled
-- everything.
DO $$
DECLARE
    settled   INTEGER;
    unsettled INTEGER;
BEGIN
    SELECT count(*) INTO settled   FROM kyc_checks WHERE cip_passed IS NOT NULL;
    SELECT count(*) INTO unsettled FROM kyc_checks WHERE cip_passed IS NULL;
    RAISE NOTICE '0033: cip_passed settled for % row(s); % row(s) left NULL '
                 '(no recorded factors, or no applicant row to type them by) '
                 'and will not satisfy the decision gate', settled, unsettled;
END $$;

COMMENT ON COLUMN kyc_checks.cip_passed IS
    'The CIP verdict as kyc-service reached it. NULL means the row predates '
    '0033 and does not say -- treated as not established, never as passed.';
