-- 0047: manual DTI as EVIDENCE, in a place of its own.
--
-- WHY THIS EXISTS
--
-- `docs/DEBT.md` RF-25. The client answered on 2026-08-29: staff may apply DTI
-- manually, but only on a REFERRED application, only as an underwriter or admin,
-- and only from approved SYNTHETIC source documents. The evidence required is
-- gross monthly income, monthly debt obligations, source-document references, the
-- calculation, staff identity, role, timestamp and reason -- with a bare
-- percentage explicitly insufficient.
--
-- And the constraint that governs the whole design: **a manual DTI is
-- human-review EVIDENCE and must not approve, deny, override, mutate a decision
-- or trigger model output.**
--
-- WHY NOT `manual_reviews`
--
-- `manual_reviews` is the record of a DECISION -- it carries `outcome` and is
-- UNIQUE on `app_id`, one row per application. Putting DTI evidence in its
-- free-text `reason` would (a) make evidence indistinguishable from the decision
-- rationale, (b) allow only one assessment ever, and (c) put regulated numbers in
-- prose that nothing can validate or recompute. RF-25 names that column as the
-- thing not to use.
--
-- Its `reviewer_name` is also a nullable free-text column, which is weaker than
-- this repository's own standard: `pending_movements.requested_by`/`resolved_by`
-- are integer user ids the database enforces. This table follows the stronger
-- pattern.
--
-- THE THREE TABLES
--
--   manual_dti_source_documents      the approved SYNTHETIC registry
--   manual_dti_assessments           one append-only evidence record
--   manual_dti_assessment_documents  which documents an assessment rests on
--
-- No document CONTENT is stored and none is ingested. There is no OCR, no
-- extraction, no embedding, no external call and no file storage anywhere in this
-- design: a source document here is a REFERENCE to an approved synthetic fixture,
-- which is exactly the scope the client authorised for the demo.
BEGIN;

