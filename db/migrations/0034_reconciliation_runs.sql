-- 0034 -- D7: give reconciliation somewhere to record that it ran.
--
-- `reconciliation.peek` exposed two totals and nothing else: no schedule, no
-- history, no threshold, no way to answer "when did this last agree?". A control
-- that leaves no trace is indistinguishable from one that never ran, and the
-- absence is silent -- the endpoint returns 200 whether reconciliation happened
-- yesterday or never.
--
-- This table is that trace. One row per run, successful or not.
--
-- No sensitive data by construction. It holds counts, signed money totals, and a
-- break list of (loan_id, processor_ref, ledger_total, settlement_total) -- no
-- card data, no cardholder name, no applicant identifiers. A break report is
-- read by whoever is on call, which is the wrong audience for PII, and a
-- reconciliation history is exactly the kind of table that gets exported into a
-- spreadsheet.
--
-- The processor's settlement reference IS recorded, and it is the one thing here
-- that has to be: a break is only actionable if it names the transaction, and
-- naming it is what stopped this control netting two defects on one loan into a
-- clean run (db/migrations/0041). A settlement reference is an opaque handle to a
-- money movement -- it identifies no person and reconstructs no card.

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id              BIGSERIAL   PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,

    -- 'ok'      -- ran, compared, everything inside threshold
    -- 'breach'  -- ran, compared, breaks exceeded the threshold
    -- 'error'   -- could not complete (settlement file missing, database down)
    --
    -- 'breach' and 'error' are deliberately different. A breach is a finding
    -- about the money; an error is a finding about the control itself, and
    -- treating them the same is how a broken control gets read as a clean one.
    outcome         TEXT        NOT NULL CHECK (outcome IN ('ok','breach','error')),

    -- What was compared, so a later reader can tell a clean run from a vacuous
    -- one. A run over zero loans is not a passing run.
    loans_compared  INTEGER     NOT NULL DEFAULT 0,

    -- How FINE the comparison was, which is a different question from how much
    -- of it there was. The comparison is keyed on (loan_id, processor_ref); a
    -- run that matched many loans and few references compared coarse totals,
    -- which is the state this control was fixed out of.
    references_compared  INTEGER NOT NULL DEFAULT 0,

    -- Captured payments in the window carrying no processor_ref, so no
    -- settlement line can corroborate them (rows predating db/migrations/0041,
    -- or a processor that reported no reference). They are counted here AND
    -- reported as breaks -- never skipped, because skipping them would
    -- understate our own side of the comparison.
    unreferenced_captures INTEGER NOT NULL DEFAULT 0,

    -- Captures in the window this control did not compare at all: rows written
    -- by servicing-service's legacy POST /payments, which calls no processor and
    -- so can appear in no settlement file, and rows whose provenance predates
    -- db/migrations/0042. Recorded because an exclusion nobody can see is how a
    -- comparison quietly narrows until it is comparing nothing.
    out_of_scope_captures INTEGER NOT NULL DEFAULT 0,

    breaks_found    INTEGER     NOT NULL DEFAULT 0,

    -- Sum of |ledger - settlement| across breaking references. Signed totals per
    -- reference live in `breaks`; this is the single number a threshold is set
    -- against. Absolute values, so two findings can never cancel.
    break_value     NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- The threshold this run was judged against, recorded WITH the run. A history
    -- of pass/fail is unreadable if the bar moved and nothing says when.
    threshold_value NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- [{"loan_id": 4471, "processor_ref": "PR-100244", "kind": "settlement_only",
    --   "ledger": "0.00", "settlement": "99.99", "difference": "-99.99"}, ...]
    -- `kind` is one of settlement_only, ledger_only, amount_mismatch or
    -- unreferenced_capture: money settled we never recorded, money recorded the
    -- processor never settled, a reference both sides know at different amounts,
    -- and a capture with no reference to match on. They route to different
    -- answers, so they are labelled rather than left to be inferred.
    -- Bounded when written -- see reconciliation.MAX_RECORDED_BREAKS -- because a
    -- systemic break would otherwise put every loan in one row.
    breaks          JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- The period this run covered, and which file it read. A result is not
    -- interpretable without them: "0 breaks" over an unknown window, from an
    -- unknown file, is not evidence of anything. Taken from the settlement
    -- file's own settlement_date values rather than configured, so a daily file
    -- and a back-filled one are both described correctly without a flag.
    window_start    DATE,
    window_end      DATE,
    -- {"file": "settlement.csv", "rows": 12, "undated_rows": 0,
    --  "unreferenced_rows": 0, "malformed_rows": 0, "sha256": "..."} -- identity,
    -- not contents. A digest makes a re-run of the same file recognisable; the
    -- three counts are what the vacuity checks fail the run on.
    source          JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Exception TYPE only on an error, never the message: a psycopg2 error string
    -- can carry the statement and its parameters.
    error_code      TEXT
);

-- The two questions asked of this table: "when did it last succeed?" and "what
-- has failed recently?".
CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_outcome_time
    ON reconciliation_runs (outcome, started_at DESC);

COMMENT ON TABLE reconciliation_runs IS
    'D7: one row per reconciliation run. Counts, money totals and the processor '
    'settlement references the breaks are attributed to -- no card data, no '
    'cardholder name, no applicant identifiers.';
