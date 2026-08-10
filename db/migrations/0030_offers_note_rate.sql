-- 0030 -- separate the contractual note rate from the disclosed APR.
--
-- PR #10 review, high. Making `compute_apr` return the true actuarial APR
-- changed what `offers.apr` MEANS without changing who reads it. Origination's
-- accept path takes `offers.apr` as `rate` and hands it to
-- `board_to_servicing_tx`, which writes it to `loans.apr`; servicing then
-- amortizes the loan at that value
-- (`servicing-service/app/routers/loans.py::loan_schedule`).
--
-- Before the APR fix that was already wrong -- it amortized at the old add-on
-- ratio, 5.196%, and produced payments BELOW the disclosed 439.35. The APR fix
-- flipped the error to 9.584%, which produces 452.94, i.e. a borrower billed
-- MORE per month than the disclosure they signed. Same defect, worse direction:
-- 13.59 a month, 652 over a 48-month term.
--
-- The two numbers are not interchangeable and never were:
--
--   note rate  -- what the payment schedule is calculated on. 7.99%.
--   APR        -- the all-in cost solved against the amount financed, which is
--                 higher than the note rate whenever a prepaid fee exists.
--
-- So `offers.apr` keeps its correct new meaning (the disclosed APR, which is
-- what Reg Z requires on the disclosure) and the contractual rate gets its own
-- column, which is what boarding reads.

ALTER TABLE offers
    ADD COLUMN IF NOT EXISTS note_rate_pct NUMERIC(7,3);

-- Back-fill, from a per-row source or not at all.
--
-- An earlier version of this migration set EVERY existing offer to 7.99, on the
-- reasoning that create_offer had only ever written that one literal. That
-- reasoning was wrong about the data it would actually meet: `db/init`'s seeds
-- are part of that data, and they carry other rates -- 003_seed_bulk.sql
-- generates `7.99 + (id % 16)`, i.e. 7.99 through 22.99, and 002_seed.sql
-- shipped 9.99% and 11.25% loans. On an upgraded database this constant would
-- have overwritten those with a rate the borrower was never given, and the new
-- UI presents note_rate_pct as a stored contractual FACT. Review finding on
-- PR #10; a false rate on a disclosure is the defect this PR exists to fix,
-- not one it may introduce.
--
-- The only per-row source is the boarded loan, and it has to be PROVEN rather
-- than trusted. `loans.apr` is a legacy misnomer that has held two different
-- things over this system's life:
--
--   * loans boarded by the pre-change acceptance path got `offers.apr` copied
--     into it -- the DISCLOSED APR, which is higher than the note rate whenever
--     a prepaid fee exists. The $18,000/48-month seed offer boarded
--     `loans.apr = 5.196` against a contractual 7.99%. Reading that as the note
--     rate would write the exact APR/note-rate conflation this migration exists
--     to end, and the UI would then present it as a stored contractual fact.
--   * seeded loans carry the note rate there, because the seeds were generated
--     from it.
--
-- The two are indistinguishable by provenance on an upgraded database, so this
-- distinguishes them ARITHMETICALLY: the value is accepted only if amortizing
-- the loan's principal at that rate over its term reproduces the offer's stored
-- monthly payment. A note rate does; an APR does not, because the APR is solved
-- against the amount financed rather than against the principal the payments
-- actually run on. Review finding on PR #10.
--
-- Two conditions, and the second is the one that makes this sound.
--
-- (1) AGREEMENT, to half a cent. A genuine note rate reproduces its own stored
--     payment to the cent, because the payment was computed from it and then
--     rounded. An absolute $0.02 window was too wide: a $100 12-month loan
--     priced at 7.99% stores an $8.70 payment, and amortizing at an old
--     disclosed APR of 7.609% gives $8.681 -- inside $0.02, so the APR would
--     have been certified as the note rate.
--
-- (2) SEPARABILITY. Agreement alone is not evidence when the row cannot tell
--     two different rates apart in the first place. Tightening the window does
--     not fix that, because the payment gap between an APR and a note rate
--     shrinks with the payment itself: a $5 loan over 84 months stores $0.08,
--     and its disclosed APR of 8.925% reproduces $0.0803 -- inside half a cent,
--     so the tighter window certifies it too. Eleven such rows exist in a
--     principal x term grid of otherwise ordinary inputs, so this is reachable
--     by scaling the principal down, exactly as the review said.
--
--     So the row must also be sensitive enough for the match to mean something:
--     moving the rate by 0.125 percentage points -- the Reg-Z APR tolerance of
--     12 CFR 1026.22(a)(1), used here as "the smallest rate difference this
--     system treats as a real difference" -- must move the computed payment by
--     more than half a cent. On $15,000/36mo that shifts the payment by $0.87
--     and the match is informative; on $5/84mo it shifts it by $0.0003, so the
--     stored cent is compatible with a wide band of rates and provenance is
--     simply not recoverable. Those rows stay NULL, which reads downstream as
--     "not recorded" rather than as a certified fact.
--
--     This is scale-free: it asks about the row's own resolving power instead of
--     comparing dollars against a fixed threshold. Reviewed on PR #10.
WITH candidate AS (
    SELECT o.id                                        AS offer_id,
           o.monthly_payment                           AS stored_payment,
           l.apr                                       AS candidate_rate,
           -- the payment this rate implies
           CASE
             WHEN l.apr = 0 THEN l.principal / l.term_months
             ELSE (l.principal * (l.apr / 100 / 12))
                  / (1 - power(1 + (l.apr / 100 / 12), -l.term_months))
           END                                         AS payment_at_rate,
           -- and the payment 0.125pp away, to measure this row's resolving power
           (l.principal * ((l.apr + 0.125) / 100 / 12))
             / (1 - power(1 + ((l.apr + 0.125) / 100 / 12), -l.term_months))
                                                       AS payment_at_rate_plus
      FROM offers o
      JOIN loans  l ON l.app_id = o.app_id   -- offers.app_id is UNIQUE: at most one row per loan
     WHERE o.note_rate_pct IS NULL
       AND l.apr IS NOT NULL
       AND l.principal IS NOT NULL
       AND l.principal > 0
       AND l.term_months IS NOT NULL
       AND l.term_months > 0
       AND o.monthly_payment IS NOT NULL
)
UPDATE offers o
   SET note_rate_pct = c.candidate_rate
  FROM candidate c
 WHERE o.id = c.offer_id
   AND abs(c.payment_at_rate - c.stored_payment) <= 0.005          -- (1) agreement
   AND abs(c.payment_at_rate_plus - c.payment_at_rate) > 0.005;    -- (2) separability