-- ---------------------------------------------------------------------------
-- The approved synthetic document registry.
-- ---------------------------------------------------------------------------
--
-- Built here rather than reused from `fixtures/offline_fairness_training/` on
-- purpose. That package is a vendor-governance / adverse-action artifact -- its
-- `sources/source-ledger.csv` is a bibliography of eCFR and CFPB citations, not
-- applicant income documents -- and its own README says it stands in for reason
-- taxonomies and fairness summaries. Pressing it into service as a DTI document
-- registry would misappropriate a client artifact scoped to something else, which
-- is worse than a small purpose-built registry that says what it is.
CREATE TABLE IF NOT EXISTS manual_dti_source_documents (
    id          SERIAL PRIMARY KEY,

    -- What staff cite. Stable and unique, so an assessment references a document
    -- rather than describing one.
    doc_ref     TEXT        NOT NULL UNIQUE,
    kind        TEXT        NOT NULL CHECK (kind IN
                    ('paystub','bank_statement','tax_return',
                     'employer_letter','debt_schedule')),
    label       TEXT        NOT NULL CHECK (length(regexp_replace(label, '\s', '', 'g')) > 0),

    -- SYNTHETIC ONLY, and the CHECK makes it unfalsifiable rather than a default
    -- somebody can override. The client authorised approved synthetic documents;
    -- a row claiming to be a real applicant's paystub cannot be written at all.
    is_synthetic BOOLEAN    NOT NULL DEFAULT TRUE CHECK (is_synthetic),

    -- Approval is explicit and defaults to FALSE. "Present in the registry" is
    -- not "approved for use" -- an unapproved row is exactly what the negative
    -- tests need to exist in order to be refused.
    approved    BOOLEAN     NOT NULL DEFAULT FALSE,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- The evidence record.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manual_dti_assessments (
    id          BIGSERIAL   PRIMARY KEY,

    -- NOT unique on app_id, unlike `manual_reviews`. A referred application may
    -- be assessed more than once -- a second reviewer, or the same one after new
    -- documents -- and the register is append-only, so a later assessment is an
    -- additional row rather than an edit of the first.
    app_id      INTEGER     NOT NULL REFERENCES applications(id),

    -- WHO, as an enforced reference rather than free text. This is the half of
    -- RF-25 that `manual_reviews.reviewer_name` gets wrong.
    assessed_by INTEGER     NOT NULL REFERENCES users(id),

    -- The role AS EXERCISED, recorded here rather than read back from `users`
    -- later: a person's role can change, and the evidence must say what authority
    -- was used at the time. Restricted to the two the client authorised, so a CSR
    -- assessment cannot be stored even if some future route forgets to check.
    assessed_role TEXT      NOT NULL CHECK (assessed_role IN ('underwriter','admin')),

    -- The two inputs. NUMERIC, never float (D12): these are regulated figures and
    -- the calculation below is asserted against them.
    gross_monthly_income NUMERIC(14,2) NOT NULL CHECK (gross_monthly_income > 0),
    monthly_debt_obligations NUMERIC(14,2) NOT NULL CHECK (monthly_debt_obligations >= 0),

    -- The calculation, in basis points, stored so the evidence carries the figure
    -- a reviewer actually relied on.
    dti_bp      INTEGER     NOT NULL CHECK (dti_bp >= 0),

    -- `btrim` with no second argument strips SPACES ONLY, so a tab-only reason
    -- satisfied the first version of this constraint. Caught by the blank-reason
    -- case parametrised over a tab. The character set is explicit here.
    reason      TEXT        NOT NULL CHECK (length(regexp_replace(reason, '\s', '', 'g')) > 0),
    assessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- REPRODUCIBILITY, ENFORCED BY THE DATABASE.
    --
    -- This is the constraint that makes "a bare percentage is not enough" true in
    -- the schema rather than only in a route. The stored ratio must be exactly
    -- what the two stored inputs produce, so a caller cannot supply a DTI that
    -- does not follow from its own evidence -- and a reader can recompute it from
    -- the row without trusting whoever wrote it.
    --
    -- Same-row and deterministic, which is all a CHECK may be. Rounding is
    -- half-up via `round()` on NUMERIC, applied once, here -- so there is exactly
    -- one definition of the figure and no second one in application code.
    CONSTRAINT manual_dti_is_reproducible CHECK (
        dti_bp = round(monthly_debt_obligations * 10000 / gross_monthly_income)
    )
);

CREATE INDEX IF NOT EXISTS idx_manual_dti_app_id
    ON manual_dti_assessments (app_id, assessed_at DESC);

-- ---------------------------------------------------------------------------
-- Which documents an assessment rests on.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manual_dti_assessment_documents (
    assessment_id BIGINT NOT NULL
        REFERENCES manual_dti_assessments(id) ON DELETE RESTRICT,
    document_id   INTEGER NOT NULL
        REFERENCES manual_dti_source_documents(id) ON DELETE RESTRICT,
    PRIMARY KEY (assessment_id, document_id)
);

-- ---------------------------------------------------------------------------
-- Append-only.
-- ---------------------------------------------------------------------------
--
-- Same shape as `ledger_entries_are_immutable`. Evidence that can be edited is
-- not evidence: the point of recording who assessed what, on which documents, at
-- what time, is that the record cannot later be made to say something else.
CREATE OR REPLACE FUNCTION manual_dti_assessments_are_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'manual_dti_assessments is append-only (attempted % on id %)',
                    TG_OP, COALESCE(OLD.id, NEW.id);
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS manual_dti_assessments_immutable ON manual_dti_assessments;
CREATE TRIGGER manual_dti_assessments_immutable
    BEFORE UPDATE OR DELETE ON manual_dti_assessments
    FOR EACH ROW EXECUTE FUNCTION manual_dti_assessments_are_immutable();

-- The document links are part of the evidence, so they are append-only too.
-- Detaching a document afterwards would change what an assessment claims to rest
-- on while leaving its ratio and reason untouched.
CREATE OR REPLACE FUNCTION manual_dti_documents_are_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'manual_dti_assessment_documents is append-only (attempted % on assessment %)',
        TG_OP, COALESCE(OLD.assessment_id, NEW.assessment_id);
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS manual_dti_documents_immutable ON manual_dti_assessment_documents;
CREATE TRIGGER manual_dti_documents_immutable
    BEFORE UPDATE OR DELETE ON manual_dti_assessment_documents
    FOR EACH ROW EXECUTE FUNCTION manual_dti_documents_are_immutable();

-- ---------------------------------------------------------------------------
-- Only an APPROVED SYNTHETIC document may be cited.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION manual_dti_document_is_approved() RETURNS trigger AS $$
DECLARE
    doc RECORD;
BEGIN
    SELECT doc_ref, approved, is_synthetic INTO doc
      FROM manual_dti_source_documents WHERE id = NEW.document_id;

    IF doc IS NULL THEN
        RAISE EXCEPTION 'source document % is not in the registry', NEW.document_id;
    END IF;
    IF NOT doc.approved THEN
        RAISE EXCEPTION
            'source document % (%) is not approved for manual DTI evidence',
            NEW.document_id, doc.doc_ref;
    END IF;
    -- Unreachable while the CHECK above holds. Asserted anyway, because "the
    -- other constraint makes this impossible" is how an unenforced rule survives
    -- a later migration that relaxes the other constraint.
    IF NOT doc.is_synthetic THEN
        RAISE EXCEPTION 'source document % is not synthetic', NEW.document_id;
    END IF;

    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS manual_dti_document_approved ON manual_dti_assessment_documents;
CREATE TRIGGER manual_dti_document_approved
    BEFORE INSERT ON manual_dti_assessment_documents
    FOR EACH ROW EXECUTE FUNCTION manual_dti_document_is_approved();

-- ---------------------------------------------------------------------------
-- At least one document, checked at COMMIT.
-- ---------------------------------------------------------------------------
--
-- "Source-document references" is required evidence, and a bare percentage is
-- explicitly insufficient -- so an assessment with no document attached must not
-- be able to exist. It cannot be a plain CHECK: the links are written after the
-- assessment row, in the same transaction.
--
-- A DEFERRABLE INITIALLY DEFERRED constraint trigger is the mechanism that fits:
-- it fires at commit, by which time the links are present, so a route may insert
-- the row and its documents in either order and still be refused if it forgets
-- the documents entirely.
CREATE OR REPLACE FUNCTION manual_dti_has_a_source_document() RETURNS trigger AS $$
DECLARE
    n INTEGER;
BEGIN
    SELECT count(*) INTO n
      FROM manual_dti_assessment_documents WHERE assessment_id = NEW.id;
    IF n = 0 THEN
        RAISE EXCEPTION
            'manual DTI assessment % cites no source document; a ratio with no '
            'evidence behind it is exactly what RF-25 refuses', NEW.id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS manual_dti_needs_a_document ON manual_dti_assessments;
CREATE CONSTRAINT TRIGGER manual_dti_needs_a_document
    AFTER INSERT ON manual_dti_assessments
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION manual_dti_has_a_source_document();

-- ---------------------------------------------------------------------------
-- A small approved synthetic registry for the demo.
-- ---------------------------------------------------------------------------
--
-- Five approved rows and one deliberately UNAPPROVED row. The unapproved one is
-- not an oversight: the client's rule is "approved synthetic documents only", and
-- a registry where everything is approved cannot demonstrate the refusal.
INSERT INTO manual_dti_source_documents (doc_ref, kind, label, approved) VALUES
    ('SYN-PAYSTUB-001',  'paystub',         'Synthetic paystub, monthly, employer A', TRUE),
    ('SYN-PAYSTUB-002',  'paystub',         'Synthetic paystub, semi-monthly, employer B', TRUE),
    ('SYN-BANK-001',     'bank_statement',  'Synthetic bank statement, 3 months', TRUE),
    ('SYN-TAX-001',      'tax_return',      'Synthetic prior-year tax return', TRUE),
    ('SYN-DEBTSCH-001',  'debt_schedule',   'Synthetic monthly obligations schedule', TRUE),
    ('SYN-DRAFT-001',    'employer_letter', 'Synthetic employer letter -- DRAFT, not approved', FALSE)
ON CONFLICT (doc_ref) DO NOTHING;

COMMIT;
