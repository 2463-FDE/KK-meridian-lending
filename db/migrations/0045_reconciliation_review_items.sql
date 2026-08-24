-- 0045 -- D22: somewhere to record a payment that a human should look at.
--
-- The client's decision of 2026-08-24 replaced the deferral in `docs/DEBT.md`
-- D22 with an exact contract, and the first sentence of it decides this table's
-- shape:
--
--     "Flag qualifying payments for human reconciliation review. Do not treat
--      the flag as a duplicate or validity conclusion or as permission to move
--      money."
--
-- So a row here is a CANDIDATE FOR REVIEW. It is not a duplicate, not a finding,
-- and not an instruction. Nothing in this migration touches capture mechanics,
-- payment application, the waterfall or the ledger, and nothing here can: this
-- table references payments and is referenced by nothing.
--
-- **Two signals, kept apart, because they mean different things.**
--
--   * `exact_provider_transaction_id` / `exact_idempotency_key` -- the same
--     provider reference or the same idempotency key seen again, regardless of
--     elapsed time. Strong evidence, still only evidence.
--   * `heuristic_30_minute_candidate` -- same loan, same amount, same payment
--     source, same channel, inside a rolling 30 minutes. All four, plus the
--     window. Same loan and same amount alone must never produce a row, because
--     that is what a legitimate second installment looks like.
--
-- **No money data beyond what a reviewer cannot work without.** The row names
-- the two payments and the loan; the amount is not copied here, because the
-- reviewer reads it from the payment itself inside an authenticated surface and
-- a review queue is exactly the kind of table that gets exported. No card data,
-- no last4, no cardholder name, no applicant identifier. `correlation_ref` is
-- the payment's own `correlation_id` -- an opaque handle that identifies no
-- person and reconstructs no instrument -- so telemetry can say "review item
-- exists, here is a non-identifying reference" and nothing more.
--
-- **The disposition is write-once.** A human classification is evidence; evidence
-- that can be quietly rewritten is not evidence. The trigger below refuses to
-- change a disposition once set, and refuses to set one without a reviewer.

CREATE TABLE IF NOT EXISTS reconciliation_review_items (
    id                  BIGSERIAL   PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- What was noticed. Never "duplicate": these name the observation, not a
    -- conclusion about it.
    signal_type         TEXT        NOT NULL,

    -- The payment that triggered the signal, and the earlier one it resembles.
    -- `related_payment_id` is nullable: an exact-idempotency replay has an
    -- original to point at, and a provider-reference collision may not.
    payment_id          BIGINT      NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    related_payment_id  BIGINT      REFERENCES payments(id) ON DELETE SET NULL,
    loan_id             BIGINT      NOT NULL,

    -- Non-identifying handle, safe for telemetry (db/migrations/0043).
    correlation_ref     TEXT,

    -- Which internal staff queue owns it. One value today; a column rather than
    -- a constant because the client's decision names the queue as part of the
    -- routing contract, and a second queue must not require a schema change.
    queue               TEXT        NOT NULL DEFAULT 'reconciliation_review',
    status              TEXT        NOT NULL DEFAULT 'open',

    -- The human's answer. Exactly the three the client authorised.
    disposition         TEXT,
    disposition_note    TEXT,
    reviewed_at         TIMESTAMPTZ,
    reviewed_by         TEXT,
    reviewed_by_role    TEXT,

    CONSTRAINT reconciliation_review_signal_known CHECK (signal_type IN (
        'exact_provider_transaction_id',
        'exact_idempotency_key',
        'heuristic_30_minute_candidate'
    )),
    CONSTRAINT reconciliation_review_status_known CHECK (status IN ('open', 'reviewed')),
    -- The three dispositions, and nothing else. A fourth would be a policy this
    -- repository has no authority to invent.
    CONSTRAINT reconciliation_review_disposition_known CHECK (
        disposition IS NULL OR disposition IN (
            'confirmed_duplicate',
            'legitimate_distinct_payment',
            'requires_further_review'
        )
    ),
    -- A reviewed item names its reviewer and when. An unreviewed one names
    -- neither. There is no half-reviewed state.
    CONSTRAINT reconciliation_review_reviewed_is_complete CHECK (
        (status = 'open'     AND disposition IS NULL AND reviewed_at IS NULL
                             AND reviewed_by IS NULL)
        OR
        (status = 'reviewed' AND disposition IS NOT NULL AND reviewed_at IS NOT NULL
                             AND reviewed_by IS NOT NULL)
    ),
    -- One signal per (payment, signal type). A retry that produces the same
    -- observation twice must not fill the queue with copies of one thing to
    -- look at.
    CONSTRAINT reconciliation_review_one_signal_per_payment UNIQUE (payment_id, signal_type)
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_review_open
    ON reconciliation_review_items (created_at DESC)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_reconciliation_review_loan
    ON reconciliation_review_items (loan_id, created_at DESC);

COMMENT ON TABLE reconciliation_review_items IS
    'Payments flagged for human reconciliation review (D22, client decision '
    '2026-08-24). A row is a candidate, never a duplicate finding, and never '
    'permission to move money.';

-- A disposition, once recorded, is what the reviewer said.
CREATE OR REPLACE FUNCTION reconciliation_review_disposition_is_write_once()
RETURNS trigger AS $$
BEGIN
    IF OLD.disposition IS NOT NULL AND NEW.disposition IS DISTINCT FROM OLD.disposition THEN
        RAISE EXCEPTION
            'reconciliation_review_items.disposition is write-once: item % is already %',
            OLD.id, OLD.disposition;
    END IF;

    -- The signal is an observation about a payment. Rewriting either would make
    -- the row describe something that was never noticed.
    IF NEW.signal_type IS DISTINCT FROM OLD.signal_type
       OR NEW.payment_id IS DISTINCT FROM OLD.payment_id
       OR NEW.related_payment_id IS DISTINCT FROM OLD.related_payment_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION
            'reconciliation_review_items: the signal and its subject are immutable (item %)',
            OLD.id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS reconciliation_review_items_write_once ON reconciliation_review_items;
CREATE TRIGGER reconciliation_review_items_write_once
    BEFORE UPDATE ON reconciliation_review_items
    FOR EACH ROW EXECUTE FUNCTION reconciliation_review_disposition_is_write_once();

-- **No no-delete trigger, deliberately.** `ledger_entries` forbids deletion
-- because it records money that moved. This records that a human was asked to
-- look at something, and it is scoped to a payment by `ON DELETE CASCADE`: if
-- the payment row is gone, an observation about it is an orphan rather than
-- evidence. The disposition being write-once is what makes the human's answer
-- durable, and that is the property worth enforcing.
