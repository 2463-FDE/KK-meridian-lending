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
-- per-loan break list of (loan_id, ledger_total, settlement_total) -- no card
-- data, no applicant identifiers, no processor references. A break report is
-- read by whoever is on call, which is the wrong audience for PII, and a
-- reconciliation history is exactly the kind of table that gets exported into a
-- spreadsheet.

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
    breaks_found    INTEGER     NOT NULL DEFAULT 0,

    -- Sum of |ledger - settlement| across breaking loans. Signed totals per loan
    -- live in `breaks`; this is the single number a threshold is set against.
    break_value     NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- The threshold this run was judged against, recorded WITH the run. A history
    -- of pass/fail is unreadable if the bar moved and nothing says when.
    threshold_value NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- [{"loan_id": 4471, "ledger": "349.99", "settlement": "250.00"}, ...]
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
    -- {"file": "settlement.csv", "rows": 12, "sha256": "..."} -- identity, not
    -- contents. A digest makes a re-run of the same file recognisable.
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
    'D7: one row per reconciliation run. Counts and totals only -- no card data, '
    'no applicant identifiers, no processor references.';
