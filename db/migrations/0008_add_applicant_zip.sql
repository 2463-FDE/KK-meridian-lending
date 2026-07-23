-- 0008 — Week 8 fair-lending fix: add the ZIP field the roadmap flagged as
-- missing entirely ("Can't check" a ZIP-level disparate-impact question --
-- confirmed no ZIP field exists anywhere in the schema; applicants.address is
-- one free-text column). Hand-tracked, as usual. Authoritative DDL lives in
-- db/init/001_schema.sql.
--
-- A separate column, not a restructure of `address` into city/state/zip --
-- address stays free text; zip_code is the one structured field a fairness
-- check actually needs.

ALTER TABLE applicants
    ADD COLUMN IF NOT EXISTS zip_code TEXT;
