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
    -- Contractual payment schedule, persisted as fact (db/migrations/0030).
    -- Model B: regular periods bill `monthly_payment`; the final period bills
    -- `final_payment`, which absorbs the cent residue and cannot be recovered
    -- from any other stored figure. The read path used to regenerate the
    -- schedule with whatever generator was deployed, so an accepted disclosure
    -- was not a stored fact. NULL on legacy rows = "not recorded"; boarding
    -- refuses those rather than inventing terms.
    regular_payment_count INTEGER,
    final_payment    NUMERIC(14,2),
    term_months      INTEGER,
    schedule_version TEXT,              -- 'B1' = cent-rounded level + final adjustment
    -- The principal the schedule was calculated on. Stored because it cannot be
    -- recovered: amount_financed is cent-rounded, so inverting it through the
    -- fee lands on a DIFFERENT principal and regenerates a schedule whose final
    -- row contradicts the disclosure above it (db/migrations/0030).
    principal        NUMERIC(14,2),
    -- The contractual rate the payment schedule was calculated on. Distinct
    -- from `apr` below, which is the disclosed all-in rate and is higher
    -- whenever a prepaid fee exists. Boarding reads THIS one; servicing
    -- amortizes what boarding gives it (db/migrations/0030).
    note_rate_pct NUMERIC(7,3),
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
    ),
    -- Model B schedule integrity (db/migrations/0030). Mirrored here so both
    -- provisioning paths enforce the same rules -- test_migration_paths_converge
    -- compares CHECK constraints by name and normalized expression, so a rule
    -- present on one path and absent on the other fails the build.
    --
    -- The application checks these too; that is not a substitute. Seed SQL, the
    -- repair path and any operator with psql all write this table, and a
    -- half-written schedule is worse than an absent one: it reads as "recorded"
    -- to a single-column NULL check while describing nothing billable.
    CONSTRAINT offers_schedule_all_or_nothing CHECK (
        (regular_payment_count IS NULL
         AND final_payment      IS NULL
         AND term_months        IS NULL
         AND schedule_version   IS NULL)
        OR
        (regular_payment_count IS NOT NULL
         AND final_payment      IS NOT NULL
         AND term_months        IS NOT NULL
         AND schedule_version   IS NOT NULL)
    ),
    -- An identity of Model B, not a policy: term_months - 1 regular payments
    -- plus one adjusted final payment. Also the exact corruption a mismatched
    -- request body used to produce -- a 36-month schedule filed as 60 months.
    CONSTRAINT offers_schedule_term_agrees CHECK (
        term_months IS NULL OR regular_payment_count + 1 = term_months
    ),
    -- Zero regular payments is correct and reachable: a single-payment loan is
    -- all final payment.
    CONSTRAINT offers_schedule_shape_sane CHECK (
        (term_months IS NULL OR term_months >= 1)
        AND (regular_payment_count IS NULL OR regular_payment_count >= 0)
    ),
    CONSTRAINT offers_final_payment_positive CHECK (
        final_payment IS NULL OR final_payment > 0
    ),
    -- An unknown version is not forward compatibility; it is a row whose
    -- amounts were produced by rounding rules the reader does not have.
    CONSTRAINT offers_schedule_version_supported CHECK (
        schedule_version IS NULL OR schedule_version IN ('B1')
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
    -- The contract as boarded (db/migrations/0030). Servicing bills THESE
    -- amounts; recomputing them from principal/rate/term is what drifts.
    regular_payment       NUMERIC(14,2),
    regular_payment_count INTEGER,
    final_payment         NUMERIC(14,2),
    schedule_version      TEXT,
    term_months     INTEGER NOT NULL,
    status          TEXT DEFAULT 'current',
    opened_at       TIMESTAMPTZ DEFAULT now(),
    -- Model B schedule integrity on the boarded contract (db/migrations/0030).
    -- No term_months in the group: loans.term_months already exists and is NOT
    -- NULL, so the count is reconciled against the loan's own term instead.
    CONSTRAINT loans_schedule_all_or_nothing CHECK (
        (regular_payment       IS NULL
         AND regular_payment_count IS NULL
         AND final_payment     IS NULL
         AND schedule_version  IS NULL)
        OR
        (regular_payment       IS NOT NULL
         AND regular_payment_count IS NOT NULL
         AND final_payment     IS NOT NULL
         AND schedule_version  IS NOT NULL)
    ),
    CONSTRAINT loans_schedule_term_agrees CHECK (
        regular_payment_count IS NULL OR regular_payment_count + 1 = term_months
    ),
    CONSTRAINT loans_schedule_amounts_positive CHECK (
        (regular_payment IS NULL OR regular_payment > 0)
        AND (final_payment IS NULL OR final_payment > 0)
        AND (regular_payment_count IS NULL OR regular_payment_count >= 0)
    ),
    CONSTRAINT loans_schedule_version_supported CHECK (
        schedule_version IS NULL OR schedule_version IN ('B1')
    )
);

