-- 0023 -- security/correctness fix (PR #6 review, Finding 2): a blocked
-- decision rerun still performed real side effects before losing the
-- finality race. run_decision's own pre-check (an unlocked read of
-- manual_reviews) caught the common case, but between that read and the
-- external call to decision-service, a staff decision or funding could
-- still commit -- and by the time the existing post-call lock+recheck
-- discarded the result, decision-service had already pulled the credit
-- bureau and durably written its own decision_events row for an attempt
-- that was discarded, leaving a misleading "proposed" audit row after a
-- final staff decision, and a wasted bureau pull.
--
-- Fix: an explicit attempt/reservation lifecycle. origination-service
-- creates a decision_attempts row (short transaction, applications row
-- locked) BEFORE ever calling decision-service -- if funded/manual
-- finality already exists at that point, decision-service is never
-- called at all. decision-service is now compute-only (see
-- services/decision-service/app/graph.py) -- it no longer writes
-- decision_events itself. After it returns, origination-service takes the
-- lock again (short transaction), rechecks finality one more time (the one
-- genuinely-concurrent race this can't close without holding a lock across
-- the network call -- see PR discussion), and only then writes decisions +
-- decision_events + marks the attempt completed, all atomically. A
-- discarded attempt writes neither.
--
-- lease_expires_at recovers a crashed process: if the app-host process
-- dies after creating an in_progress attempt but before completing it, the
-- row would otherwise block every future rerun forever (the partial unique
-- index below allows only one in_progress attempt per application). A
-- later request atomically expires the stale attempt (lease_expires_at
-- in the past) and creates a fresh one, under the same applications lock --
-- no background worker, request-time recovery only.

CREATE TABLE IF NOT EXISTS decision_attempts (
    id               SERIAL PRIMARY KEY,
    app_id           INTEGER NOT NULL REFERENCES applications(id),
    state            TEXT NOT NULL,
    -- Role string only ('borrower' | 'csr' | 'underwriter' | 'admin') --
    -- never a name/email. Sourced server-side from the same X-User-Role /
    -- access_token ownership check run_decision already performs, never
    -- from unvalidated client input.
    requested_by     TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    failure_code     TEXT,
    -- Sanitized, bounded, templated text only -- see decision_state.py's
    -- failure-code/detail constants. Never a raw exception, stack trace,
    -- HTTP response body, bureau response, credential, or applicant field.
    failure_detail   VARCHAR(200),

    CONSTRAINT decision_attempts_state_allowed
        CHECK (state IN ('in_progress', 'completed', 'discarded', 'failed', 'expired')),

    CONSTRAINT decision_attempts_failure_code_allowed
        CHECK (failure_code IS NULL OR failure_code IN
            ('timeout', 'unavailable', 'invalid_response', 'superseded_by_staff',
             'funded', 'internal_error', 'expired_lease', 'persistence_error')),

    -- in_progress must not yet be completed and must carry a lease;
    -- every terminal state must be completed.
    CONSTRAINT decision_attempts_in_progress_shape
        CHECK (state <> 'in_progress' OR (completed_at IS NULL AND lease_expires_at IS NOT NULL)),
    CONSTRAINT decision_attempts_terminal_shape
        CHECK (state = 'in_progress' OR completed_at IS NOT NULL),

    -- A lease, if present, must be after the attempt started -- the closest
    -- a static CHECK can get to "was in the future when created" (actual
    -- staleness is judged by application code comparing to now() at read
    -- time, not by this constraint).
    CONSTRAINT decision_attempts_lease_after_start
        CHECK (lease_expires_at IS NULL OR lease_expires_at > started_at),

    -- completed never carries a failure reason; every OTHER terminal state
    -- (discarded/failed/expired) must carry one -- an attempt that ended
    -- abnormally with no recorded reason is a data-quality gap, not a
    -- valid row.
    CONSTRAINT decision_attempts_completed_has_no_failure
        CHECK (state <> 'completed' OR failure_code IS NULL),
    CONSTRAINT decision_attempts_terminal_failure_states_have_a_reason
        CHECK (state NOT IN ('discarded', 'failed', 'expired') OR failure_code IS NOT NULL)
);

-- Primary guard against two concurrent reruns both starting an attempt is
-- the applications row lock every writer takes first; this partial unique
-- index is the same defense-in-depth backstop pattern manual_reviews.app_id
-- UNIQUE already uses for the same reason (see
-- db/tests/test_decision_single_writer_concurrency.py's own "backstop"
-- test) -- only one attempt may be in flight per application at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_attempts_one_active
    ON decision_attempts (app_id) WHERE state = 'in_progress';
CREATE INDEX IF NOT EXISTS idx_decision_attempts_app_id ON decision_attempts (app_id);

-- Ties every permanent audit row to the exact attempt that produced it --
-- strengthens the Reg B dispute-proof lineage decision_events already
-- exists for. Nullable: historical rows (written by decision-service
-- before this fix) keep NULL forever, and the append-only trigger on
-- decision_events (db/init/004_decision_events.sql) still forbids ever
-- rewriting them to backfill it.
ALTER TABLE decision_events ADD COLUMN IF NOT EXISTS attempt_id INTEGER REFERENCES decision_attempts(id);

-- One attempt can produce at most one permanent decision_events row --
-- partial so historical NULL rows (and any future NULL, though the
-- application code never writes one going forward) don't collide with
-- each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_events_attempt_id
    ON decision_events (attempt_id) WHERE attempt_id IS NOT NULL;