-- Everything else stays NULL on purpose. An unboarded legacy offer has no
-- second record of what it was priced at, and there is no way to recover it
-- that is not a guess:
--
--   * a constant would assert a rate for rows that demonstrably had others;
--   * solving the rate from the stored payment would look more rigorous and be
--     less honest, since it would manufacture a plausible per-row rate for any
--     row whose payment was itself written wrong.
--
-- NULL is readable by everything downstream: `accept_offer` refuses to board a
-- row without a stored note rate, and the offer endpoint reports it as absent
-- rather than inventing one. The row can be regenerated through POST /offers,
-- which produces a NEW disclosure at today's rate and an audit_logs entry
-- saying so -- which is the honest way to give a legacy offer a rate.

DO $$
DECLARE
    unset_rows INTEGER;
BEGIN
    SELECT count(*) INTO unset_rows FROM offers WHERE note_rate_pct IS NULL;
    IF unset_rows = 0 THEN
        RAISE NOTICE '0030: every offer now records the note rate it was written at.';
    ELSE
        RAISE WARNING '0030: % offer(s) still have no note_rate_pct -- accept will refuse to board them.', unset_rows;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Contractual payment schedule, persisted as fact (Model B)
-- ---------------------------------------------------------------------------
-- Extends this migration rather than adding a new one. 0030 is unmerged and has
-- never been deployed anywhere (it exists only on kalab-actuarial-apr-fix), and
-- 0031 is already taken by PR #15's DROP COLUMN. Adding 0032 while 0031 is still
-- open would land a HIGHER number first and then a lower one, which
-- version-tracked replay cannot express safely. The note-rate correction and the
-- schedule-fact correction are also one semantic change: both exist because the
-- disclosed APR and the contractual terms were being conflated.
--
-- WHY PERSIST THE SCHEDULE AT ALL
-- The read path used to rebuild it: principal recovered as
-- amount_financed / (1 - fee_pct), term as total_of_payments / monthly_payment,
-- then the schedule regenerated by whatever generator was deployed at read time.
-- So an accepted disclosure was not a stored fact -- a later rounding-policy or
-- fee change silently re-derived different contractual terms for a loan somebody
-- had already signed. Under Model B the final payment differs from the regular
-- one, and it cannot be recovered from any stored figure at all.
--
-- schedule_version identifies the rounding policy that produced the row, so a
-- future policy change is distinguishable rather than retroactive. 'B1' is
-- level cent-rounded regular payments with the final period billing remaining
-- principal plus that period's interest.

