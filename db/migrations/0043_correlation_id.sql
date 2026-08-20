-- 0043 -- one identifier that follows a payment across service boundaries.
--
-- The client asked at the 2026-08-19 demo to be able to follow ONE payment from
-- the processor charge through to the servicing application. Today they cannot,
-- and the reason is not missing records -- it is missing a shared key.
--
-- What already exists is AUDITABILITY: `ledger_entries` is immutable, every
-- movement names its actor, and `reconciliation_runs` records each comparison.
-- That answers "what happened to this loan". It does not answer "show me
-- everything belonging to this one charge", because each hop is keyed by
-- something different:
--
--   * the processor authorization is keyed by `idempotency_key`;
--   * the `payments` row is keyed by its own serial `id`;
--   * the ledger rows are keyed by `payment_id`, which does not exist until
--     after the row is inserted -- so the authorization leg has no key at all.
--
-- **Auditable is not traceable.** This column is the difference.
--
-- ## Why not reuse `idempotency_key`
--
-- It is the obvious candidate and it is the wrong one, for two reasons that
-- both matter.
--
-- It is CALLER-SUPPLIED. It decides whether two requests are the same payment,
-- so it is an input to a money decision; widening its job to "and it is also
-- our log correlator" means a caller chooses how our evidence is indexed.
-- `PaymentIn` already has to reject keys carrying card or personal data for
-- exactly this reason.
--
-- And it is SCOPED TO ONE SERVICE. servicing never receives it, and giving it
-- one would hand the apply path a value that changes dedupe behaviour at the
-- processor. A correlator must be inert: nothing may behave differently because
-- of it. That is the whole design constraint here, and it is why this is a
-- separate column rather than a second use of an existing one.
--
-- ## Nullable, and staying nullable
--
-- Every row written before this migration has no correlation id and no way to
-- derive one -- inventing one would fabricate a trace that never happened. So
-- the column is nullable, and NULL means "written before the trace existed",
-- which is a true statement rather than a gap. No reader may require it.
--
-- Nothing keys, joins or reconciles on this column. Reconciliation's predicates
-- are untouched, so a NULL here cannot move money, cannot create a break, and
-- cannot exclude a row from a comparison. It is evidence, not control flow.

ALTER TABLE payments        ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE ledger_entries  ADD COLUMN IF NOT EXISTS correlation_id TEXT;

COMMENT ON COLUMN payments.correlation_id IS
    'Server-minted identifier correlating one payment across services. NOT the '
    'idempotency key: caller-supplied, decides dedupe, scoped to payment-service. '
    'This is server-generated, inert, and shared with servicing. NULL means the '
    'row predates the trace.';

COMMENT ON COLUMN ledger_entries.correlation_id IS
    'The correlation id of the payment that produced this entry, as received '
    'from payment-service -- never re-generated here, or the two sides would '
    'each hold an id the other has never seen. NULL for entries with no payment '
    'behind them (a fee assessment, an approved adjustment) and for rows written '
    'before this column.';

-- Searchable is the entire point: an operator holding an id from a log line has
-- to be able to find the rows without a sequential scan of a money table. A
-- partial index, because the column is NULL for every historical row and for
-- every non-payment ledger entry, and indexing those buys nothing.
CREATE INDEX IF NOT EXISTS idx_payments_correlation_id
    ON payments (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ledger_entries_correlation_id
    ON ledger_entries (correlation_id) WHERE correlation_id IS NOT NULL;
