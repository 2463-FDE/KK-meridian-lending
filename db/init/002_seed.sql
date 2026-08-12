-- Meridian Lending — seed data

-- Staff + one borrower login. password_hash is sha256('password') for every seeded user
-- (Halcyon shipped them all with the same demo password and never forced a reset).
INSERT INTO users (username, password_hash, role, display_name, applicant_id) VALUES
  ('admin',       '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8', 'admin',       'Dana Whitfield (VP Lending Ops)', NULL),
  ('underwriter', '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8', 'underwriter', 'Sam Okafor (Underwriting)',       NULL),
  ('csr',         '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8', 'csr',         'Jordan Reyes (Servicing Rep)',    NULL),
  ('maria',       '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8', 'borrower',    'Maria Gonzalez',                  1);

INSERT INTO applicants (id, name, dob, ssn, ein, is_entity, email, phone, address) VALUES
  (1, 'Maria Gonzalez', '1971-03-02', '412-55-9981', NULL, FALSE, 'maria.gonzalez@example.com', '559-555-0118', '118 Larkspur Ave, Fresno, CA 93722'),
  (2, 'Darnell Webb',   '1985-12-09', '501-22-7733', NULL, FALSE, 'd.webb@example.com',         '419-555-0009', '9 Cedar Ct, Toledo, OH 43604'),
  (3, 'Priya Raman',    '1989-07-14', '622-41-0098', NULL, FALSE, 'priya.raman@example.com',    '512-555-0740', '740 Birch St, Austin, TX 78702'),
  (4, 'Travis Booker',  '1992-04-21', '330-90-5512', NULL, FALSE, 'tbooker@example.com',        '901-555-0055', '55 Plum Rd, Memphis, TN 38106'),
  (5, 'Aisha Bello',    '1990-10-30', '447-08-2261', NULL, FALSE, 'aisha.bello@example.com',    '901-555-0012', '12 Quince Way, Memphis, TN 38114'),
  (6, 'Northgate Holdings LLC', NULL, NULL, '47-2210098', TRUE, 'ap@northgateholdings.example', '302-555-0200', '200 Commerce Plaza, Wilmington, DE 19801');
SELECT setval('applicants_id_seq', 6);

INSERT INTO applications (id, applicant_id, amount, term_months, purpose, income, employer, job_title, employment_years, status) VALUES
  (4471, 1, 18000, 48, 'debt_consolidation', 52000, 'Valley Health System', 'RN',               9,  'funded'),
  (5582, 2, 12000, 36, 'auto',               47000, 'Toledo Logistics Co',  'Dispatcher',       4,  'funded'),
  (6011, 3, 15000, 36, 'home_improvement',   84000, 'Lone Star Software',   'Engineer',         6,  'funded'),
  (6012, 4,  9000, 24, 'personal',           31000, 'Bluff City Retail',    'Shift Lead',       2,  'decided'),
  (6013, 5,  8000, 24, 'personal',           29500, 'Memphis Care Partners','CNA',              1,  'decided'),
  (6014, 6, 50000, 60, 'working_capital',   240000, NULL,                   NULL,               NULL,'funded');
SELECT setval('applications_id_seq', 6014);