ALTER TABLE offers
    ADD COLUMN IF NOT EXISTS regular_payment_count INTEGER,
    ADD COLUMN IF NOT EXISTS final_payment         NUMERIC(14,2),
    ADD COLUMN IF NOT EXISTS term_months           INTEGER,
    ADD COLUMN IF NOT EXISTS schedule_version      TEXT,
    -- The principal the schedule was calculated on. Stored because it CANNOT be
    -- recovered from the other stored figures: amount_financed is rounded to
    -- cents, so inverting it through the fee (`amount_financed / (1 - fee_pct)`)
    -- lands on a different principal than the one the payments came from -- a
    -- $1,002.50 loan stores $972.43 and inverts to $1,002.51, whose regenerated
    -- final row is $24.39 against the disclosed $24.37. The GET endpoint was
    -- doing exactly that, so a borrower could be shown a schedule that
    -- contradicted the disclosure printed above it. Review finding on PR #10.
    ADD COLUMN IF NOT EXISTS principal             NUMERIC(14,2);

-- Acceptance back-fill for rows boarded before 0021.
--
-- 0021 added `offers.accepted_at` without back-filling, so an offer boarded
-- before it has a loan and a NULL accepted_at. Every guard that asks "has this
-- offer been accepted?" reads that column -- including the repair path, which
-- refuses to touch an accepted offer. Left as-is, an authorised POST /offers
-- retry could rewrite the monetary and contractual terms of an offer somebody
-- has already been funded against. Review finding on PR #10.
--
-- `loans.opened_at` is the closest true record of when the offer was accepted;
-- it is used rather than now() so the column does not claim the acceptance
-- happened during this migration. A loan with a NULL opened_at still gets a
-- non-null marker, because the fact being recorded is "this was accepted", and
-- the timestamp is secondary to the guard reading IS NOT NULL.
UPDATE offers o
   SET accepted_at = COALESCE(l.opened_at, now())
  FROM loans l
 WHERE l.app_id = o.app_id
   AND o.accepted_at IS NULL;

-- The contract as boarded. Servicing must bill these amounts, not recompute them
-- from principal/rate/term -- that recomputation is exactly what would drift.
ALTER TABLE loans
    ADD COLUMN IF NOT EXISTS regular_payment       NUMERIC(14,2),
    ADD COLUMN IF NOT EXISTS regular_payment_count INTEGER,
    ADD COLUMN IF NOT EXISTS final_payment         NUMERIC(14,2),
    ADD COLUMN IF NOT EXISTS schedule_version      TEXT;

-- Deliberately NO back-fill for any of the above.
--
-- The exact contractual schedule of an already-accepted historical offer was
-- never stored. Reconstructing it with today's generator would persist invented
-- terms as though they were the agreed ones -- the same defect as the read-path
-- regeneration, made permanent. NULL means "not recorded", which is a true
-- statement, and the code treats it as such:
--
--   * new offers always store the complete schedule;
--   * an UNACCEPTED legacy offer missing schedule terms cannot board (it can be
--     regenerated through the audited repair path instead);
--   * an ACCEPTED legacy offer stays immutable, still displays its stored
--     four-box disclosure, and reports the detailed schedule as unavailable
--     rather than regenerating it.
--
-- Contrast with note_rate_pct above, which IS back-filled: every offer this
-- system ever wrote used a single hard-coded 7.99%, so that recovers a known
-- constant. No such constant exists for a per-row final payment.

