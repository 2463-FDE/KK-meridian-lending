-- Meridian Lending — bulk seed (synthetic portfolio for a realistic dashboard).
--
-- Generates ~300 applicants, ~300 applications, 300 decisions, ~180 funded loans with
-- balances + offers, and ~600 payments. Money is DOUBLE PRECISION throughout (same float
-- debt as the rest of the platform). IDs start at 100 (applicants) / 7000 (apps+loans) so
-- they never collide with the hand-curated anchor rows in 002_seed.sql (1..6 / 4471..6014).

-- 300 borrowers (ids 100..399)
INSERT INTO applicants (id, name, dob, ssn, email, phone, is_entity, address)
SELECT g,
  (ARRAY['James','Mary','Robert','Patricia','John','Jennifer','Michael','Linda','David','Elizabeth','William','Barbara','Richard','Susan','Joseph','Jessica','Thomas','Karen','Charles','Nancy'])[1 + (g % 20)]
    || ' ' ||
  (ARRAY['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Rodriguez','Martinez','Hernandez','Lopez','Gonzalez','Wilson','Anderson','Thomas','Taylor','Moore','Jackson','Martin'])[1 + ((g * 7) % 20)],
  (DATE '1960-01-01' + ((g * 97) % 14000)),
  lpad(((g * 131) % 900 + 100)::text, 3, '0') || '-' || lpad(((g * 17) % 90 + 10)::text, 2, '0') || '-' || lpad(((g * 53) % 9000 + 1000)::text, 4, '0'),
  'user' || g || '@example.com',
  '555-01' || lpad((g % 100)::text, 2, '0'),
  FALSE,
  ((g * 13) % 900 + 100)::text || ' ' || (ARRAY['Oak','Maple','Cedar','Pine','Elm','Birch','Walnut','Spruce'])[1 + (g % 8)] || ' St, ' ||
    (ARRAY['Fresno, CA 93722','Toledo, OH 43604','Austin, TX 78702','Memphis, TN 38106','Akron, OH 44303','Mesa, AZ 85201','Tulsa, OK 74103','Omaha, NE 68102'])[1 + ((g * 3) % 8)]
FROM generate_series(100, 399) g;
SELECT setval('applicants_id_seq', 399);

-- 300 applications (ids 7000..7299), applicant_id = 100 + (id - 7000)
INSERT INTO applications (id, applicant_id, amount, term_months, purpose, income, employer, job_title, employment_years, status)
SELECT g,
  100 + (g - 7000),
  (1000 + ((g * 263) % 49000))::double precision,
  (ARRAY[12,24,36,48,60])[1 + ((g * 3) % 5)],
  (ARRAY['debt_consolidation','home_improvement','auto','medical','personal','other'])[1 + ((g * 7) % 6)],
  (24000 + ((g * 311) % 180000))::double precision,
  (ARRAY['Acme Corp','Globex','Initech','Umbrella Co','Hooli','Stark Industries','Wayne Enterprises','Soylent Inc'])[1 + ((g * 5) % 8)],
  (ARRAY['Analyst','Manager','Technician','Clerk','Engineer','Driver','Nurse','Teacher'])[1 + ((g * 11) % 8)],
  ((g % 15) + 1)::double precision,
  (ARRAY['funded','funded','funded','decided','submitted'])[1 + ((g * 2) % 5)]
FROM generate_series(7000, 7299) g;
SELECT setval('applications_id_seq', 7299);

-- A decision row for every application (outcome only — the audit-trail debt is preserved).
INSERT INTO decisions (app_id, outcome)
SELECT g, (ARRAY['approve','approve','approve','deny','refer'])[1 + ((g * 2) % 5)]
FROM generate_series(7000, 7299) g;

-- Loans for every FUNDED application (loan id = app id, mirroring the anchor convention).
INSERT INTO loans (id, app_id, applicant_name, principal, apr, term_months, status)
SELECT a.id, a.id, ap.name, a.amount,
  round((7.99 + (a.id % 16))::numeric, 3)::double precision,
  a.term_months,
  (ARRAY['current','current','current','delinquent','paid_off'])[1 + ((a.id * 5) % 5)]
FROM applications a JOIN applicants ap ON ap.id = a.applicant_id
WHERE a.id BETWEEN 7000 AND 7299 AND a.status = 'funded';
SELECT setval('loans_id_seq', 7299);

