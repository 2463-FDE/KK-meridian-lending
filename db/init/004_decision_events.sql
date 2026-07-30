-- Append-only decision audit trail (Week 3). Replaces the outcome-only `decisions`
-- table's blind spot: one row per decision run, with the inputs, the model score/
-- version, which factors drove it, and the emitted reason codes -- the dispute-proof
-- record Reg B/ECOA requires and the brief's own "if a denied applicant disputes the
-- reason, what proves why" question found missing.
--
-- No UPDATE/DELETE grants: enforced with a trigger rather than table-level GRANTs,
-- since every service in this project connects as the same schema-owning role
-- (ADR 0002 shared database) -- a plain REVOKE doesn't bind the owner. The trigger
-- rejects UPDATE/DELETE unconditionally regardless of which role issues them.
CREATE TABLE IF NOT EXISTS decision_events (
    id                SERIAL PRIMARY KEY,
    app_id            INTEGER NOT NULL REFERENCES applications(id),
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    requested_amount  NUMERIC(14,2),   -- D12: was DOUBLE PRECISION
    term_months       INTEGER,
    annual_income     NUMERIC(14,2),   -- D12: was DOUBLE PRECISION
    bureau_score      INTEGER,
    model_score       DOUBLE PRECISION,  -- a scoring value, not money -- left as-is
    model_version     TEXT NOT NULL,
    top_features      JSONB,
    decision          TEXT NOT NULL,
    reason_codes      JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_decision_events_app_id ON decision_events(app_id);
CREATE INDEX IF NOT EXISTS idx_decision_events_occurred_at ON decision_events(occurred_at);

CREATE OR REPLACE FUNCTION reject_decision_events_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'decision_events is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS decision_events_no_update ON decision_events;
CREATE TRIGGER decision_events_no_update
    BEFORE UPDATE OR DELETE ON decision_events
    FOR EACH ROW EXECUTE FUNCTION reject_decision_events_mutation();
