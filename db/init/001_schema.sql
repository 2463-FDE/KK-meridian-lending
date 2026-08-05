-- Meridian Lending — schema (Halcyon v1, extended in-place over the years)
-- D12 fix: money columns are NUMERIC now, not DOUBLE PRECISION -- see
-- db/migrations/0005_money_columns_to_numeric.sql for the ALTER TABLE path on an
-- existing deployment (this file only runs automatically on a fresh volume).
-- Dollar-amount columns: NUMERIC(14,2). Percentage/rate columns (apr): NUMERIC(7,3),
-- matching the app's own round(apr, 3) convention.

-- Staff + borrower logins. Passwords are sha256 hex (no salt, no bcrypt — Halcyon's
-- "we'll harden it later"). Roles: admin | underwriter | csr | borrower.
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,        -- sha256(password), unsalted
    role          TEXT NOT NULL DEFAULT 'csr',
    display_name  TEXT,
    applicant_id  INTEGER,              -- set for borrower logins
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS applicants (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    dob         DATE,
    ssn         TEXT,            -- plaintext
    ein         TEXT,            -- for entity applicants
    is_entity   BOOLEAN DEFAULT FALSE,
    email       TEXT,
    phone       TEXT,
    address     TEXT,
    zip_code    TEXT,             -- W8: fair-lending ZIP-level check needs this; address alone is free text
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS applications (
    id                SERIAL PRIMARY KEY,
    applicant_id      INTEGER REFERENCES applicants(id),
    amount            NUMERIC(14,2) NOT NULL,      -- D12: was DOUBLE PRECISION
    term_months       INTEGER NOT NULL,
    purpose           TEXT,
    income            NUMERIC(14,2),                -- D12: was DOUBLE PRECISION
    employer          TEXT,
    job_title         TEXT,
    employment_years  DOUBLE PRECISION,            -- a duration, not money -- left as-is
    status            TEXT DEFAULT 'submitted',
    -- Minted once at submission (intake.create_application) and returned to
    -- the caller -- proves ownership for the FIRST decision call (see
    -- routers/applications.py run_decision), since app_id alone is a guessable
    -- integer and the borrower has no account yet at this point.
    --
    -- Security fix (db/migrations/0025, backported here so a fresh volume never
    -- recreates the vulnerability): this used to be a plaintext `access_token
    -- TEXT` column that never expired and was never consumed -- the same
    -- bearer-credential-at-rest problem 0022 fixed for the acceptance token.
    -- Only the sha256 hash is stored now, with a server-clock (Postgres now())
    -- expiry and a single-use consumed marker stamped by the decision that
    -- used it.
    access_token_hash          TEXT,
    access_token_expires_at    TIMESTAMPTZ,
    access_token_consumed_at   TIMESTAMPTZ,
    -- Review fix: one-time token minted onto the application when it's
    -- approved (run_decision), required to accept it anonymously (the
    -- no-account borrower flow) since app_id is a sequential, guessable
    -- integer. NULL means "no token issued or already spent" -- never
    -- valid to accept with. See routers/applications.py accept_offer.
    --
    -- Security fix (db/migrations/0022, backported here so a fresh volume
    -- never recreates the vulnerability): the raw token used to be stored
    -- here in plain text with no expiry -- a plaintext bearer credential
    -- at rest is itself a live secret. Only its sha256 hash is stored now,
    -- alongside a server-clock (Postgres now()) expiry and a single-use
    -- consumed marker. No production environment exists for this
    -- application, so there is no plaintext value here to "migrate" --
    -- this fresh-init path and 0022's upgrade path for an EXISTING
    -- database both converge on this exact same shape.
    accept_token_hash          TEXT,
    accept_token_expires_at    TIMESTAMPTZ,
    accept_token_consumed_at   TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_accept_token_hash
    ON applications (accept_token_hash)
    WHERE accept_token_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_applications_access_token_hash
    ON applications (access_token_hash)
    WHERE access_token_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_applications_applicant ON applications(applicant_id);

-- KYC: CIP only. No sanctions/OFAC, no beneficial owner, no monitoring.
CREATE TABLE IF NOT EXISTS kyc_checks (
    id              SERIAL PRIMARY KEY,
    applicant_id    INTEGER REFERENCES applicants(id),
    name_verified   BOOLEAN,
    dob_verified    BOOLEAN,
    address_verified BOOLEAN,
    ssn_verified    BOOLEAN,
    -- no sanctions_screened, no ubo_identified, no ongoing_monitoring columns
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Decision: OUTCOME ONLY. No reason, no model drivers, no inputs, no timestamp of model run.
CREATE TABLE IF NOT EXISTS decisions (
    app_id      INTEGER PRIMARY KEY REFERENCES applications(id),
    outcome     TEXT NOT NULL   -- 'approve' | 'deny' | 'refer' | 'counteroffer'
);

CREATE TABLE IF NOT EXISTS offers (
    id          SERIAL PRIMARY KEY,
    -- Review fix: UNIQUE here (not just on decision_id below) is the real
    -- "one canonical offer per application" guarantee -- app_id is populated
    -- on every offer row, unlike the nullable decision_id, whose own UNIQUE
    -- constraint silently allows unlimited NULL rows to coexist.
    app_id      INTEGER REFERENCES applications(id) UNIQUE,
    decision_id INTEGER REFERENCES decisions(app_id) UNIQUE,  -- W4: which decision this offer came from; UNIQUE makes offer creation idempotent per decision
    fee_pct_used NUMERIC(5,4),          -- W4: snapshot of ORIGINATION_FEE_PCT used at creation time
    apr         NUMERIC(7,3),           -- D12: was DOUBLE PRECISION
    finance_charge NUMERIC(14,2),       -- D12: was DOUBLE PRECISION
    monthly_payment NUMERIC(14,2),      -- D12: was DOUBLE PRECISION
    amount_financed NUMERIC(14,2),      -- D12: was DOUBLE PRECISION
    total_of_payments NUMERIC(14,2),    -- D12: was DOUBLE PRECISION
    created_at  TIMESTAMPTZ DEFAULT now(),
    -- Review fix (db/migrations/0021): OFFER_ACCEPTED is a real workflow
    -- state, not just implied by BOARDED -- stamped the moment accept_offer
    -- boards the loan (same atomic action in this system today).
    accepted_at TIMESTAMPTZ,
    -- Gap F (db/migrations/0026, backported here): the five canonical TILA
    -- amounts above must all be present or the row is not a disclosure. Read
    -- paths used to substitute defaults for a NULL (`apr or 7.99`), turning a
    -- corrupt row into a real-looking offer a borrower could accept. On a
    -- fresh volume there is no historical damage to tolerate, so this is a
    -- plain (already-valid) CHECK; 0026 adds it NOT VALID on an existing
    -- database so the operator can see and remediate offending rows first.
    CONSTRAINT offers_canonical_terms_present CHECK (
        apr IS NOT NULL
        AND finance_charge IS NOT NULL
        AND monthly_payment IS NOT NULL
        AND amount_financed IS NOT NULL
        AND total_of_payments IS NOT NULL
    )
);

-- LSS tables. A funded loan is "boarded" here by a direct insert from origination.
CREATE TABLE IF NOT EXISTS loans (
    id              SERIAL PRIMARY KEY,
    -- Review fix: UNIQUE -- one canonical loan per application, no matter
    -- what code path inserts here. Closes a race where two concurrent
    -- accept_offer calls on the same not-yet-funded application both used
    -- to pass the (stale-read) status check and both board a loan.
    app_id          INTEGER UNIQUE,
    applicant_name  TEXT,
    principal       NUMERIC(14,2) NOT NULL,   -- D12: was DOUBLE PRECISION
    apr             NUMERIC(7,3) NOT NULL,     -- D12: was DOUBLE PRECISION
    term_months     INTEGER NOT NULL,
    status          TEXT DEFAULT 'current',
    opened_at       TIMESTAMPTZ DEFAULT now()
);

-- Mutable balance: one column, overwritten in place. No ledger, no transaction history.
CREATE TABLE IF NOT EXISTS balances (
    loan_id     INTEGER PRIMARY KEY REFERENCES loans(id),
    balance     NUMERIC(14,2) NOT NULL,    -- D12: was DOUBLE PRECISION, UPDATE-d in place
    past_due    NUMERIC(14,2) DEFAULT 0,   -- D12: was DOUBLE PRECISION
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Payments: stores full PAN + CVV (still open, PCI debt -- unrelated to the fix below).
CREATE TABLE IF NOT EXISTS payments (
    id          SERIAL PRIMARY KEY,
    loan_id     INTEGER REFERENCES loans(id),
    pan         TEXT,                 -- full PAN stored
    cvv         TEXT,                 -- CVV stored (SAD — flat PCI prohibition)
    amount      NUMERIC(14,2) NOT NULL,  -- D12: was DOUBLE PRECISION
    method      TEXT DEFAULT 'card',
    -- Review fix: a timeout retry or a double-click on submit used to insert a
    -- second row and apply the balance twice (no idempotency key at all).
    -- Caller-supplied; NULL only for pre-fix legacy rows, which the partial
    -- unique index below deliberately excludes (see db/migrations/0007).
    idempotency_key TEXT,
    -- Review fix: NULL means captured but not yet applied to the loan balance
    -- (a pending/outbox record) -- set once servicing-service confirms the
    -- apply succeeded. A retry on the same idempotency_key checks this and
    -- retries the apply instead of blindly reporting "captured" again.
    applied_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS payments_idempotency_key_key
    ON payments (idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Review fix: guards servicing-service's apply-payment endpoint against
-- applying the same captured payment twice (a payment-service retry after a
-- lost response, or two requests racing). One row per payment_id that has
-- actually been applied to a balance; the INSERT that creates this row is
-- the atomic idempotency check -- see services/servicing-service/app/balance.py.
CREATE TABLE IF NOT EXISTS payment_applications (
    payment_id  INTEGER PRIMARY KEY,
    loan_id     INTEGER NOT NULL,
    amount      NUMERIC(14,2) NOT NULL,
    applied_at  TIMESTAMPTZ DEFAULT now()
);

-- "audit" log: an ordinary, mutable table. Rows can be UPDATE/DELETE-d. Not append-only.
CREATE TABLE IF NOT EXISTS audit_logs (
    id          SERIAL PRIMARY KEY,
    actor       TEXT,
    action      TEXT,
    detail      TEXT,
    deleted_at  TIMESTAMPTZ,        -- soft-delete column on an "audit" trail
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- A few indexes added over time for the servicing dashboard. (No reason/driver
-- columns on decisions.)
CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status);
CREATE INDEX IF NOT EXISTS idx_payments_loan ON payments(loan_id);
CREATE INDEX IF NOT EXISTS idx_offers_app ON offers(app_id);