-- Offers for those funded loans.
--
-- Every field is DERIVED, never a multiplier. The previous version wrote
--   finance_charge  = principal * 0.16
--   monthly_payment = principal / term_months * 1.1
--   amount_financed = principal * 0.97
--   total_of_payments = principal * 1.16
-- which satisfied no TILA relationship at all: 180 of 180 rows failed
-- amount_financed + finance_charge = total_of_payments (worst error -1498.74),
-- and the stored `apr` was the loan's note rate, not an APR of anything.
--
-- Rate source. `loans.apr` holds the CONTRACTUAL note rate despite its name --
-- it is the value board_to_servicing() writes and the value
-- servicing/schedule.py::amortization() consumes to bill the borrower. The
-- column rename is tracked as D19; this is the historical name, not a new
-- claim about it. db/tests/test_seed_offer_consistency.py asserts the semantics
-- independently rather than trusting this comment.
--
-- Rounding boundary, fixed deliberately:
--   * the payment is kept UNROUNDED for the total-of-payments multiplication
--     (rounding first shifts the total by cents and breaks the identity),
--   * monetary fields persist at 2dp, ROUND HALF UP (Postgres NUMERIC default),
--   * the disclosed APR is 3dp,
--   * zero monthly rate uses principal/term, never the amortization formula.
--
-- APR source. The actuarial APR is a pure function of (fee, note rate, term):
-- amount financed and payment both scale linearly in principal, so principal
-- cancels out of the present-value equation (verified to 46 decimal places over
-- a 48x principal range). The 48 combinations these seeds use are therefore
-- enumerable, and are listed below rather than solved in SQL -- a second APR
-- implementation in seed SQL would be a second thing to get wrong. Each value
-- was generated offline and cross-checked three ways: bisection,
-- Newton-Raphson, and disclosure-service's own compute_apr(). All 48 agree to
-- 3dp with deviation 0. The consistency test re-derives every one of them from
-- the seeded payment stream on each CI run, so a stale literal fails the build.
--
-- REGENERATING THESE. If the origination fee changes, or a new note rate or
-- term appears in the seeds, run:
--
--     python db/tools/regenerate_seed_apr_lookup.py
--
-- and paste its output over the VALUES rows below. That script cross-checks
-- every value three ways and exits non-zero rather than emitting one the
-- methods disagree on. Then rebuild and re-run the consistency suite:
--
--     docker compose down -v && docker compose up -d --build
--     python -m pytest db/tests/test_seed_offer_consistency.py -q
--
-- Do NOT approximate a missing combination: the DO block at the end of this
-- file raises rather than seeding fewer offers, which is the intended
-- behaviour -- a demo with holes in it looks like working software.
WITH apr_lookup (fee_pct, note_rate_pct, term_months, apr) AS (VALUES
    (0.030, 7.99, 12, 13.760),
    (0.030, 7.99, 48, 9.584),
    (0.030, 7.99, 60, 9.288),
    (0.030, 8.99, 12, 14.773),
    (0.030, 8.99, 48, 10.596),
    (0.030, 8.99, 60, 10.301),
    (0.030, 9.99, 12, 15.787),
    (0.030, 9.99, 48, 11.609),
    (0.030, 9.99, 60, 11.314),
    (0.030, 10.99, 12, 16.801),
    (0.030, 10.99, 48, 12.621),
    (0.030, 10.99, 60, 12.326),
    (0.030, 11.99, 12, 17.815),
    (0.030, 11.99, 48, 13.634),
    (0.030, 11.99, 60, 13.339),
    (0.030, 12.99, 12, 18.829),
    (0.030, 12.99, 48, 14.647),
    (0.030, 12.99, 60, 14.353),
    (0.030, 13.99, 12, 19.843),
    (0.030, 13.99, 48, 15.660),
    (0.030, 13.99, 60, 15.366),
    (0.030, 14.99, 12, 20.857),
    (0.030, 14.99, 48, 16.673),
    (0.030, 14.99, 60, 16.380),
    (0.030, 15.99, 12, 21.871),
    (0.030, 15.99, 48, 17.687),
    (0.030, 15.99, 60, 17.394),
    (0.030, 16.99, 12, 22.885),
    (0.030, 16.99, 48, 18.700),
    (0.030, 16.99, 60, 18.408),
    (0.030, 17.99, 12, 23.899),
    (0.030, 17.99, 48, 19.714),
    (0.030, 17.99, 60, 19.422),
    (0.030, 18.99, 12, 24.913),
    (0.030, 18.99, 48, 20.728),
    (0.030, 18.99, 60, 20.436),
    (0.030, 19.99, 12, 25.927),
    (0.030, 19.99, 48, 21.742),
    (0.030, 19.99, 60, 21.451),
    (0.030, 20.99, 12, 26.941),
    (0.030, 20.99, 48, 22.756),
    (0.030, 20.99, 60, 22.466),
    (0.030, 21.99, 12, 27.955),
    (0.030, 21.99, 48, 23.770),
    (0.030, 21.99, 60, 23.481),
    (0.030, 22.99, 12, 28.969),
    (0.030, 22.99, 48, 24.785),
    (0.030, 22.99, 60, 24.496)
), priced AS (
    SELECT l.app_id,
           l.principal::numeric                       AS principal,
           l.term_months,
           l.apr::numeric                             AS note_rate_pct,
           0.030::numeric                             AS fee_pct,
           CASE WHEN l.apr::numeric = 0
                THEN l.principal::numeric / l.term_months
                ELSE l.principal::numeric
                     * (l.apr::numeric / 100 / 12)
                     * power(1 + l.apr::numeric / 100 / 12, l.term_months)
                     / (power(1 + l.apr::numeric / 100 / 12, l.term_months) - 1)
           END                                        AS payment_unrounded
    FROM loans l
    WHERE l.id BETWEEN 7000 AND 7299
)
INSERT INTO offers (app_id, note_rate_pct, fee_pct_used, apr,
                    finance_charge, monthly_payment, amount_financed, total_of_payments)
