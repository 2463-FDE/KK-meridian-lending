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
    created_at        TIMESTAMPTZ DEFAULT now(),
    -- Client-supplied key making intake safe to retry (db/migrations/0036).
    -- Intake commits this row BEFORE calling kyc-service, so a KYC failure used
    -- to leave the caller with a 503 and no identifier -- and a retry created a
    -- second applicant and a second application. A retry with the same key
    -- resumes this one instead.
    idempotency_key TEXT,
    -- Recovery of an INCOMPLETE application needs the key AND this token
    -- (db/migrations/0037). The key identifies which application; the token
    -- authorises the caller. Only the sha256 hash is stored.
    resume_token_hash        TEXT,
    resume_token_expires_at  TIMESTAMPTZ,
    resume_token_consumed_at TIMESTAMPTZ,
    -- sha256 of the canonical identity + underwriting payload that created this
    -- application (db/migrations/0038). The key says WHICH application a retry
    -- belongs to and the token says the caller MAY recover it; neither says the
    -- retry is the SAME request. A retry with matching credentials and a
    -- different fingerprint is refused with 409 rather than being served the
    -- stored data, because the borrower can see the value they corrected and a
    -- decision made against the old one is invisible to them.
    request_fingerprint      TEXT,
    -- The access token displaced by the most recent resume rotation
    -- (db/migrations/0039). Accepted until its own expiry so two overlapping
    -- retries both leave a usable credential; killed by
    -- access_token_consumed_at along with the current slot, so single use
    -- still means single use.
    prev_access_token_hash       TEXT,
    prev_access_token_expires_at TIMESTAMPTZ
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
    -- Which application this CIP result was run for (db/migrations/0032).
    -- Without it the only answerable question was "has this APPLICANT ever been
    -- verified?", which passes a repeat applicant whose current application's
    -- KYC never ran. Nullable: rows predating 0032 genuinely do not know.
    application_id  INTEGER REFERENCES applications(id),
    name_verified   BOOLEAN,
    dob_verified    BOOLEAN,
    address_verified BOOLEAN,
    ssn_verified    BOOLEAN,
    -- The CIP verdict as kyc-service reached it (db/migrations/0033). Stored
    -- rather than recomputed by each reader: the pass rule is applicant-type
    -- aware, and a second copy of it would be a second thing to keep in step.
    -- NULL means the row does not say, which the decision gate treats as not
    -- established.
    cip_passed      BOOLEAN,
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
    -- `principal` and `note_rate_pct` are part of the set, not adjacent to it.
    -- Expanding a stored schedule needs the principal the payments run on and
    -- the rate they were priced at, so a row holding the four columns below
    -- without those two is a schedule that cannot be reproduced -- and it
    -- satisfied every single-column NULL check, so the read path labelled it
    -- "contract" and filled the gaps with an inverted principal and a rate
    -- recovered from a rounded payment. Inferred numbers presented as agreed
    -- terms. Mirrors disclosure-service's CONTRACT_FACTS. Reviewed on PR #10.
    CONSTRAINT offers_schedule_all_or_nothing CHECK (
        (regular_payment_count IS NULL
         AND final_payment      IS NULL
         AND term_months        IS NULL
         AND schedule_version   IS NULL
         AND principal          IS NULL
         AND note_rate_pct      IS NULL)
        OR
        (regular_payment_count IS NOT NULL
         AND final_payment      IS NOT NULL
         AND term_months        IS NOT NULL
         AND schedule_version   IS NOT NULL
         AND principal          IS NOT NULL
         AND note_rate_pct      IS NOT NULL)
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
    -- The contractual rate the payment stream is priced at and servicing
    -- amortizes (D19; db/migrations/0038 expand, 0039 contract).
    --
    -- This column replaced `apr`, which was the defect: it held THIS value on
    -- loans boarded by the current path and the DISCLOSED APR on legacy ones --
    -- 5.196% for a contract priced at 7.99%, because the disclosed figure
    -- carries the prepaid fee. Servicing amortizes the column, so billing the
    -- disclosed figure would charge a borrower above their own disclosure.
    --
    -- NOT NULL: 0039 refused to drop `apr` while any loan lacked a proven rate,
    -- so a boarding path that forgets this now fails at the INSERT instead of
    -- creating a loan whose rate nobody knows.
    note_rate_pct   NUMERIC(7,3) NOT NULL,
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

-- Balance PROJECTION. `ledger_entries` (below, ADR 0010 / db/migrations/0035) is the
-- record of what moved; the projection trigger maintains these columns by composing
-- signed deltas, so two concurrent movements compose instead of one overwriting the
-- other. This comment read "No ledger, no transaction history" in the same file that
-- creates the ledger 34 lines later.
--
-- Three legacy writers still UPDATE these columns directly. Their deltas are captured
-- into the ledger by the compatibility bridge, and the guard that would reject a direct
-- write ships disabled until those writers are converted (ADR 0010 steps 3 and 5).
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
-- No PAN and no CVV: db/migrations/0031 dropped them from existing databases
-- (after 0029 back-filled last4 so payment history still displays), and a fresh
-- volume has never created them since. This file and that migration changed in
-- the same release on purpose -- the parity suite compares a fresh install
-- against a migrated one.
CREATE TABLE IF NOT EXISTS payments (
    id          SERIAL PRIMARY KEY,
    loan_id     INTEGER REFERENCES loans(id),
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
    -- When the processor CONFIRMED the capture (db/migrations/0040), written in
    -- the same UPDATE that sets auth_status. Reconciliation scopes its window on
    -- this rather than created_at: created_at is stamped at INSERT while the row
    -- is still pending, so an authorization crossing midnight would put the
    -- capture in the previous day's window and report a false break.
    captured_at TIMESTAMPTZ,
    -- Review fix (db/migrations/0019): the processor's own authorization id,
    -- persisted in the SAME UPDATE that flips auth_status to 'captured' --
    -- a pending retry asks the processor for this via get_authorization()
    -- before ever calling authorize_charge() again, instead of blindly
    -- re-charging. See services/payment-service/app/payments.py.
    authorization_id TEXT,
    -- The PROCESSOR's own settlement reference for this capture, e.g. PR-100231
    -- (db/migrations/0041), written in the same UPDATE that sets auth_status.
    -- This is the join key to the settlement file: authorization_id above is a
    -- DIFFERENT identifier minted by our own authorization call and appears in
    -- no settlement file, which is why reconciliation could only compare
    -- per-loan totals and could therefore net two offsetting defects to zero.
    -- NULL on rows captured before 0041; reconciliation reports those as
    -- unreferenced_capture breaks rather than skipping them.
    processor_ref TEXT,
    -- Who captured this payment (db/migrations/0042). 'processor' means
    -- payment-service obtained a real authorization and the row must appear in a
    -- settlement file -- these are the only rows reconciliation compares.
    -- 'servicing_legacy' was servicing-service's prototype POST /payments (D2),
    -- which called no processor, so no settlement line exists for those rows and
    -- comparing them against one is a category error rather than a strict
    -- control. **That route is retired and the value is now closed to new rows**
    -- -- no writer emits it. It stays in this CHECK because the rows it already
    -- wrote are real money history: a schema that rejected the value would
    -- invalidate every database holding one.
    -- 'unknown' is the default and covers rows written before the column:
    -- counted by reconciliation, excluded from the comparison, because admitting
    -- them would manufacture breaks out of missing evidence.
    capture_source TEXT NOT NULL DEFAULT 'unknown'
        CONSTRAINT payments_capture_source_known
        CHECK (capture_source IN ('processor', 'servicing_legacy', 'unknown')),
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

    -- One identifier following this payment across services (db/migrations/0043).
    -- NOT the idempotency key: that one is caller-supplied and decides dedupe,
    -- this one is server-minted and inert -- nothing behaves differently because
    -- of it. NULL means the row predates the trace; no reader may require it.
    correlation_id TEXT,
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
CREATE UNIQUE INDEX IF NOT EXISTS applications_idempotency_key_uniq
    ON applications (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status);
CREATE INDEX IF NOT EXISTS idx_payments_loan ON payments(loan_id);
CREATE INDEX IF NOT EXISTS idx_offers_app ON offers(app_id);

CREATE INDEX IF NOT EXISTS idx_kyc_checks_application_id ON kyc_checks(application_id);

-- ---------------------------------------------------------------------------
-- ADR 0010: the append-only ledger and its projection. Identical to
-- db/migrations/0035 -- a fresh volume and a migrated database must agree, and
-- db/tests/test_migration_paths_converge.py builds both and compares them.
--
-- The opening balances for seeded loans are written by db/init/007, AFTER the
-- seed data exists. They cannot be here: this file creates the tables, and there
-- are no balances to open yet.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ledger_entries (
    id           BIGSERIAL   PRIMARY KEY,
    loan_id      INTEGER     NOT NULL REFERENCES loans(id),

    -- What moved. A signed delta, never a total: an entry says "this much
    -- changed", so two concurrent entries compose instead of racing. This is
    -- what closes D3, and it is why `amount` may be negative.
    component    TEXT        NOT NULL CHECK (component IN ('principal','interest','fees')),
    amount       NUMERIC(14,2) NOT NULL CHECK (amount <> 0),

    -- Why it moved. 'opening_balance' is the back-fill's marker and nothing else
    -- may use it: it means "this loan's balance as it stood when the ledger
    -- began, with no record of how it got there". A distinct type rather than a
    -- boolean, because it cannot be defaulted, cannot be forgotten on insert,
    -- and every query that means "real money movements" already filters on
    -- entry_type.
    entry_type   TEXT        NOT NULL CHECK (entry_type IN
                   ('opening_balance','legacy_direct_write','disbursement','payment',
                    'fee_assessed','fee_waived','adjustment')),
    reason       TEXT,

    -- Who moved it.
    actor_id     INTEGER,
    actor_role   TEXT,

    -- Provenance. payment_id makes an apply idempotent by construction.
    --
    -- Deliberately no single-column REFERENCES here: the foreign key is the
    -- COMPOSITE one added below, which ties the payment to the same loan the
    -- entry moves. A plain reference to payments(id) would let an entry cite a
    -- payment captured for one borrower while moving another borrower's
    -- balance -- and ledger rows are immutable, so that movement could never be
    -- corrected by an update. It would sit on the wrong balance permanently.
    payment_id   INTEGER,

    -- The correlation id of the payment that produced this entry, as RECEIVED
    -- from payment-service (db/migrations/0043). Never minted here: a second
    -- generator would leave each side holding an id the other has never seen,
    -- which is the failure this column exists to prevent. NULL for entries with
    -- no payment behind them -- a fee assessment, an approved adjustment.
    correlation_id TEXT,

    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ledger_control (
    singleton           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    initialization_open BOOLEAN NOT NULL DEFAULT TRUE
);
INSERT INTO ledger_control(singleton, initialization_open)
VALUES (TRUE, TRUE) ON CONFLICT (singleton) DO NOTHING;

CREATE OR REPLACE FUNCTION ledger_control_cannot_reopen() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.initialization_open = FALSE
       OR NEW.initialization_open = TRUE THEN
        RAISE EXCEPTION 'ledger initialization gate cannot be reopened or deleted';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS ledger_control_one_way ON ledger_control;
CREATE TRIGGER ledger_control_one_way
    BEFORE UPDATE OR DELETE ON ledger_control
    FOR EACH ROW EXECUTE FUNCTION ledger_control_cannot_reopen();

CREATE OR REPLACE FUNCTION ledger_system_entry_is_authorized() RETURNS trigger AS $$
BEGIN
    IF NEW.entry_type = 'legacy_direct_write' AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'legacy_direct_write may only come from the balances capture trigger';
    END IF;
    IF NEW.entry_type = 'opening_balance' AND NOT EXISTS (
        SELECT 1 FROM ledger_control WHERE singleton AND initialization_open
    ) THEN
        RAISE EXCEPTION 'opening_balance is closed after ledger initialization';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS ledger_entries_system_type_guard ON ledger_entries;
CREATE TRIGGER ledger_entries_system_type_guard
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_system_entry_is_authorized();

-- Invariant 7: exactly one entry per payment/component pair. NOT per payment --
-- a waterfall (D14) splits one payment across components by definition, so a
-- single-row rule would forbid the thing the ledger is being built to allow.
-- Idempotency comes from the pair being unique, not from the payment appearing
-- once.
CREATE UNIQUE INDEX IF NOT EXISTS ledger_entries_payment_component
    ON ledger_entries (payment_id, component) WHERE payment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ledger_entries_loan ON ledger_entries (loan_id, occurred_at);

-- Searchable by the id an operator reads off a log line (db/migrations/0043).
-- Partial: NULL for every pre-trace row and every entry with no payment behind
-- it, and indexing those buys nothing.
CREATE INDEX IF NOT EXISTS idx_ledger_entries_correlation_id
    ON ledger_entries (correlation_id) WHERE correlation_id IS NOT NULL;

-- Invariant 7, the half the unique index cannot express: a payment entry must
-- HAVE a payment, and nothing else may have one.
--
-- The index above is `UNIQUE (payment_id, component) WHERE payment_id IS NOT
-- NULL`, and NULLs are excluded from it. So without this constraint a
-- ledger-writing path that passed no payment_id would create entries that never
-- collide -- a retried apply posting the balance twice, which is exactly the
-- idempotency the pair is supposed to provide. ADR 0010's invariant 7 says so in
-- words; this is the words made enforceable.
--
-- And the other direction: a non-payment entry may not consume a
-- (payment_id, component) pair, which would block the real payment entry from
-- ever being written. An adjustment's provenance is its proposal (ADR 0011), not
-- a payment.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_payment_provenance;
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_payment_provenance CHECK (
    (entry_type = 'payment') = (payment_id IS NOT NULL)
);

-- The payment must belong to the SAME loan the entry moves.
--
-- `loan_id` and `payment_id` were independent, so a row could cite a payment
-- captured for loan A while projecting the movement onto loan B. Nothing in the
-- schema said otherwise, and the ledger is immutable: a wrong movement cannot be
-- corrected by updating the row, so it stays on that borrower's balance for
-- good.
--
-- Enforced by a COMPOSITE foreign key rather than a trigger, because it is a
-- referential fact and a declarative constraint cannot be forgotten, bypassed by
-- a session flag, or dropped independently of the column it protects. It needs a
-- unique key on the referenced pair; `payments.id` is already the primary key, so
-- `(id, loan_id)` is unique by construction and the index costs only space.
--
-- MATCH SIMPLE (the default) is what makes this work alongside the CHECK above:
-- when `payment_id` is NULL the constraint does not apply at all, which is
-- exactly right for the non-payment entry types. When it is present, both columns
-- must match a real payments row -- so an entry may not cite a payment that is
-- not attached to any loan either.
-- Dropped in dependency order and re-added in the reverse, so a replay of this
-- file is idempotent. The unique key cannot be dropped while the foreign key
-- depends on its index, and this migration is replayed by
-- db/tests/test_migration_paths_converge.py precisely to catch that.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_entries_payment_loan_fk;
ALTER TABLE payments       DROP CONSTRAINT IF EXISTS payments_id_loan_uniq;

ALTER TABLE payments ADD CONSTRAINT payments_id_loan_uniq UNIQUE (id, loan_id);
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_entries_payment_loan_fk
    FOREIGN KEY (payment_id, loan_id) REFERENCES payments (id, loan_id);

-- The same rule again, BEFORE the row is inserted.
--
-- Not redundancy for its own sake. The composite key is the guarantee; this is
-- what makes the failure legible and makes it happen FIRST. A referential
-- constraint is checked by an internal AFTER-row trigger, so without this the
-- projection trigger can run before it -- and if the wrongly-named loan has no
-- `balances` row, the error a developer sees is the projection's row-count
-- complaint about loan B rather than the fact that the entry cited loan A's
-- payment. Same rollback either way, entirely different diagnosis, and the
-- misleading one arrives on the path most likely to be hit.
CREATE OR REPLACE FUNCTION ledger_entry_payment_matches_loan() RETURNS trigger AS $$
DECLARE
    payment_loan INTEGER;
    payment_status TEXT;
BEGIN
    IF NEW.payment_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT loan_id, auth_status INTO payment_loan, payment_status
      FROM payments WHERE id = NEW.payment_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger entry names payment % which does not exist',
                        NEW.payment_id;
    END IF;
    IF payment_loan IS DISTINCT FROM NEW.loan_id THEN
        RAISE EXCEPTION 'ledger entry moves loan % but cites payment %, which '
                        'was captured for loan %',
                        NEW.loan_id, NEW.payment_id, payment_loan;
    END IF;
    IF payment_status IS DISTINCT FROM 'captured' THEN
        RAISE EXCEPTION 'ledger payment entry requires captured payment % (status: %)',
                        NEW.payment_id, payment_status;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_payment_belongs_to_loan ON ledger_entries;
CREATE TRIGGER ledger_entries_payment_belongs_to_loan
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_payment_matches_loan();

-- Invariant 4: the sign is keyed to the effect on what the borrower owes.
-- A payment reduces; a fee assessment increases; only an adjustment may go
-- either way, which is exactly why it is the type that needs an approver.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_sign_matches_type;
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_sign_matches_type CHECK (
       (entry_type = 'payment'         AND amount < 0)
    OR (entry_type = 'fee_waived'      AND amount < 0)
    OR (entry_type = 'fee_assessed'    AND amount > 0)
    OR (entry_type = 'disbursement'    AND amount > 0)
    OR (entry_type = 'opening_balance' AND amount <> 0)
    OR (entry_type = 'legacy_direct_write' AND amount <> 0)
    OR (entry_type = 'adjustment')
);

ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_type_matches_component;
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_type_matches_component CHECK (
       (entry_type IN ('opening_balance','legacy_direct_write','adjustment')
        AND component IN ('principal','fees'))
    OR (entry_type = 'disbursement' AND component = 'principal')
    OR (entry_type = 'payment' AND component IN ('principal','fees','interest'))
    OR (entry_type IN ('fee_assessed','fee_waived') AND component = 'fees')
);

CREATE OR REPLACE FUNCTION ledger_payment_allocation_matches_capture() RETURNS trigger AS $$
DECLARE captured NUMERIC(14,2); allocated NUMERIC(14,2);
BEGIN
    IF NEW.payment_id IS NULL THEN RETURN NULL; END IF;
    SELECT amount INTO captured FROM payments WHERE id = NEW.payment_id;
    SELECT COALESCE(-SUM(amount), 0) INTO allocated
      FROM ledger_entries WHERE payment_id = NEW.payment_id;
    IF allocated <> captured THEN
        RAISE EXCEPTION 'ledger allocation % does not equal captured payment % for payment %',
                        allocated, captured, NEW.payment_id;
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS ledger_payment_allocation_exact ON ledger_entries;
CREATE CONSTRAINT TRIGGER ledger_payment_allocation_exact
    AFTER INSERT ON ledger_entries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_payment_allocation_matches_capture();

-- A posted payment's amount is part of the immutable ledger provenance.
CREATE OR REPLACE FUNCTION reject_posted_payment_amount_change() RETURNS trigger AS $$
BEGIN
    IF (NEW.amount IS DISTINCT FROM OLD.amount
        OR NEW.auth_status IS DISTINCT FROM OLD.auth_status)
       AND (EXISTS (SELECT 1 FROM ledger_entries WHERE payment_id = OLD.id)
            OR EXISTS (SELECT 1 FROM payment_applications WHERE payment_id = OLD.id)) THEN
        RAISE EXCEPTION 'cannot change amount or capture status for posted payment %', OLD.id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS payments_posted_amount_immutable ON payments;
CREATE TRIGGER payments_posted_amount_immutable
    BEFORE UPDATE ON payments
    FOR EACH ROW EXECUTE FUNCTION reject_posted_payment_amount_change();

-- Invariant 5: a human-directed entry names the human.
-- 'payment' is exempt: servicing's apply-payment receives an amount and a
-- payment_id and no actor, because the borrower is not "acting" on the balance
-- in the sense this column means. Requiring one here would fail every real
-- payment on insert. Its provenance is stronger than an actor string anyway --
-- payment_id points at the row carrying the idempotency key and the capture.
-- 'opening_balance' is exempt because no one authored it.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_actor_required;
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_actor_required CHECK (
    entry_type IN ('disbursement','fee_assessed','payment','opening_balance',
                   'legacy_direct_write')
    OR (actor_id IS NOT NULL AND actor_role IS NOT NULL)
);

-- Invariant 1: append-only. A trigger rather than a REVOKE, for the reason
-- ADR 0002/0006 already established for decision_events: every service connects
-- as the schema-owning role, so a revoke from the owner does not stick.
CREATE OR REPLACE FUNCTION ledger_entries_are_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entries is append-only (attempted % on id %)',
                    TG_OP, COALESCE(OLD.id, NEW.id);
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_immutable ON ledger_entries;
CREATE TRIGGER ledger_entries_immutable
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_are_immutable();

-- Invariant 3: `balances` is a projection. This trigger is what maintains it.
--
-- Opening-state and legacy-capture entries describe a mutation already present
-- in balances and do not project. Normal entries cannot be suppressed by a
-- caller-controlled session setting.
CREATE OR REPLACE FUNCTION project_ledger_entry() RETURNS trigger AS $$
DECLARE
    projected INTEGER;
BEGIN
    IF NEW.entry_type IN ('opening_balance', 'legacy_direct_write') THEN
        RETURN NEW;
    END IF;

    PERFORM set_config('meridian.projecting', 'on', true);

    IF NEW.component = 'principal' THEN
        UPDATE balances SET balance  = balance  + NEW.amount, updated_at = now()
         WHERE loan_id = NEW.loan_id;
        GET DIAGNOSTICS projected = ROW_COUNT;
    ELSIF NEW.component = 'fees' THEN
        UPDATE balances SET past_due = COALESCE(past_due, 0) + NEW.amount, updated_at = now()
         WHERE loan_id = NEW.loan_id;
        GET DIAGNOSTICS projected = ROW_COUNT;
    ELSE
        -- 'interest' projects nowhere: it is owed within a payment, not a
        -- separate balance the borrower carries. See ADR 0010. Nothing was
        -- updated and nothing should have been, so the check below is skipped.
        projected := 1;
    END IF;

    -- Exactly one row, or the entry does not exist.
    --
    -- Without this the UPDATE silently matches zero rows when a loan has no
    -- `balances` row, and the insert still succeeds: the ledger records that
    -- money moved and no balance moves with it. That is the projection claiming
    -- to be maintained while it is not, which is the one failure this design
    -- cannot tolerate -- `balances` is derived, so a divergence is invisible
    -- until a parity run, and the entry is immutable so it cannot be corrected
    -- afterwards.
    --
    -- Raising rolls back the INSERT with it. An entry that could not be
    -- projected must not be retained: it would be a permanent, uncorrectable
    -- record of a movement that never reached the borrower's balance.
    IF projected <> 1 THEN
        RAISE EXCEPTION 'ledger entry for loan % projected onto % balance rows '
                        '(expected exactly 1)', NEW.loan_id, projected;
    END IF;

    PERFORM set_config('meridian.projecting', 'off', true);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_project ON ledger_entries;
CREATE TRIGGER ledger_entries_project
    AFTER INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION project_ledger_entry();

-- The write-guard function ships now and the TRIGGER IS NOT CREATED. Step 5
-- enables it, once every writer has been converted (step 3) and verified. Having
-- the function present makes that step one statement, and makes reverting it one
-- statement too.
CREATE OR REPLACE FUNCTION balances_are_trigger_maintained() RETURNS trigger AS $$
BEGIN
    IF current_setting('meridian.projecting', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'balances is maintained by the ledger projection; '
                        'write a ledger entry instead';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

-- Immediate guard provenance is backed by a deferred parity invariant because
-- custom GUCs are caller-settable and therefore cannot be authorization alone.
CREATE OR REPLACE FUNCTION balances_must_match_ledger() RETURNS trigger AS $$
DECLARE
    target_loan INTEGER := COALESCE(NEW.loan_id, OLD.loan_id);
    actual_principal NUMERIC(14,2);
    actual_fees NUMERIC(14,2);
    ledger_principal NUMERIC(14,2);
    ledger_fees NUMERIC(14,2);
BEGIN
    SELECT balance, COALESCE(past_due, 0)
      INTO actual_principal, actual_fees
      FROM balances WHERE loan_id = target_loan;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'balance projection row for loan % cannot be removed', target_loan;
    END IF;
    SELECT COALESCE(SUM(amount) FILTER (WHERE component = 'principal'), 0),
           COALESCE(SUM(amount) FILTER (WHERE component = 'fees'), 0)
      INTO ledger_principal, ledger_fees
      FROM ledger_entries WHERE loan_id = target_loan;
    IF actual_principal IS DISTINCT FROM ledger_principal
       OR actual_fees IS DISTINCT FROM ledger_fees THEN
        RAISE EXCEPTION 'balance/ledger parity violation for loan %', target_loan;
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

COMMENT ON FUNCTION balances_are_trigger_maintained() IS
    'ADR 0010 step 5. Not attached to a trigger yet -- every writer must be '
    'converted first, or the guard turns working code into exceptions. Enable '
    'with: CREATE TRIGGER balances_guard BEFORE UPDATE OR DELETE ON balances '
    'FOR EACH ROW EXECUTE FUNCTION balances_are_trigger_maintained();';

-- Mirrors db/migrations/0040. Reconciliation's window predicate reads this on
-- every run; without it here a fresh install and a migrated one would differ,
-- which test_migration_paths_converge catches -- and did.
CREATE INDEX IF NOT EXISTS idx_payments_captured_at
    ON payments (capture_source, captured_at)
 WHERE auth_status = 'captured';

-- Mirrors db/migrations/0041. One settlement line, one capture: two payment
-- rows claiming the same processor reference is either a double-recorded
-- capture or a mis-keyed one, and either makes the transaction-level
-- comparison ambiguous exactly where it has to be exact. Partial so the
-- unreferenced legacy rows do not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_processor_ref
    ON payments (processor_ref)
 WHERE processor_ref IS NOT NULL;

-- The payment side of the cross-service trace (db/migrations/0043).
CREATE INDEX IF NOT EXISTS idx_payments_correlation_id
    ON payments (correlation_id) WHERE correlation_id IS NOT NULL;

-- D7: one row per reconciliation run (db/migrations/0034). Counts and totals
-- only -- no card data, no applicant identifiers, no processor references. A
-- control that leaves no trace is indistinguishable from one that never ran.
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id              BIGSERIAL   PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    outcome         TEXT        NOT NULL CHECK (outcome IN ('ok','breach','error')),
    loans_compared  INTEGER     NOT NULL DEFAULT 0,
    -- How fine the comparison was: it is keyed on (loan_id, processor_ref), and
    -- a run matching many loans but few references compared coarse per-loan
    -- totals, which is the state this control was fixed out of. Captures with no
    -- reference cannot be matched at all and are counted separately AND reported
    -- as breaks (db/migrations/0034, 0041).
    references_compared   INTEGER NOT NULL DEFAULT 0,
    unreferenced_captures INTEGER NOT NULL DEFAULT 0,
    -- Captures excluded from the comparison entirely -- the legacy servicing
    -- writer and rows of unestablished provenance (db/migrations/0042).
    out_of_scope_captures INTEGER NOT NULL DEFAULT 0,
    breaks_found    INTEGER     NOT NULL DEFAULT 0,
    break_value     NUMERIC(14,2) NOT NULL DEFAULT 0,
    threshold_value NUMERIC(14,2) NOT NULL DEFAULT 0,
    breaks          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- The period covered and the file read (db/migrations/0034). A result is not
    -- interpretable without them.
    window_start    DATE,
    window_end      DATE,
    source          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error_code      TEXT
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_outcome_time
    ON reconciliation_runs (outcome, started_at DESC);


-- =============================================================================
-- Maker-checker proposals (ADR 0011 step 1, db/migrations/0036).
--
-- Mirrored here so a freshly initialised database and a migrated one agree --
-- `db/tests/test_schema_parity.py` compares the two and fails on any difference.
--
-- NO application writer creates these rows yet. `adjust-balance` and `waive-fee`
-- still move money on one person's say-so; what exists here is the shape the
-- control needs and the guarantees that shape enforces by itself. D8 stays open
-- until the cutover lands.
--
-- The two configured limits (MAKER_CHECKER_ADMIN_THRESHOLD, MAKER_CHECKER_MAX_DELTA)
-- appear nowhere in this schema on purpose: they are human-approved configuration
-- read at runtime, not database facts, and a CHECK carrying a figure would make a
-- policy change a migration.
-- =============================================================================

CREATE TABLE IF NOT EXISTS pending_movements (
    id            BIGSERIAL   PRIMARY KEY,
    loan_id       INTEGER     NOT NULL REFERENCES loans(id),
    component     TEXT        NOT NULL,
    amount        NUMERIC(14,2) NOT NULL,
    entry_type    TEXT        NOT NULL CHECK (entry_type IN ('adjustment','fee_waived')),
    -- Required: a proposal without a reason is unreviewable. The approver is
    -- otherwise being asked to authorise a number with no account of why.
    reason        TEXT        NOT NULL,
    requested_by  INTEGER     NOT NULL,
    -- The role is stored beside the id on both sides because the ledger's actor
    -- constraint needs both, and the entry's actor is written FROM this row
    -- rather than from the caller. A proposal recording only ids would leave the
    -- role to whoever inserts the entry.
    requested_role TEXT       NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Terminal state, written once. This table is NOT append-only: resolving a
    -- proposal is the one legitimate mutation in the design, confined here
    -- precisely so the ledger's own guarantee stays absolute.
    resolution    TEXT        CHECK (resolution IN ('approved','rejected')),
    resolved_by   INTEGER,
    resolved_role TEXT,
    resolved_at   TIMESTAMPTZ,
    ledger_entry_id BIGINT    REFERENCES ledger_entries(id),

    -- The threshold this resolution was judged against, recorded at resolution
    -- time (spec 0002 AC-22). A history of approvals is unreadable if the bar
    -- moved and nothing says when -- the same rule `reconciliation_runs`
    -- follows for its own threshold. NULL until resolved; the cutover writes it
    -- from configuration, and this schema states no figure of its own.
    resolved_threshold NUMERIC(14,2),

    CONSTRAINT no_self_approval CHECK (resolved_by IS NULL OR resolved_by <> requested_by),
    -- PM-THRESHOLD-001: `resolved_threshold` is part of a complete resolution.
    -- It was nullable and absent from this constraint, so a resolution could
    -- commit without recording the bar it was judged against -- which is the one
    -- thing spec 0002 AC-22 says the column exists to preserve, and a history of
    -- approvals is unreadable if the bar moved and nothing says when.
    CONSTRAINT resolution_complete CHECK (
        (resolution IS NULL
            AND resolved_by IS NULL AND resolved_role IS NULL AND resolved_at IS NULL
            AND resolved_threshold IS NULL)
     OR (resolution IS NOT NULL
            AND resolved_by IS NOT NULL AND resolved_role IS NOT NULL
            AND resolved_at IS NOT NULL AND resolved_threshold IS NOT NULL)
    ),
    -- PM-REASON-001: `NOT NULL` admits '' and '   '. The reason is the evidence
    -- D8 names as missing, and an empty one is the same absence wearing a value.
    -- Matched on a non-space character rather than btrim() so tabs and newlines
    -- are covered too.
    CONSTRAINT pending_reason_not_blank CHECK (reason ~ '[^[:space:]]'),
    -- PM-TERMS-001: a proposal the ledger can never execute must not reach an
    -- approver's queue. These mirror ADR 0010's executable constraints, so the
    -- refusal happens at creation with a message the requester can act on,
    -- rather than at approval -- after a second person has reviewed and accepted
    -- a request the system was always going to reject.
    CONSTRAINT pending_amount_nonzero CHECK (amount <> 0),
    CONSTRAINT pending_fee_waiver_reduces CHECK (
        entry_type <> 'fee_waived' OR amount < 0
    ),
    -- "an approval produces exactly one ledger entry, a rejection produces none"
    -- is NOT a CHECK. It cannot be: the entry is inserted after the row is marked
    -- approved, so an immediate CHECK would fail mid-transaction on a state that
    -- is legitimately transient. It is enforced at COMMIT instead, by the
    -- deferred constraint trigger below -- PostgreSQL CHECK constraints cannot be
    -- DEFERRABLE, which is exactly why that is a constraint trigger.

    -- Same component vocabulary as the ledger. Without this a proposal could
    -- name a component the ledger cannot hold, and the mismatch would surface
    -- only at approval -- after a human had reviewed and accepted it.
    -- The ledger holds an `adjustment` against principal or fees only
    -- (`ledger_type_matches_component`, ADR 0010). `interest` is in the
    -- vocabulary for the components the ledger CAN hold generally, and an
    -- interest adjustment is not one of them -- so proposing one would queue a
    -- movement that fails at execution. Spec 0002 REQ-VAL-2 lists all three; the
    -- narrowing to what is executable is recorded there rather than left as a
    -- silent difference between the document and the database.
    CONSTRAINT pending_component CHECK (
        (entry_type = 'adjustment'  AND component IN ('principal','fees'))
     OR (entry_type = 'fee_waived'  AND component = 'fees')
    ),

    -- A fee waiver moves fees. ADR 0010 fixes `fee_waived` to the `fees`
    -- component, so a proposal naming another describes a movement the ledger
    -- cannot represent, and it would fail at the entry insert AFTER a second
    -- person approved it. `adjustment` is deliberately open to all three:
    -- correcting principal, interest or fees are all real corrections.
    CONSTRAINT pending_fee_waiver_is_fees CHECK (
        entry_type <> 'fee_waived' OR component = 'fees'
    )
);

-- The queue a reviewer reads: unresolved proposals, oldest first.
CREATE INDEX IF NOT EXISTS pending_movements_queue
    ON pending_movements (requested_at) WHERE resolution IS NULL;

CREATE INDEX IF NOT EXISTS pending_movements_loan
    ON pending_movements (loan_id, requested_at);


-- --- the link to the ledger ---------------------------------------------------

ALTER TABLE ledger_entries
    ADD COLUMN IF NOT EXISTS pending_movement_id BIGINT;

-- Existence checked against THIS table, not against the constraint name alone.
-- `pg_constraint` is global: a name-only lookup finds a constraint of the same
-- name in any other schema, so on a database that already holds one -- a parity
-- fixture, a test schema, a staging copy -- these blocks silently skipped, and
-- the UNIQUE guarantee that stops one approval yielding two ledger entries was
-- never created. Caught by `test_one_approval_cannot_yield_two_entries`, which
-- inserted a second entry successfully.
--
-- `conrelid = 'ledger_entries'::regclass` resolves through the search_path, so
-- it asks about the table this migration is actually altering.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'ledger_entries'::regclass
                      AND conname = 'ledger_entries_pending_movement_key') THEN
        -- UNIQUE: one approval can never yield two entries.
        ALTER TABLE ledger_entries
            ADD CONSTRAINT ledger_entries_pending_movement_key UNIQUE (pending_movement_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'ledger_entries'::regclass
                      AND conname = 'ledger_entries_pending_movement_fk') THEN
        ALTER TABLE ledger_entries
            ADD CONSTRAINT ledger_entries_pending_movement_fk
            FOREIGN KEY (pending_movement_id) REFERENCES pending_movements(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'ledger_entries'::regclass
                      AND conname = 'approved_entries_have_a_proposal') THEN
        -- Which entry types must come from an approval at all. The maker-checker
        -- subjects may only enter the ledger through a proposal; the
        -- machine-originated types must carry none, so a payment cannot be
        -- dressed up as an approved adjustment.
        ALTER TABLE ledger_entries ADD CONSTRAINT approved_entries_have_a_proposal CHECK (
            (entry_type IN ('adjustment','fee_waived') AND pending_movement_id IS NOT NULL)
         OR (entry_type NOT IN ('adjustment','fee_waived') AND pending_movement_id IS NULL)
        );
    END IF;
END $$;


-- --- invariants 1 and 2: one terminal transition, and nothing else changes -----

CREATE OR REPLACE FUNCTION pending_movements_single_transition() RETURNS trigger AS $$
BEGIN
    -- The substance never changes, resolved or not. `reason` and `requested_at`
    -- are in this list, not merely the fields describing the money: anything
    -- holding the application role could otherwise rewrite WHY a movement was
    -- requested, after a second person approved the reason they were shown. The
    -- reason is the evidence D8 says is missing; a rewritable reason is a note.
    IF NEW.loan_id        IS DISTINCT FROM OLD.loan_id
    OR NEW.component      IS DISTINCT FROM OLD.component
    OR NEW.amount         IS DISTINCT FROM OLD.amount
    OR NEW.entry_type     IS DISTINCT FROM OLD.entry_type
    OR NEW.reason         IS DISTINCT FROM OLD.reason
    OR NEW.requested_by   IS DISTINCT FROM OLD.requested_by
    OR NEW.requested_role IS DISTINCT FROM OLD.requested_role
    OR NEW.requested_at   IS DISTINCT FROM OLD.requested_at THEN
        RAISE EXCEPTION 'the substance of a pending movement is immutable';
    END IF;

    -- PM-LINK-001. The link may not be attached on the SAME update that resolves
    -- the proposal. Until this, the first resolution could set `resolution` and
    -- `ledger_entry_id` together -- and nothing checked that the entry it pointed
    -- at belonged to THIS proposal, so an approval could commit citing another
    -- proposal's ledger row. Invariants 6 and 7 were enforced on the entry's way
    -- in and not on the proposal's way out.
    --
    -- Forcing a separate update is what makes the reciprocal checkable: the entry
    -- must already exist, and by then it carries a `pending_movement_id` the
    -- deferred check below compares against this row.
    IF OLD.resolution IS NULL AND NEW.ledger_entry_id IS NOT NULL THEN
        RAISE EXCEPTION 'a pending movement may not gain a ledger entry link in '
                        'the same statement that resolves it -- insert the entry '
                        'first, then attach it';
    END IF;

    IF OLD.resolution IS NOT NULL THEN
        -- Already resolved. Exactly ONE further write is legal: attaching the
        -- ledger entry this approval produced, once, from NULL. An earlier
        -- revision refused every post-resolution UPDATE outright, which made the
        -- approval order impossible -- mark approved, insert the entry, write the
        -- link back -- so no staff movement could ever complete.
        IF OLD.ledger_entry_id IS NOT NULL THEN
            RAISE EXCEPTION 'pending movement % is already linked to entry %',
                OLD.id, OLD.ledger_entry_id;
        END IF;
        IF NEW.ledger_entry_id IS NULL THEN
            RAISE EXCEPTION 'pending movement % is already %', OLD.id, OLD.resolution;
        END IF;
        IF OLD.resolution <> 'approved' THEN
            RAISE EXCEPTION 'pending movement % was %, so it produces no ledger entry',
                OLD.id, OLD.resolution;
        END IF;
        IF NEW.resolution    IS DISTINCT FROM OLD.resolution
        OR NEW.resolved_by   IS DISTINCT FROM OLD.resolved_by
        OR NEW.resolved_role IS DISTINCT FROM OLD.resolved_role
        OR NEW.resolved_at   IS DISTINCT FROM OLD.resolved_at
        OR NEW.resolved_threshold IS DISTINCT FROM OLD.resolved_threshold THEN
            RAISE EXCEPTION 'a resolved movement may only gain its ledger entry link';
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS pending_movements_one_way ON pending_movements;
CREATE TRIGGER pending_movements_one_way
    BEFORE UPDATE ON pending_movements
    FOR EACH ROW EXECUTE FUNCTION pending_movements_single_transition();


-- --- invariant 5: retention is a delete guard, not a promise -------------------

CREATE OR REPLACE FUNCTION pending_movements_are_retained() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'pending movement % may not be deleted: proposals are retained as the '
        'evidence of what staff asked for (%)',
        OLD.id, COALESCE(OLD.resolution, 'pending');
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS pending_movements_no_delete ON pending_movements;
CREATE TRIGGER pending_movements_no_delete
    BEFORE DELETE ON pending_movements
    FOR EACH ROW EXECUTE FUNCTION pending_movements_are_retained();


-- --- invariant 4: checked at COMMIT, because the intermediate state is legal ---

CREATE OR REPLACE FUNCTION pending_movement_resolution_is_complete() RETURNS trigger AS $$
DECLARE
    final_resolution TEXT;
    final_entry      BIGINT;
BEGIN
    -- The row AS IT STANDS NOW, not as it stood when this event was queued.
    -- Running at COMMIT inside the same transaction, this sees every earlier
    -- statement's effect, so the transient approved-without-entry state that
    -- queued the event is no longer what is validated. Both queued events re-read
    -- the same final row and agree, which makes the check idempotent rather than
    -- order-dependent -- an implementation adding a third UPDATE must not be able
    -- to break it by doing so.
    SELECT resolution, ledger_entry_id
      INTO final_resolution, final_entry
      FROM pending_movements
     WHERE id = NEW.id;

    IF NOT FOUND THEN
        -- Inserted and deleted in the same transaction. Nothing is committed
        -- about this proposal, so there is nothing to validate.
        RETURN NULL;
    END IF;

    IF final_resolution = 'approved' AND final_entry IS NULL THEN
        RAISE EXCEPTION 'approved movement % has no ledger entry', NEW.id;
    END IF;
    IF final_resolution IS DISTINCT FROM 'approved' AND final_entry IS NOT NULL THEN
        RAISE EXCEPTION 'movement % is %, so it must have no ledger entry',
                        NEW.id, COALESCE(final_resolution, 'pending');
    END IF;

    -- PM-LINK-001. Non-null is not enough: the entry must be THIS proposal's.
    -- Checked in both directions, because each catches a different mistake --
    -- a proposal pointing at a foreign entry, and an entry whose own
    -- `pending_movement_id` names someone else.
    IF final_entry IS NOT NULL THEN
        PERFORM 1 FROM ledger_entries
         WHERE id = final_entry AND pending_movement_id = NEW.id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'movement % points at ledger entry %, which does not '
                            'name it -- an approval may only link the entry it '
                            'authorised', NEW.id, final_entry;
        END IF;
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS pending_movements_resolution_complete ON pending_movements;
CREATE CONSTRAINT TRIGGER pending_movements_resolution_complete
    AFTER INSERT OR UPDATE ON pending_movements
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION pending_movement_resolution_is_complete();


-- --- invariants 3, 6 and 7: the entry must BE the movement that was approved ---

CREATE OR REPLACE FUNCTION ledger_entry_matches_its_proposal() RETURNS trigger AS $$
DECLARE
    proposal pending_movements;
BEGIN
    -- Machine-originated entries have no proposal and are not this trigger's
    -- business. `approved_entries_have_a_proposal` already refuses them a
    -- pending_movement_id.
    IF NEW.entry_type NOT IN ('adjustment','fee_waived') THEN
        RETURN NEW;
    END IF;

    IF NEW.pending_movement_id IS NULL THEN
        RAISE EXCEPTION 'a % entry must name the proposal that authorised it',
                        NEW.entry_type;
    END IF;

    -- FOR SHARE, not a plain read: the proposal must not be resolved differently
    -- by another transaction between this check and the commit depending on it.
    SELECT * INTO proposal FROM pending_movements
     WHERE id = NEW.pending_movement_id FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'pending movement % does not exist', NEW.pending_movement_id;
    END IF;

    IF proposal.resolution IS DISTINCT FROM 'approved' THEN
        RAISE EXCEPTION 'pending movement % is %, so it authorises no entry',
                        proposal.id, COALESCE(proposal.resolution, 'pending');
    END IF;

    -- Belt and braces with `no_self_approval` on the table. This is the path that
    -- writes the money, so it re-checks rather than assuming the constraint
    -- guarding the other path was never dropped.
    IF proposal.resolved_by IS NULL OR proposal.resolved_by = proposal.requested_by THEN
        RAISE EXCEPTION 'pending movement % has no distinct approver', proposal.id;
    END IF;

    IF NEW.loan_id    IS DISTINCT FROM proposal.loan_id
    OR NEW.component  IS DISTINCT FROM proposal.component
    OR NEW.amount     IS DISTINCT FROM proposal.amount
    OR NEW.entry_type IS DISTINCT FROM proposal.entry_type THEN
        RAISE EXCEPTION 'entry does not match pending movement % -- an approval '
                        'may not authorise different terms than the ones reviewed',
                        proposal.id;
    END IF;

    -- Overwritten, not validated: the actor on a human-authorised entry is the
    -- APPROVER, and a caller reproducing everything else correctly must not get
    -- to choose who is credited with authorising it. Both fields, because
    -- ledger_actor_required needs both and overwriting only the id would leave
    -- the role caller-supplied on exactly the row that exists to record it.
    NEW.actor_id   := proposal.resolved_by;
    NEW.actor_role := proposal.resolved_role;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_match_proposal ON ledger_entries;
CREATE TRIGGER ledger_entries_match_proposal
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_matches_its_proposal();


-- =============================================================================
-- The approval function (ADR 0011 step 2, db/migrations/0037).
--
-- Mirrored here so a fresh database and a migrated one agree. Approval is ONE
-- function because the ordering the 0036 triggers require is not discoverable
-- from the requirements: mark resolved, insert the entry, attach the link in a
-- separate statement. Every caller gets that order.
--
-- No policy is encoded: the threshold and the permitted loan statuses arrive as
-- parameters from a caller that read them from configuration and failed closed.
-- =============================================================================

CREATE OR REPLACE FUNCTION resolve_pending_movement(
    p_movement_id        BIGINT,
    p_resolver           INTEGER,
    p_resolver_role      TEXT,
    p_resolution         TEXT,          -- 'approved' | 'rejected'
    p_threshold          NUMERIC,       -- the bar this decision is judged against
    p_permitted_statuses TEXT[]         -- loan statuses a movement may execute on
) RETURNS BIGINT AS $$
DECLARE
    proposal     pending_movements;
    loan_status  TEXT;
    component_now NUMERIC;
    new_entry    BIGINT;
BEGIN
    IF p_resolution NOT IN ('approved', 'rejected') THEN
        RAISE EXCEPTION 'resolution must be approved or rejected, not %', p_resolution;
    END IF;
    IF p_resolver IS NULL OR p_resolver_role IS NULL THEN
        RAISE EXCEPTION 'a resolution must name the human making it';
    END IF;
    IF p_threshold IS NULL THEN
        RAISE EXCEPTION 'a resolution must record the threshold it was judged '
                        'against -- an approval history is unreadable if the bar '
                        'moved and nothing says when';
    END IF;
    IF p_permitted_statuses IS NULL OR array_length(p_permitted_statuses, 1) IS NULL THEN
        RAISE EXCEPTION 'no permitted loan statuses were supplied, so no movement '
                        'can be shown to be executable -- refusing rather than '
                        'assuming a default';
    END IF;

    -- 1. LOCK FIRST. Two approvers clicking at once would otherwise both read
    -- `resolution IS NULL` and both proceed. Everything below reads the locked
    -- row, never the caller's idea of it.
    SELECT * INTO proposal FROM pending_movements
     WHERE id = p_movement_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'pending movement % does not exist', p_movement_id;
    END IF;

    -- 2. Exactly one transition. The loser of the race lands here.
    IF proposal.resolution IS NOT NULL THEN
        RAISE EXCEPTION 'pending movement % is already %',
                        proposal.id, proposal.resolution;
    END IF;

    -- 3. No self-approval, including admin. Checked here as well as by the table
    -- constraint because this is the path that writes the money.
    IF p_resolver = proposal.requested_by THEN
        RAISE EXCEPTION 'pending movement % was requested by %, who may not '
                        'resolve it', proposal.id, proposal.requested_by;
    END IF;

    -- Revalidate the whole executable target INSIDE the lock. A proposal that
    -- was valid when raised is not necessarily valid now: the loan may have
    -- closed, servicing may have been removed, the fees may have been paid down.
    -- A check performed when the proposal entered the queue is not evidence
    -- about the state when money moves.
    --
    -- Done for a rejection too, but only as far as reading -- a rejection moves
    -- nothing, so it must remain possible even for a target that has since
    -- become unexecutable. Otherwise a proposal against a closed loan could be
    -- neither approved nor rejected, and would sit in the queue for ever.
    IF p_resolution = 'approved' THEN
        SELECT l.status INTO loan_status
          FROM loans l
          JOIN balances b ON b.loan_id = l.id
         WHERE l.id = proposal.loan_id
         FOR SHARE OF l;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'loan % is no longer serviced (missing loan or '
                            'balances row), so movement % cannot execute',
                            proposal.loan_id, proposal.id;
        END IF;

        -- Exact match, including case: a status the caller does not recognise
        -- must refuse rather than be normalised into one it does.
        --
        -- `loan_status IS NULL` is tested SEPARATELY and first. `NULL = ANY(...)`
        -- is NULL, `NOT NULL` is NULL, and `IF NULL THEN` does not execute -- so
        -- without this a loan with no status at all would sail through the check
        -- that exists to refuse unrecognised ones. `loans.status` is a nullable
        -- TEXT column, so that row shape is reachable rather than theoretical.
        -- Found by parametrising the status test over NULL.
        IF loan_status IS NULL OR NOT (loan_status = ANY (p_permitted_statuses)) THEN
            RAISE EXCEPTION 'loan % is %, which is not a status a movement may '
                            'execute on', proposal.loan_id,
                            COALESCE(loan_status, 'unset');
        END IF;

        -- The component may not be driven below zero. Re-read now, not at
        -- creation: a waiver raised when fees were 80.00 and approved after they
        -- were paid down to 10.00 was valid when written and is not now.
        SELECT CASE proposal.component
                 WHEN 'fees' THEN COALESCE(b.past_due, 0)
                 ELSE b.balance
               END
          INTO component_now
          FROM balances b WHERE b.loan_id = proposal.loan_id;
        IF component_now + proposal.amount < 0 THEN
            RAISE EXCEPTION 'movement % would take % below zero (% + % < 0)',
                            proposal.id, proposal.component, component_now,
                            proposal.amount;
        END IF;
    END IF;

    -- The order below is the one the 0036 triggers require, and it is why this
    -- is a function. Mark resolved first; insert the entry second; attach the
    -- link third, in its own statement. Reversing the first two fails the
    -- entry's proposal check ("is pending, so it authorises no entry"), and
    -- combining the first and third is refused outright by the transition
    -- trigger, which is what makes the reciprocal link verifiable.
    UPDATE pending_movements
       SET resolution = p_resolution,
           resolved_by = p_resolver,
           resolved_role = p_resolver_role,
           resolved_at = now(),
           resolved_threshold = p_threshold
     WHERE id = proposal.id;

    IF p_resolution = 'rejected' THEN
        -- 4. A rejection writes no entry, and the proposal is retained as the
        -- evidence that a control refused something.
        RETURN NULL;
    END IF;

    -- 5. Built FROM the locked row. Nothing here reads a caller argument for the
    -- money: an approval that inserted different terms than the ones reviewed
    -- would be a bypass wearing the shape of an approval.
    --
    -- 6. actor_id/actor_role are supplied, and the ledger's own trigger
    -- overwrites them from the proposal regardless -- so a future caller of this
    -- function cannot smuggle a different actor in either.
    INSERT INTO ledger_entries
        (loan_id, component, amount, entry_type, reason,
         actor_id, actor_role, pending_movement_id)
    VALUES
        (proposal.loan_id, proposal.component, proposal.amount, proposal.entry_type,
         proposal.reason, p_resolver, p_resolver_role, proposal.id)
    RETURNING id INTO new_entry;

    UPDATE pending_movements
       SET ledger_entry_id = new_entry
     WHERE id = proposal.id;

    RETURN new_entry;
END $$ LANGUAGE plpgsql;

COMMENT ON FUNCTION resolve_pending_movement(BIGINT, INTEGER, TEXT, TEXT, NUMERIC, TEXT[]) IS
    'The only path that resolves a maker-checker proposal (ADR 0011). Locks the '
    'proposal, permits exactly one transition, refuses self-approval, revalidates '
    'the executable target inside the lock, and on approval writes exactly one '
    'ledger entry built from the locked row. Policy (threshold, permitted '
    'statuses) is passed in by a caller that read it from configuration -- this '
    'function encodes none.';