-- KYC: CIP fields only; the entity (6/6014) cleared with no UBO and no sanctions screen.
-- application_id is populated here, not left NULL. The decision gate accepts a
-- CIP row only for the application it was run for (db/migrations/0032), so a
-- seeded row without it would display as identity evidence in the UI while
-- decisioning refused the same application as unverified -- and only on FRESH
-- databases, since migrated ones are back-filled. That fresh-versus-migrated
-- skew is worse than either behaviour on its own, because it makes a smoke test
-- pass or fail depending on how the database was built (PR #18 review).
--
-- cip_passed is populated here for the same reason, one column later
-- (db/migrations/0033). The gate reads the VERDICT now, and a NULL verdict is a
-- row that does not say -- which it treats as not established, correctly. Seeded
-- rows without it left every seeded application undecidable on a FRESH database
-- and decidable on a migrated one, which is precisely the skew the paragraph
-- above exists to warn about. The migration back-fills; the seed has to state it.
--
-- The values follow kyc-service's own applicant-type rule: an individual needs
-- name, address, dob and ssn; an entity clears on name and address (debt D11).
-- Asserted against that rule in db/tests, not trusted to stay in step by hand.
INSERT INTO kyc_checks (applicant_id, application_id, name_verified, dob_verified, address_verified, ssn_verified, cip_passed) VALUES
  (1, 4471, TRUE, TRUE, TRUE, TRUE, TRUE),
  (2, 5582, TRUE, TRUE, TRUE, TRUE, TRUE),
  (3, 6011, TRUE, TRUE, TRUE, TRUE, TRUE),
  (4, 6012, TRUE, TRUE, TRUE, TRUE, TRUE),
  (5, 6013, TRUE, TRUE, TRUE, TRUE, TRUE),
  -- entity: no real person verified, cleared anyway (D11)
  (6, 6014, TRUE, FALSE, TRUE, FALSE, TRUE);

-- Decisions: outcome only. Denials 6012/6013 have no recorded reason anywhere.
INSERT INTO decisions (app_id, outcome) VALUES
  (4471, 'approve'),
  (5582, 'approve'),
  (6011, 'approve'),
  (6012, 'deny'),
  (6013, 'deny'),
  (6014, 'approve');

-- Offers. Every field recomputed from principal / note rate / term through the
-- same formulas production uses -- these were hand-written literals that
-- satisfied no TILA relationship: amount_financed equalled the principal (no fee
-- deducted at all), the stored `apr` was a stale float value, note_rate_pct and
-- fee_pct_used were absent, and 3 of 4 failed
-- amount_financed + finance_charge = total_of_payments.
--
-- Rounding: payment kept unrounded for the total, monetary fields at 2dp,
-- APR at 3dp, finance charge derived as total - amount_financed so the box
-- cannot fail to foot. Verified by db/tests/test_seed_offer_consistency.py.
--
--   app   note   apr     finance_charge  payment  amount_financed  total
-- BEGIN GENERATED OFFER ROWS (db/tools/regenerate_seed_offers.py)
-- The four curated anchors. Do not hand-edit: regenerate with
-- `python db/tools/regenerate_seed_offers.py --write`.
--
-- These previously carried pre-Model-B values -- 4471's total was 21088.71
-- against an actual 21088.70, and 6011's 16919.15 against 16919.17. A cent
-- or two, on the figures a demo points at to show the disclosure footing.
INSERT INTO offers (app_id, note_rate_pct, fee_pct_used, apr,
                    finance_charge, monthly_payment, amount_financed,
                    total_of_payments, regular_payment_count, final_payment,
                    term_months, schedule_version, principal) VALUES
  (4471, 7.99, 0.0300, 9.584, 3628.70, 439.35, 17460.00, 21088.70, 47, 439.25, 48, 'B1', 18000),
  (5582, 9.99, 0.0300, 12.096, 2297.39, 387.15, 11640.00, 13937.39, 35, 387.14, 36, 'B1', 12000),
  (6011, 7.99, 0.0300, 10.072, 2369.17, 469.98, 14550.00, 16919.17, 35, 469.87, 36, 'B1', 15000),
  (6014, 11.25, 0.0300, 12.590, 17101.83, 1093.37, 48500.00, 65601.83, 59, 1093.00, 60, 'B1', 50000);
-- END GENERATED OFFER ROWS

-- `loans.apr` holds the CONTRACTUAL note rate despite the column name (D19) --
-- it is what servicing/schedule.py::amortization() bills from. 4471 and 6011
-- previously carried 7.142, a stale disclosed-APR value, so amortizing them
-- reproduced neither the disclosed payment nor anything else. They now match
-- their offer's note_rate_pct, which is the invariant
-- db/tests/test_seed_offer_consistency.py enforces for every row.
INSERT INTO loans (id, app_id, applicant_name, principal, apr, term_months, status) VALUES
  (4471, 4471, 'Maria Gonzalez', 18000,  7.990, 48, 'current'),
  (5582, 5582, 'Darnell Webb',   12000,  9.990, 36, 'current'),
  (6011, 6011, 'Priya Raman',    15000,  7.990, 36, 'current'),
  (6014, 6014, 'Northgate Holdings LLC', 50000, 11.250, 60, 'current');
SELECT setval('loans_id_seq', 6014);

-- Balances stored as a single overwritten float.
INSERT INTO balances (loan_id, balance, past_due) VALUES
  (4471, 12200.0, 0),
  (5582, 7999.0, 410.50),   -- note: 5582 was double-applied on 2026-06-01 (see payment log)
  (6011, 13135.64, 0),
  (6014, 49000.0, 0);

-- Payments. Seeds populate no PAN/CVV -- and as of the contract migration
-- (db/migrations/0031) there are no such columns to populate: 001_schema.sql
-- stopped creating them, so a fresh volume never has them and a migrated
-- database no longer does. Nothing wrote them anyway (ADR 0008), and seeding
-- card numbers into a demo database is how one ended up committed in a log file
-- in the first place.
-- 5582 still has TWO rows for one retried charge -- that double-charge is the
-- Week 7 reconciliation scenario and is deliberately preserved.
INSERT INTO payments (loan_id, last4, brand, amount, method, created_at) VALUES
  (4471, '1111', 'visa',       250.00, 'card', '2026-06-01 09:14:11'),
  (5582, '5559', 'mastercard', 410.50, 'card', '2026-06-01 09:31:04'),
  (5582, '5559', 'mastercard', 410.50, 'card', '2026-06-01 09:31:06'),  -- duplicate
  (4471, '0009', 'amex',        99.99, 'card', '2026-06-01 11:18:45'),
  (6011, NULL,   NULL,         432.18, 'ach',  '2026-06-02 08:00:00'),
  (4471, '1111', 'visa',       250.00, 'card', '2026-06-03 09:00:00'),
  (6011, NULL,   NULL,         432.18, 'ach',  '2026-06-03 08:00:00');

-- "audit" entries that are really app logging, not an actor->action->time control trail.
INSERT INTO audit_logs (actor, action, detail) VALUES
  ('system', 'payment', 'charge req last4=1111 amount=250.00'),
  ('rep_jordan', 'waive_fee', 'loan 5582 waived 35.00'),
  ('system', 'decision', 'app 6012 deny');
