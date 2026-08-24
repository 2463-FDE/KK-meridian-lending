-- 0014 — a structured ZIP on `applicants`, kept as normalised postal-address
-- data. Hand-tracked, as usual. Authoritative DDL lives in db/init/001_schema.sql.
--
-- A separate column, not a restructure of `address` into city/state/zip --
-- address stays free text, and this is the one part of it worth normalising:
-- an address without a consistent ZIP is an incomplete address.
--
-- **Why the original reason no longer applies, kept because the reversal is the
-- point.** This migration was written for a Week 8 ZIP3 fair-lending screen --
-- the roadmap had flagged that a ZIP-level disparate-impact question could not
-- be asked at all, since `applicants.address` was one free-text column. The
-- client prohibited ZIP and ZIP3 as a protected-class proxy on 2026-08-24, that
-- screen is retired (`specs/0003-fair-lending-monitoring.md` § *Superseding
-- authority*), and no runtime path groups decisions by this column or any
-- truncation of it -- `db/tests/test_no_runtime_protected_class_proxy.py` fails
-- if one appears.
--
-- The column stays. Dropping it would lose real address data to make a point
-- about a screen that no longer exists.

ALTER TABLE applicants
    ADD COLUMN IF NOT EXISTS zip_code TEXT;
