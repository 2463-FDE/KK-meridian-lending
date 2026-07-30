-- 0009 — Week 4 review fix: make offer creation idempotent per decision.
-- Hand-tracked, as usual. Authoritative DDL lives in db/init/001_schema.sql.
--
-- Without this, a retried/duplicated create_offer call (timeout retry, double
-- click) inserted a second offer row for the same decision, and every read path
-- (ORDER BY id DESC) silently treated the newest one as authoritative -- with no
-- record of why two offers existed for one decision. NULL is left legal (a
-- Postgres UNIQUE constraint allows any number of NULLs) so legacy offers that
-- predate the decision_id column aren't touched by this migration.

ALTER TABLE offers
    ADD CONSTRAINT offers_decision_id_key UNIQUE (decision_id);
