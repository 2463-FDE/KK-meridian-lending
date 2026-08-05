# Spec 0001: Online payments — idempotency + PCI-scope tokenization

- **Status:** Both parts shipped — Part 1 (idempotency) on earlier branches,
  Part 2 (tokenization) on `kalab-week5-payment-tokenization` (ADR 0008).
- **Date:** 2026-07-30 (re-authored — the original spec this section cites was
  never actually committed to this repo; confirmed via `git log --all`, no
  trace on any branch)
- **Author:** In-house team

## Context

Client ask (Dana): let customers pay online (card + ACH) — "just add a
payment form." Attached three "charged twice" support tickets, dismissed as
customer confusion. Handed over the last vendor's prototype handler.

Verified directly against the code at the time, not the client's framing:

- `payments.py` had zero dedupe. A payment log showed a slow (2.4s)
  `POST /payments`, a client retry, and a second POST 410ms later — both
  inserted, both applied to the balance. "People are just confused" was
  wrong; this was a real, reproducible double-charge bug.
- No idempotency-key contract existed anywhere in the schema — nothing told
  the server a retried request was the same request.
- The `payments` table stored the full PAN and CVV in plaintext. The payment
  endpoint additionally accepted an `ssn` field with no functional use in a
  card/ACH capture flow.
- No design existed for reducing PCI scope — the service touched raw
  PAN/CVV directly on every call, which maximizes rather than minimizes the
  PCI SAQ tier the service falls under.

## Part 1 — Idempotency (shipped)

Documented here retroactively: this part of the original spec was written,
then actually built across several later review passes
(`kalab-week3-decision-memory`, `kalab-week4-disclosure-automation`,
`kalab-input-validation-fixes`), not left spec-only as first planned.

**Design, as built:**

1. **`idempotency_key` is required at the API boundary**
   (`services/payment-service/app/schemas.py::PaymentIn.idempotency_key`,
   `Field(min_length=1, max_length=255)`) — caller-generated (e.g. a UUID
   minted once per submit attempt, reused verbatim on retry). Not optional:
   a caller with no key has no way to have a retry recognized at all.

2. **Atomic check-and-write, not check-then-insert.** A partial unique index
   on `payments.idempotency_key` (`db/migrations/0007_payments_idempotency_key.sql`)
   backs an `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id,
   loan_id, amount` (`services/payment-service/app/payments.py::charge()`). A
   conflict means this exact key was already used; the original stored row is
   read back and its result replayed verbatim — no second row, no second
   charge, even if the retry races the original request.

3. **A reused key with a different `loan_id` or `amount` is a 409, not a
   silent misapply.** Review finding: an early version of this reconciled a
   retry against the *request's own* `loan_id` instead of the row's stored
   value — a retry that (by bug or bad-faith) sent a different `loan_id`
   with the same key could misapply the balance to the wrong loan.
   `IdempotencyKeyConflict` now raises whenever `row["loan_id"] != loan_id or
   row["amount"] != amount`, surfaced as HTTP 409 — a key collision like this
   is a client bug or an attempted attack, not a safe retry to honor either
   way.

4. **Capturing the charge and confirming the balance moved are tracked
   separately.** `applied_at` (`db/migrations/0012_payments_applied_at.sql`)
   is `NULL` until `servicing-service` confirms the apply-payment call
   succeeded — a servicing-side failure reports `status: "pending"`, not a
   false `"captured"`. A same-key retry checks this column and retries the
   apply instead of repeating a stale success.

5. **The apply side is idempotent too.** `servicing-service`'s own
   apply-payment endpoint used to move the balance unconditionally on every
   call, trusting payment-service to never call it twice for the same
   payment. `payment_applications` (`db/migrations/0013_payment_applications.sql`)
   is a `payment_id`-keyed atomic guard (`INSERT ... ON CONFLICT DO NOTHING`)
   — only the caller whose insert actually lands a row goes on to move the
   balance (`services/servicing-service/app/balance.py::apply_payment_once()`).

6. **The marker and the balance update commit together, not separately.**
   Review finding: these were originally two separate auto-committed
   statements — a crash between them left a permanent "applied" marker with
   no balance ever moved, silently no-opping every future retry forever.
   Both now run inside one transaction (`services/servicing-service/app/db.py::transaction()`);
   an exception rolls the marker back with the balance update, so a retry
   sees no marker and genuinely retries instead of skipping.

**Acceptance criteria (all met, covered by tests):**

- A retried POST with the same `idempotency_key`, `loan_id`, and `amount`
  returns the identical response and produces exactly one `payments` row.
  (`test_repeated_post_payment_with_same_idempotency_key_is_not_double_charged`)
- A retried POST with the same key but a different `loan_id` or `amount`
  returns 409, applies nothing new.
  (`test_reusing_a_key_with_a_different_loan_id_is_a_409_not_a_misapply`,
  `test_reusing_a_key_with_a_different_amount_is_a_409`)
- A charge whose balance-apply call fails reports `"pending"`; a subsequent
  same-key retry retries the apply and reconciles to `"captured"` once it
  succeeds, without ever double-applying the balance.
  (`test_repeated_post_payment_reconciles_a_pending_apply`,
  `test_apply_payment_once_is_a_noop_on_duplicate_payment_id`)
