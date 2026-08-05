-- Fresh-volume mirror of db/migrations/0023_decision_attempts.sql -- see
-- that file for the full rationale (PR #6 review, Finding 2: attempt/
-- reservation lifecycle so a blocked decision rerun performs no bureau/
-- model work and can never leave a discarded attempt's decision_events row
-- looking like a permanent one). Kept as its own numbered file (rather than
-- folded into 004_decision_events.sql) because decision_attempts must exist
-- before decision_events.attempt_id's FK can be added, and 004/005 are
-- already-numbered files other tests reference by name.
CREATE TABLE IF NOT EXISTS decision_attempts (
    id               SERIAL PRIMARY KEY,
    app_id           INTEGER NOT NULL REFERENCES applications(id),
    state            TEXT NOT NULL,
    requested_by     TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    failure_code     TEXT,
    failure_detail   VARCHAR(200),

    CONSTRAINT decision_attempts_state_allowed
        CHECK (state IN ('in_progress', 'completed', 'discarded', 'failed', 'expired')),

    CONSTRAINT decision_attempts_failure_code_allowed
        CHECK (failure_code IS NULL OR failure_code IN
            ('timeout', 'unavailable', 'invalid_response', 'superseded_by_staff',
             'funded', 'internal_error', 'expired_lease', 'persistence_error')),

    CONSTRAINT decision_attempts_in_progress_shape
        CHECK (state <> 'in_progress' OR (completed_at IS NULL AND lease_expires_at IS NOT NULL)),
    CONSTRAINT decision_attempts_terminal_shape
        CHECK (state = 'in_progress' OR completed_at IS NOT NULL),

    CONSTRAINT decision_attempts_lease_after_start
        CHECK (lease_expires_at IS NULL OR lease_expires_at > started_at),

    CONSTRAINT decision_attempts_completed_has_no_failure
        CHECK (state <> 'completed' OR failure_code IS NULL),
    CONSTRAINT decision_attempts_terminal_failure_states_have_a_reason
        CHECK (state NOT IN ('discarded', 'failed', 'expired') OR failure_code IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_attempts_one_active
    ON decision_attempts (app_id) WHERE state = 'in_progress';
CREATE INDEX IF NOT EXISTS idx_decision_attempts_app_id ON decision_attempts (app_id);

ALTER TABLE decision_events ADD COLUMN IF NOT EXISTS attempt_id INTEGER REFERENCES decision_attempts(id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_events_attempt_id
    ON decision_events (attempt_id) WHERE attempt_id IS NOT NULL;
