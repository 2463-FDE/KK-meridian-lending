-- 0028 -- make a captured-but-unapplied payment recoverable without the
-- borrower (PR #8 review, high).
--
-- 0012 added `applied_at` so a capture that never reached the loan balance is
-- at least DISTINGUISHABLE from one that did. But nothing ever looked for
-- those rows: `applied_at IS NULL` appears in no query anywhere in the repo.
-- The only recovery path was a client retry on the same idempotency_key, so a
-- borrower who closed the tab after the card was authorized left money captured
-- and the balance uncredited, permanently, with nothing surfacing it.
--
-- These three columns turn that row into a durable, self-draining work item.
-- The retry loop lives in payment-service (`app/reconcile.py`); this migration
-- only provides the state it needs to be safe and bounded:
--
--   apply_attempts        -- how many times the apply has been tried. Bounds
--                            the retry so a permanently broken row backs off
--                            instead of hammering servicing forever.
--   apply_next_attempt_at -- earliest time the next attempt may run. Doubles as
--                            the claim marker: a worker claims a row by pushing
--                            this into the future in the same statement that
--                            selects it, so two workers (or two replicas) can
--                            never work the same payment concurrently.
--   apply_last_error      -- exception TYPE only, never the message. A servicing
--                            error message can embed request parameters; this
--                            column is for triage, not for reconstructing a
--                            failed call.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS apply_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS apply_next_attempt_at TIMESTAMPTZ;
ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS apply_last_error TEXT;

-- The reconciler's only query shape. Partial, because the interesting rows are
-- a vanishing fraction of `payments` -- everything already applied, still
-- pending authorization, or declined is excluded from the index entirely.
CREATE INDEX IF NOT EXISTS idx_payments_unapplied
    ON payments (apply_next_attempt_at)
    WHERE auth_status = 'captured' AND applied_at IS NULL;