DO $$
DECLARE
    unpinned INTEGER;
BEGIN
    SELECT count(*) INTO unpinned FROM offers WHERE final_payment IS NULL;
    IF unpinned > 0 THEN
        RAISE NOTICE
          '0030: % offer(s) have no stored payment schedule (legacy). They keep '
          'their four-box disclosure; boarding refuses them and the detailed '
          'schedule reports as unavailable.', unpinned;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Constraints: make "partly recorded" unrepresentable
-- ---------------------------------------------------------------------------
-- Application code checks these already, and that is not the same thing. The
-- application is one of several writers (seed SQL, the repair path, migrations,
-- and any operator with psql), and a half-written schedule is worse than none:
-- a row with a final_payment but no count reads as "recorded" to a NULL check
-- while describing no schedule anybody can bill. Every rule below is a fact
-- about what a Model B schedule IS, so the database is where it belongs.
--
-- All are validated immediately rather than NOT VALID. That is safe precisely
-- because 0030 does not back-fill: every pre-existing row has all four columns
-- NULL and so satisfies the all-null branch. A NOT VALID constraint here would
-- buy nothing and leave a second step for someone to forget.
--
-- Guarded by pg_constraint lookups because ALTER TABLE ... ADD CONSTRAINT has
-- no IF NOT EXISTS form for CHECK, and this file has to stay re-runnable.
--
-- Every guard filters on the table AND current_schema(). conname is unique only
-- per table, so matching on the name alone means any constraint of that name
-- anywhere in the database makes this migration skip silently and leave the
-- table unprotected. 0026 carries the same warning because it shipped that bug
-- once; these guards were written with it and caught by
-- test_legacy_upgrade_reaches_the_same_shape_as_a_fresh_install[checks], which
-- compares both provisioning paths inside one database and so reproduces the
-- cross-schema collision exactly.

-- Pre-0011 offers get their decision_id here, while it is still possible.
--
-- A row written before migration 0011 has `app_id` set and `decision_id` NULL.
-- disclosure-service recovers that link at runtime by joining the offer's own
-- app_id to decisions.app_id -- but it cannot do so for an ACCEPTED row that is
-- also a partial contract, because the CHECK added at the end of this file is
-- installed NOT VALID and PostgreSQL enforces a NOT VALID check on every
-- subsequent UPDATE, including an update that touches only unrelated columns.
-- The stamp would raise a check violation instead of adopting the offer, and the
-- offer would stay unreachable -- exactly the state the runtime fix exists to
-- end. Reviewed on PR #10.
--
-- So it is done now: before the constraint exists, when an UPDATE of one column
-- is still just an UPDATE of one column. Only where an approved decision exists,
-- and it changes no agreed figure, so it is safe on accepted rows.
UPDATE offers o
   SET decision_id = d.app_id
  FROM decisions d
 WHERE d.app_id = o.app_id
   AND d.outcome = 'approve'
   AND o.decision_id IS NULL;

