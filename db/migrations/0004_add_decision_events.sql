-- 0004 — add the append-only decision_events audit trail (Week 3).
-- Hand-tracked, as usual. Authoritative DDL lives in db/init/004_decision_events.sql.
-- Applied 2026-07-14.
--
-- Needed on any existing database whose Postgres volume already existed before this
-- change -- db/init/*.sql only runs automatically on a FRESH volume's first boot, so
-- a persistent-volume deployment created before Week 3 never picks up the new table
-- on its own (review finding: decision-service's /decisions would then fail every
-- request against decision_events, since decide() now requires that insert to
-- succeed -- see app/db.py::transaction() and app/main.py's readiness check).
CREATE TABLE IF NOT EXISTS decision_events (
    id                SERIAL PRIMARY KEY,
    app_id            INTEGER NOT NULL REFERENCES applications(id),
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    requested_amount  DOUBLE PRECISION,
    term_months       INTEGER,
    annual_income     DOUBLE PRECISION,
    bureau_score      INTEGER,
    model_score       DOUBLE PRECISION,
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
