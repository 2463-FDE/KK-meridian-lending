-- 0006 — Week 4 fix: link an offer back to the decision + fee-rule version that
-- produced it. Hand-tracked, as usual. Authoritative DDL lives in db/init/001_schema.sql.
--
-- Needed on any existing database whose Postgres volume already existed before this
-- change -- db/init/*.sql only runs automatically on a fresh volume's first boot.
--
-- decision_id: FK to decisions(app_id) -- decisions.app_id is the table's own PK, so
-- this is a real constraint: an offer can't be linked to a decision that doesn't exist.
-- fee_pct_used: snapshot of ORIGINATION_FEE_PCT at offer-creation time -- without this,
-- there was no way to prove what fee was actually used for a loan originated before the
-- constant's next change.

ALTER TABLE offers
    ADD COLUMN IF NOT EXISTS decision_id INTEGER REFERENCES decisions(app_id),
    ADD COLUMN IF NOT EXISTS fee_pct_used NUMERIC(5,4);