- A balance-update failure occurring *after* the apply-marker would have
  landed rolls the marker back too — a retry still genuinely applies, rather
  than silently no-op'ing forever.
  (`test_apply_payment_once_rolls_back_marker_and_retries_after_a_failed_balance_update`)

## Part 2 — PCI-scope tokenization (built, `kalab-week5-payment-tokenization`)

**Problem, as it stood before this branch:** `payment-service` received and
stored raw PAN and CVV on every charge
(`services/payment-service/app/payments.py` docstring used to read: "Stores
the FULL PAN and the CVV on the payments row (D5 — still open)").
`PaymentIn.ssn` was accepted with no functional use in a card/ACH capture
flow. This maximized the service's PCI SAQ scope instead of minimizing it,
and stored GLBA-covered data (SSN) inside a PCI-scoped flow for no reason.

**Design, as built (ADR 0008, supersedes ADR 0003):**

1. **Tokenize at the processor, not at Meridian.** The payment form
   (`frontend/lib/tokenize.ts`) tokenizes the card client-side before any
   network call — Meridian's frontend and backend never see a raw PAN or
   CVV at any point. Returns `processor_token`, `last4`, `brand`. No real
   payment processor is integrated in this training app, so this function
   is an explicit mock standing in for a real processor SDK (e.g. Stripe
   Elements) — the contract it enforces (opaque token + display fields
   only, ever) is what a real integration slots in behind.

2. **`payment-service` accepts only the token.** `PaymentIn`
   (`services/payment-service/app/schemas.py`) drops `pan`, `cvv`, and `ssn`
   entirely and sets `model_config = {"extra": "forbid"}` — a client still
   sending any of the three gets a 422, not a silent drop. Adds
   `processor_token`, `last4` (validated 4-digit), `brand`.

3. **Storage changed to match.** `payments.pan`/`.cvv` are **not dropped** —
   kept nullable, dead-going-forward, for rows that predate this change
   (`db/migrations/0016_payments_tokenization.sql`). New rows populate
   `last4`/`brand` instead; `processor_token` is used transiently in
   `charge()` and is never written to the row at all — a vaulted token is
   itself sensitive, so persisting it would just relocate the same problem.

4. **Migration path for existing rows.** Historical rows with a real
   `pan`/`cvv` predate this design and are not retroactively tokenized —
   that needs contacting the processor per row, a separate project, same
   shape as the Week 10 "hard delete vs. redact" retention question.

**Acceptance criteria (met, covered by tests):**

- `payment-service` rejects a request containing a `pan`, `cvv`, or `ssn`
  field outright (422, `extra="forbid"`), not a silent drop.
  (`test_post_payment_rejects_pan_cvv_ssn_outright`)
- A charge succeeds using only `processor_token` + `last4` + amount +
  `loan_id`; the stored row has `last4`/`brand`, never the processor token.
  (`test_post_payment_stores_last4_and_brand`,
  `test_post_payment_never_persists_processor_token`)
- The processor token is redacted from logs, same as pan/cvv/ssn were.
  (`test_post_payment_log_line_redacts_processor_token`)
- `last4` is validated as exactly 4 digits; a malformed value is rejected.
  (`test_post_payment_rejects_malformed_last4`)
- A PCI-scope assessment (SAQ-A or equivalent, once a real processor is
  chosen) can be argued from the code, not asserted in a README — closing
  the same "false compliance claim" problem Week 1 fixed for the PCI-DSS
  claim itself. (Still conditional on an actual processor integration —
  the mock demonstrates the architecture, it is not itself a PCI control.)

## Part 2 addendum — authorization is idempotent at the processor boundary too

**Review finding:** `authorize_charge()`/auth_status were introduced alongside
tokenization but weren't actually idempotent — the processor call carried no
idempotency key, and a same-key retry on a `'pending'` row called
`authorize_charge()` again unconditionally. A crash between the processor
approving a charge and payment-service persisting that fact (`auth_status`
and the processor's own authorization id were also two separate writes, not
one) left a real risk of charging the card twice on retry.

**Design, as built:**

- `authorization_id` (`db/migrations/0019_payments_authorization_id.sql`) is
  written in the SAME `UPDATE` that flips `auth_status` to `'captured'` —
  one atomic write, not two.
- `idempotency_key` is now passed to `processor.authorize_charge()`, forwarded
  to a real processor as an `Idempotency-Key` header so it also dedupes on
  its end.
- A `'pending'` retry calls `processor.get_authorization(idempotency_key)`
  first — reuses the processor's own record if one exists, and only calls
  `authorize_charge()` if the processor genuinely has none.

**Acceptance criteria (met, covered by tests):**

- A same-key retry after a crash between processor approval and
  `auth_status` being persisted reuses the existing authorization instead of
  re-issuing a charge — `authorize_charge()` is called exactly once across
  both attempts.
  (`test_retry_after_crash_before_auth_status_persists_reuses_existing_authorization`)