-- Partial contracts, demoted before the constraint that forbids them.
--
-- The all-or-nothing CHECK below covers SIX columns, not four: `principal` and
-- `note_rate_pct` belong to the set, because expanding a stored schedule needs
-- the principal the payments run on and the rate they were priced at. A row
-- holding the other four without those two satisfied every single-column NULL
-- check in the codebase, so the read path called it "contract" and filled the
-- gaps with an inverted principal and a rate recovered from an already-rounded
-- payment: inferred numbers, displayed as agreed terms, with the estimate caveat
-- suppressed precisely because the row looked stored.
--
-- Widening the constraint makes those rows illegal, so they are dealt with
-- first, and only in the one direction that invents nothing: an UNACCEPTED
-- partial row is demoted to legacy (all six cleared), which is what it already
-- was in substance. The old values are written into audit_logs first, so the
-- demotion is recoverable and nothing disappears quietly. Completing them
-- instead would mean solving terms today and filing them as the terms that were
-- agreed, which is the whole failure this migration exists to end.
--
-- ACCEPTED partial rows are left exactly as they are -- an accepted disclosure
-- is immutable, and this migration does not get to edit one. See the VALIDATE
-- step after the constraint for what happens if any exist.
INSERT INTO audit_logs (actor, action, detail)
SELECT 'db/migrations/0030', 'offer.partial_contract_demoted',
       'offer_id=' || o.id || ' app_id=' || coalesce(o.app_id::text, 'null')
       || ' cleared: principal=' || coalesce(o.principal::text, 'null')
       || ' note_rate_pct=' || coalesce(o.note_rate_pct::text, 'null')
       || ' regular_payment_count=' || coalesce(o.regular_payment_count::text, 'null')
       || ' final_payment=' || coalesce(o.final_payment::text, 'null')
       || ' term_months=' || coalesce(o.term_months::text, 'null')
       || ' schedule_version=' || coalesce(o.schedule_version, 'null')
  FROM offers o
 WHERE o.accepted_at IS NULL
   AND num_nonnulls(o.regular_payment_count, o.final_payment, o.term_months,
                    o.schedule_version, o.principal, o.note_rate_pct)
       NOT IN (0, 6);

UPDATE offers o
   SET regular_payment_count = NULL,
       final_payment         = NULL,
       term_months           = NULL,
       schedule_version      = NULL,
       principal             = NULL,
       note_rate_pct         = NULL
 WHERE o.accepted_at IS NULL
   AND num_nonnulls(o.regular_payment_count, o.final_payment, o.term_months,
                    o.schedule_version, o.principal, o.note_rate_pct)
       NOT IN (0, 6);