-- Mutable balance: one column, overwritten in place. No ledger, no transaction history.
CREATE TABLE IF NOT EXISTS balances (
    loan_id     INTEGER PRIMARY KEY REFERENCES loans(id),
    balance     NUMERIC(14,2) NOT NULL,    -- D12: was DOUBLE PRECISION, UPDATE-d in place
    past_due    NUMERIC(14,2) DEFAULT 0,   -- D12: was DOUBLE PRECISION
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Payments. Card capture is tokenized in the browser, so no code path writes a
-- PAN or a CVV any more (ADR 0008, supersedes ADR 0003) -- new rows carry only
-- last4/brand from the processor's token response.
--
-- pan/cvv still exist here on purpose, for one release. Removing them from a
-- fresh install while db/migrations has only reached the EXPAND step (0029,
-- back-fill last4) would make a fresh database and a migrated one disagree,
-- and db/tests/test_migration_paths_converge.py compares exactly that. They go
-- in the CONTRACT step, db/migrations/0031, and come out of this file in the
-- same release.
CREATE TABLE IF NOT EXISTS payments (
    id          SERIAL PRIMARY KEY,
    loan_id     INTEGER REFERENCES loans(id),
    -- Legacy, write-path-dead, dropped by db/migrations/0031. No application
    -- code reads or writes either column as of this release.
    pan         TEXT,
    cvv         TEXT,
    last4       TEXT,                 -- display only; never enough to reconstruct a PAN
    brand       TEXT,                 -- e.g. "visa", "mastercard" -- display only
    amount      NUMERIC(14,2) NOT NULL,  -- D12: was DOUBLE PRECISION
    method      TEXT DEFAULT 'card',
    -- Review fix: charge() used to treat a processor_token as proof of a real
    -- charge without ever calling a processor -- 'pending' is written first
    -- (before authorization is confirmed), then flipped to 'captured' or
    -- 'failed' once services/payment-service/app/processor.py::authorize_
    -- charge() actually returns. A row stuck at 'pending' means the process
    -- died mid-authorization, not that anything was approved. Historical
    -- rows (before this column existed) default to 'captured' -- they really
    -- were, just without a formal record of it.
    auth_status TEXT NOT NULL DEFAULT 'captured',
    -- Review fix (db/migrations/0019): the processor's own authorization id,
    -- persisted in the SAME UPDATE that flips auth_status to 'captured' --
    -- a pending retry asks the processor for this via get_authorization()
    -- before ever calling authorize_charge() again, instead of blindly
    -- re-charging. See services/payment-service/app/payments.py.
    authorization_id TEXT,
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
    -- db/migrations/0028: a captured-but-unapplied row is a durable work item
    -- the reconciler drains (payment-service/app/reconcile.py). Nothing used
    -- to look for these at all, so a borrower who closed the tab left money
    -- captured and the balance uncredited, permanently. apply_next_attempt_at
    -- doubles as the claim marker: a worker claims a row by pushing it into
    -- the future in the same statement that selects it, so two replicas can
    -- never work the same payment at once. apply_last_error holds the
    -- exception TYPE only -- never a message, which can embed request values.
    apply_attempts INTEGER NOT NULL DEFAULT 0,
    apply_next_attempt_at TIMESTAMPTZ,
    apply_last_error TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS payments_idempotency_key_key
    ON payments (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payments_unapplied
    ON payments (apply_next_attempt_at)
    WHERE auth_status = 'captured' AND applied_at IS NULL;

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
