-- 0044 -- a stable, non-identifying handle for the funding source a payment came
-- from.
--
-- **Why this column has to exist before the duplicate-review heuristic can.**
-- The client's decision of 2026-08-24 flags a payment for human review only when
-- ALL of: same loan, same amount, same payment SOURCE, same payment CHANNEL,
-- inside a rolling 30 minutes. Same loan and same amount alone must never flag,
-- because that is exactly what a second legitimate installment looks like.
--
-- Nothing in this schema could prove "same source":
--
--   * `method` is the CHANNEL (card / ach) and is already stored;
--   * `capture_source` is provenance -- which writer produced the row
--     (processor vs the retired servicing writer, db/migrations/0042) -- and says
--     nothing about the customer's funding instrument;
--   * `processor_ref` identifies one transaction, not the source behind it;
--   * `processor_token` is per-capture and is deliberately never persisted
--     (ADR 0008);
--   * `last4` + `brand` are instrument CONTENT, they are not unique, and the
--     client's decision explicitly declines to treat them as a source identity.
--
-- So the smallest honest thing is a column holding an opaque handle the capture
-- boundary supplies. In a real integration that handle is the processor's own
-- vaulted-source id (a "fingerprint" in several providers' vocabulary), which
-- arrives with the token and identifies the instrument without describing it.
--
-- **What it is in this training build, stated rather than implied.**
-- `frontend/lib/tokenize.ts` is a mock standing in for a processor SDK, and it
-- mints `src_mock_<uuid>` once per card per browser session, remembering the
-- mapping in `sessionStorage`. So the same synthetic card twice in one session
-- yields the same handle, two different cards yield different handles, and the
-- handle is NOT derived from the PAN -- deriving it would put a card-correlatable
-- value in the database, which is the thing this repository has spent Weeks 5-8
-- removing. Across sessions or devices the same card gets a new handle, so "same
-- source" is provable within a session only. That is enough for the seeded
-- fictional traffic the client asked the heuristic to be validated against, and
-- it is not a claim about production provider semantics.
--
-- **Nullable, and null means "cannot prove it".** An ACH payment has no
-- tokenizer, a payment captured before this column existed has no handle, and a
-- caller may omit it. In every one of those cases the heuristic must NOT fire:
-- the client's rule requires the source to match, and unknown is not a match.
-- Falling back to loan + amount + channel is the false-positive the rule exists
-- to prevent.
--
-- No credential, no card data, no customer identity, no new integration.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS source_ref TEXT;

COMMENT ON COLUMN payments.source_ref IS
    'Opaque, non-identifying handle for the funding source (db/migrations/0044). '
    'Not derived from a PAN. NULL means the source cannot be proven, and the '
    'duplicate-review heuristic must not fire on it.';

-- The heuristic asks: "any other capture on this loan, same amount, same source,
-- same channel, in the last 30 minutes?" This is that lookup.
CREATE INDEX IF NOT EXISTS idx_payments_source_window
    ON payments (loan_id, source_ref, method, captured_at)
    WHERE source_ref IS NOT NULL;