DO $$
BEGIN
    -- All-or-nothing. The six columns describe one schedule; any proper subset
    -- of them describes nothing. This is the constraint that makes every NULL
    -- check elsewhere in the codebase sound: code may test one column and
    -- conclude about the group.
    --
    -- Dropped and re-added rather than skipped when present, because an earlier
    -- release of this migration created the same constraint NAME over only four
    -- of the columns. A plain IF NOT EXISTS would find that name, decide the
    -- table was protected, and leave the two-column hole open on every database
    -- that had already run it -- the exact silent-skip failure the guards in
    -- this file were written to avoid. Reviewed on PR #10.
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'offers'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'offers_schedule_all_or_nothing'
          AND pg_get_constraintdef(c.oid) NOT LIKE '%principal%'
    ) THEN
        ALTER TABLE offers DROP CONSTRAINT offers_schedule_all_or_nothing;
        RAISE NOTICE '0030: replacing the 4-column offers schedule CHECK with the 6-column one';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'offers'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'offers_schedule_all_or_nothing'
    ) THEN
        -- NOT VALID, then validated separately below. An accepted partial row
        -- cannot be demoted and cannot be completed, so a plain ADD CONSTRAINT
        -- would abort the whole migration on a database that has one -- leaving
        -- it with no constraint at all, which is strictly worse than one that
        -- governs every future write.
        ALTER TABLE offers ADD CONSTRAINT offers_schedule_all_or_nothing CHECK (
            (regular_payment_count IS NULL
             AND final_payment      IS NULL
             AND term_months        IS NULL
             AND schedule_version   IS NULL
             AND principal          IS NULL
             AND note_rate_pct      IS NULL)
            OR
            (regular_payment_count IS NOT NULL
             AND final_payment      IS NOT NULL
             AND term_months        IS NOT NULL
             AND schedule_version   IS NOT NULL
             AND principal          IS NOT NULL
             AND note_rate_pct      IS NOT NULL)
        ) NOT VALID;
    END IF;

    -- Now try to make it cover the existing rows too. It succeeds whenever the
    -- demotion above cleared everything, which is every database without an
    -- accepted partial offer.
    BEGIN
        ALTER TABLE offers VALIDATE CONSTRAINT offers_schedule_all_or_nothing;
    EXCEPTION WHEN check_violation THEN
        RAISE WARNING '0030: % accepted offer(s) hold a partial contract and cannot be '
                      'demoted (an accepted disclosure is immutable). The constraint is '
                      'enforced for all new and updated rows but left NOT VALID for the '
                      'existing ones; those offers report schedule_source=reconstructed '
                      'and cannot board.',
            (SELECT count(*) FROM offers
              WHERE num_nonnulls(regular_payment_count, final_payment, term_months,
                                 schedule_version, principal, note_rate_pct)
                    NOT IN (0, 6));
    END;

    -- The count and the term must describe the same schedule. Under Model B
    -- there are exactly term_months - 1 regular payments and one adjusted
    -- final payment, so this is an identity, not a policy. It is also the
    -- specific corruption a mismatched request body produced before the
    -- server-derived term was stored: a 36-month schedule filed under a
    -- 60-month term.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'offers'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'offers_schedule_term_agrees'
    ) THEN
        ALTER TABLE offers ADD CONSTRAINT offers_schedule_term_agrees CHECK (
            term_months IS NULL OR regular_payment_count + 1 = term_months
        );
    END IF;

    -- A term of at least one period, and a non-negative count (zero is correct
    -- and reachable: a single-payment loan is all final payment).
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'offers'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'offers_schedule_shape_sane'
    ) THEN
        ALTER TABLE offers ADD CONSTRAINT offers_schedule_shape_sane CHECK (
            (term_months IS NULL OR term_months >= 1)
            AND (regular_payment_count IS NULL OR regular_payment_count >= 0)
        );
    END IF;

    -- A billed amount of zero or less is not a payment. Scoped to the new
    -- column only: monthly_payment predates this migration and constraining it
    -- here would be an unrelated rule smuggled into a schedule change.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'offers'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'offers_final_payment_positive'
    ) THEN
        ALTER TABLE offers ADD CONSTRAINT offers_final_payment_positive CHECK (
            final_payment IS NULL OR final_payment > 0
        );
    END IF;

    -- Only rounding policies this codebase can actually bill. An unknown
    -- version is not a forward-compatible value -- it is a row whose payment
    -- amounts were produced by rules the reader does not have.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'offers'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'offers_schedule_version_supported'
    ) THEN
        ALTER TABLE offers ADD CONSTRAINT offers_schedule_version_supported CHECK (
            schedule_version IS NULL OR schedule_version IN ('B1')
        );
    END IF;
END $$;

DO $$
BEGIN
    -- The same rules on the boarded contract. loans has no term_months of its
    -- own to reconcile -- the column already exists and is NOT NULL -- so the
    -- group here is the three amounts plus the version, and the count is
    -- checked against the loan's own term.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'loans'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'loans_schedule_all_or_nothing'
    ) THEN
        ALTER TABLE loans ADD CONSTRAINT loans_schedule_all_or_nothing CHECK (
            (regular_payment       IS NULL
             AND regular_payment_count IS NULL
             AND final_payment     IS NULL
             AND schedule_version  IS NULL)
            OR
            (regular_payment       IS NOT NULL
             AND regular_payment_count IS NOT NULL
             AND final_payment     IS NOT NULL
             AND schedule_version  IS NOT NULL)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'loans'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'loans_schedule_term_agrees'
    ) THEN
        ALTER TABLE loans ADD CONSTRAINT loans_schedule_term_agrees CHECK (
            regular_payment_count IS NULL OR regular_payment_count + 1 = term_months
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'loans'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'loans_schedule_amounts_positive'
    ) THEN
        ALTER TABLE loans ADD CONSTRAINT loans_schedule_amounts_positive CHECK (
            (regular_payment IS NULL OR regular_payment > 0)
            AND (final_payment IS NULL OR final_payment > 0)
            AND (regular_payment_count IS NULL OR regular_payment_count >= 0)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'loans'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND c.conname = 'loans_schedule_version_supported'
    ) THEN
        ALTER TABLE loans ADD CONSTRAINT loans_schedule_version_supported CHECK (
            schedule_version IS NULL OR schedule_version IN ('B1')
        );
    END IF;
END $$;
