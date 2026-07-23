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
    created_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
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
    app_id      INTEGER REFERENCES applications(id),
    decision_id INTEGER REFERENCES decisions(app_id) UNIQUE,  -- W4: which decision this offer came from; UNIQUE makes offer creation idempotent per decision
    fee_pct_used NUMERIC(5,4),          -- W4: snapshot of ORIGINATION_FEE_PCT used at creation time
    apr         NUMERIC(7,3),           -- D12: was DOUBLE PRECISION
    finance_charge NUMERIC(14,2),       -- D12: was DOUBLE PRECISION
    monthly_payment NUMERIC(14,2),      -- D12: was DOUBLE PRECISION
    amount_financed NUMERIC(14,2),      -- D12: was DOUBLE PRECISION
    total_of_payments NUMERIC(14,2),    -- D12: was DOUBLE PRECISION
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- LSS tables. A funded loan is "boarded" here by a direct insert from origination.
CREATE TABLE IF NOT EXISTS loans (
    id              SERIAL PRIMARY KEY,
    app_id          INTEGER,
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

-- Payments: stores full PAN + CVV. No idempotency key. No unique charge reference.
CREATE TABLE IF NOT EXISTS payments (
    id          SERIAL PRIMARY KEY,
    loan_id     INTEGER REFERENCES loans(id),
    pan         TEXT,                 -- full PAN stored
    cvv         TEXT,                 -- CVV stored (SAD — flat PCI prohibition)
    amount      NUMERIC(14,2) NOT NULL,  -- D12: was DOUBLE PRECISION
    method      TEXT DEFAULT 'card',
    created_at  TIMESTAMPTZ DEFAULT now()
    -- no idempotency_key, no unique(charge_ref)
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

-- A few indexes added over time for the servicing dashboard. (No idempotency index on
-- payments — there is no idempotency key to index. No reason/driver columns on decisions.)
CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status);
CREATE INDEX IF NOT EXISTS idx_payments_loan ON payments(loan_id);
CREATE INDEX IF NOT EXISTS idx_offers_app ON offers(app_id);
