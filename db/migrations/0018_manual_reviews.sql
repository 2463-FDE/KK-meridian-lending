-- 0018 — feature: a staff tool to resolve "refer" decisions (score 600-659
-- or DTI 43-50%, policies/underwriting_guidelines.md's manual-review band).
-- There was no way for staff to actually turn a refer into an approve/deny
-- before this -- accept_offer already correctly blocked self-accept on
-- anything but "approve", but nothing existed to move a refer OUT of that
-- state. See services/origination-service/app/routers/applications.py's
-- new POST /{app_id}/review.
--
-- Kept separate from decision_events (db/migrations/0004) on purpose:
-- decision_events is the model's own append-only audit trail (model_score,
-- model_version, reason_codes) -- a human override has none of that, and
-- forcing a dummy model_version into that table would misrepresent a staff
-- decision as a model one. This table is its own small, human-decision audit
-- record instead: who reviewed it, what they decided, and why.
CREATE TABLE IF NOT EXISTS manual_reviews (
    id            SERIAL PRIMARY KEY,
    app_id        INTEGER NOT NULL REFERENCES applications(id),
    reviewer_role TEXT NOT NULL,
    outcome       TEXT NOT NULL,   -- 'approve' | 'deny'
    reason        TEXT NOT NULL,
    reviewed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_manual_reviews_app_id ON manual_reviews(app_id);
