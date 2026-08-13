-- Staff tool to resolve "refer" decisions (model score 600-659; the DTI half
-- of that band was retired -- nothing computes a DTI, see adr/0007),
-- policies/underwriting_guidelines.md's manual-review band). There was no
-- way for staff to actually turn a refer into an approve/deny before this --
-- accept_offer already correctly blocked self-accept on anything but
-- "approve", but nothing existed to move a refer OUT of that state. See
-- services/origination-service/app/routers/applications.py's
-- POST /{app_id}/review.
--
-- Kept separate from decision_events (004_decision_events.sql) on purpose:
-- decision_events is the model's own append-only audit trail (model_score,
-- model_version, reason_codes) -- a human override has none of that, and
-- forcing a dummy model_version into that table would misrepresent a staff
-- decision as a model one. This table is its own small, human-decision audit
-- record instead: who reviewed it, what they decided, and why.
-- Review fix (db/migrations/0020): once staff decides, it's final -- no
-- staff member (not even a different one) may change it afterward.
-- app_id is UNIQUE (at most one manual review per application, ever) and
-- reviewer_name identifies the actual person, not just their role.
CREATE TABLE IF NOT EXISTS manual_reviews (
    id            SERIAL PRIMARY KEY,
    app_id        INTEGER NOT NULL REFERENCES applications(id) UNIQUE,
    reviewer_role TEXT NOT NULL,
    reviewer_name TEXT,
    outcome       TEXT NOT NULL,   -- 'approve' | 'deny'
    reason        TEXT NOT NULL,
    reviewed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_manual_reviews_app_id ON manual_reviews(app_id);