SELECT p.app_id,
       p.note_rate_pct,
       p.fee_pct,
       k.apr,
       -- finance charge is DERIVED from the identity, so it cannot fail to foot
       round(p.payment_unrounded * p.term_months, 2)
         - round(p.principal - p.principal * p.fee_pct, 2)          AS finance_charge,
       round(p.payment_unrounded, 2)                                AS monthly_payment,
       round(p.principal - p.principal * p.fee_pct, 2)              AS amount_financed,
       round(p.payment_unrounded * p.term_months, 2)                AS total_of_payments
FROM priced p
JOIN apr_lookup k
  ON k.fee_pct = p.fee_pct
 AND k.note_rate_pct = p.note_rate_pct
 AND k.term_months = p.term_months;

-- Fail loudly rather than silently seeding fewer offers: an INNER JOIN above
-- would just drop any (fee, rate, term) combination missing from the lookup,
-- and a demo with holes in it looks like working software.
DO $$
DECLARE
    expected int;
    got      int;
BEGIN
    SELECT count(*) INTO expected FROM loans WHERE id BETWEEN 7000 AND 7299;
    SELECT count(*) INTO got FROM offers WHERE app_id BETWEEN 7000 AND 7299;
    IF got <> expected THEN
        RAISE EXCEPTION
          'seed: % funded loans but only % offers -- a (fee, note rate, term) '
          'combination has no verified APR in apr_lookup. Add it rather than '
          'approximating: see the header comment for how the values are produced.',
          expected, got;
    END IF;
END $$;

-- Balances: single mutable float column (no ledger — debt preserved).
INSERT INTO balances (loan_id, balance, past_due)
SELECT l.id,
  round((l.principal * (0.30 + ((l.id % 60) / 100.0)))::numeric, 2)::double precision,
  CASE WHEN l.status = 'delinquent' THEN round((50 + (l.id % 400))::numeric, 2)::double precision ELSE 0 END
FROM loans l WHERE l.id BETWEEN 7000 AND 7299;

-- 1..6 payments per loan (~600 rows). Card rows carry last4/brand only. Seeds
-- no longer populate PAN/CVV. The nullable legacy columns remain until PR #15's
-- contract migration (db/migrations/0031); nothing writes them (ADR 0008).
INSERT INTO payments (loan_id, last4, brand, amount, method, created_at)
SELECT l.id,
  CASE WHEN (l.id + s) % 3 = 0 THEN NULL ELSE '1111' END,
  CASE WHEN (l.id + s) % 3 = 0 THEN NULL ELSE 'visa' END,
  round((l.principal / l.term_months)::numeric, 2)::double precision,
  CASE WHEN (l.id + s) % 3 = 0 THEN 'ach' ELSE 'card' END,
  TIMESTAMPTZ '2026-05-01 09:00:00' + ((l.id % 20) || ' days')::interval + (s || ' days')::interval
FROM loans l CROSS JOIN LATERAL generate_series(1, 1 + (l.id % 5)) AS s
WHERE l.id BETWEEN 7000 AND 7299;
