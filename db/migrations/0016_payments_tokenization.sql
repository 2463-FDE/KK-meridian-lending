-- 0016 — Week 5 tokenization fix (ADR 0008, supersedes ADR 0003):
-- payment-service used to receive and store the full PAN and CVV on every
-- charge. Card capture now tokenizes at the processor (see
-- specs/0001-online-payments-idempotency-tokenization.md, Part 2) --
-- payment-service never receives a raw PAN/CVV at all, only a processor
-- token + last4 + brand for display, and never persists the token itself.
--
-- pan/cvv stay as nullable, dead-going-forward columns for rows that predate
-- this change -- not dropped, since retroactively tokenizing historical rows
-- would mean contacting the processor for each one (out of scope here, see
-- the Week 10 retention/redaction problem for the same shape of question).
-- No code path writes to pan/cvv anymore; last4/brand are what every new
-- row actually populates.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS last4 TEXT,
    ADD COLUMN IF NOT EXISTS brand TEXT;
