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
    -- 'servicing_legacy' is servicing-service's prototype POST /payments (D2),
    -- which calls no processor, so no settlement line exists for it and
    -- comparing it against one is a category error rather than a strict control.
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
BEGIN
    IF NEW.payment_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT loan_id INTO payment_loan FROM payments WHERE id = NEW.payment_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger entry names payment % which does not exist',
                        NEW.payment_id;
    END IF;
    IF payment_loan IS DISTINCT FROM NEW.loan_id THEN
        RAISE EXCEPTION 'ledger entry moves loan % but cites payment %, which '
                        'was captured for loan %',
                        NEW.loan_id, NEW.payment_id, payment_loan;
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
        UPDATE balances SET past_due = past_due + NEW.amount, updated_at = now()
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
    IF pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'balances is maintained by the ledger projection; '
                        'write a ledger entry instead';
    END IF;
    RETURN NEW;
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
