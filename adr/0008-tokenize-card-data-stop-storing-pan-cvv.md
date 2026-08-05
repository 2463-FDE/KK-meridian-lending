# ADR 0008: Tokenize card data at the processor, stop storing PAN/CVV

- **Status:** Accepted — supersedes ADR 0003
- **Date:** 2026-07-30
- **Author:** In-house team

## Context

ADR 0003 (2023-10-11) decided to store the full PAN and CVV on the `payments`
row "for convenience" — support could "see the card on file," finance could
re-run a charge without asking the customer again. Its own reviewer note,
never resolved: "Are we *sure* about storing CVV?"

Week 5's client brief (Dana): "let customers pay online — just add a payment
form," dismissing three "charged twice" tickets as customer confusion.
Verified directly against the code, not the framing: the double-charge was
real (fixed separately — idempotency, see `specs/0001-...md` Part 1). The
PAN/CVV storage ADR 0003 accepted was never revisited, and turned out to be
the same shape of problem as ADR 0003's own unresolved reviewer question:

- `payments.pan` and `payments.cvv` stored the full card number and CVV in
  plaintext on every charge. CVV storage is an unconditional PCI-DSS
  violation — there is no "encrypted at rest" exception for storing the
  security code at all, which ADR 0003's "the disk is encrypted" reasoning
  did not actually address.
- The payment-capture endpoint additionally accepted an `ssn` field with no
  functional role in a card/ACH charge — GLBA-covered data creeping into a
  PCI-scoped flow for no reason.
- Every code path that touches raw PAN/CVV maximizes the service's PCI SAQ
  scope. The "convenience" ADR 0003 bought (re-running a charge without
  asking again) is exactly the thing a real PCI assessor flags hardest.

## Decision

**Tokenize at the processor, not at Meridian.** The payment form collects
card details into the processor's own client-side boundary (a hosted
field/SDK the processor controls) and returns an opaque `processor_token` +
`last4` + `brand` + expiry. Meridian's frontend and backend never receive a
raw PAN or CVV at any point — not "encrypted in transit," genuinely never
present.

- `payment-service`'s `PaymentIn` drops `pan`, `cvv`, and `ssn` entirely;
  accepts `processor_token`, `last4`, `brand` instead
  (`services/payment-service/app/schemas.py`).
- The `payments` table stores `last4`/`brand` for display — never the
  processor token itself, since a vaulted token is itself sensitive and
  storing it would just relocate the same problem
  (`db/migrations/0016_payments_tokenization.sql`).
- `pan`/`cvv` columns are **not dropped** — they stay nullable, dead-going-
  forward columns for rows that predate this change. Retroactively
  tokenizing historical rows would mean contacting the processor per row,
  which is a real project of its own, not a migration-file line item; this
  is the same shape of question the Week 10 retention/redaction problem
  already covers.
- This training app has no real payment processor integrated. The
  tokenization boundary is simulated client-side (`frontend/lib/tokenize.ts`)
  — clearly marked as a mock standing in for a real processor SDK (e.g.
  Stripe Elements), matching this codebase's existing pattern for other
  vendor boundaries (the bureau-pull stub, the AI-scorer stub): a real
  integration replaces the mock, the contract (opaque token + display
  fields only) does not change.

## Consequences

- **Pro:** payment-service's PCI scope shrinks instead of growing — it never
  handles raw cardholder data, only an opaque token plus non-sensitive
  display fields (SAQ-A-shaped, once a real processor is chosen; asserting
  this from the code is the same discipline Week 1 restored after removing
  the false "PCI-DSS compliant" README claim).
- **Pro:** SSN is gone from a flow that never needed it — one less
  GLBA-covered field sitting inside a PCI-scoped service.
- **Con (accepted):** historical rows keep their raw PAN/CVV until a
  separate retention/redaction project (Week 10-shaped) actually addresses
  them — this ADR closes the leak going forward, not retroactively.
- **Con (accepted):** the mock tokenization boundary is not a real PCI
  control by itself — it demonstrates the architecture; a real deployment
  needs an actual processor integration behind it before this claim is true
  in production, same caveat every other vendor stub in this repo already
  carries.
