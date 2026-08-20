# Where the card number and the security code go

The client asked one question at the 2026-08-19 demo: *what exactly happens to
the card security code (CVV) and the full card number (PAN) in the payment
path?* "We removed the columns" answers a narrower question than the one asked.
The columns are gone (`db/migrations/0031_drop_payments_pan_cvv.sql`), and that
was already evidenced — but a value can be absent from storage and still pass
through a process, a log line or a cache. This document traces both values end
to end, names the boundaries it cannot prove, and points at the tests that
execute the claims.

Every claim below is either enforced by a test named beside it, or stated as
unproven. Nothing here is an assurance.

**Synthetic data only.** The tests use `4111111111111111` / `123` — a published
test card, not a card belonging to anyone.

---

## 1. The short answer

| Value | Does a Meridian **backend** service hold it? | Where it dies |
|---|---|---|
| PAN (full card number) | **No, not even transiently** — no backend service receives it | In the browser, inside `frontend/lib/tokenize.ts`; the last four digits leave it, nothing else |
| CVV (security code) | **No** — it is a parameter of one browser-side function that reads and discards it | Same place, and it is not even returned from that function |

The reason this is a stronger statement than "we do not store it" is the
tokenization boundary: the values are reduced *before* the first Meridian
backend is called, so there is no backend process, no request body, no log line
and no cache downstream of that point that could hold them.

**Read the scope of that claim exactly.** It is about backend services. **Meridian
frontend code in this repository does receive both values** — `tokenizeCard(pan,
cvv)` is our code, running in the borrower's browser, and it takes the card
number and the security code as arguments. An earlier draft of this document
said "no Meridian process ever receives it", which was wrong on its own next
page, and was corrected in review (PR #51, DOC-FRONTEND-001). A claim that no
Meridian code of any kind handles PAN or CVV requires a real processor's hosted
field, where the input element itself belongs to the processor's origin and the
values never enter a script we wrote. That is boundary B1 below, and it is not
built.

## 2. The flow, step by step

### Step 1 — the browser holds the PAN and the CVV

`frontend/app/servicing/[loanId]/page.tsx` collects the card and calls
`tokenizeCard(pan, cvv)`. In this repository the page uses a hardcoded
synthetic card as demo texture rather than a real input field, but the boundary
is the same either way.

### Step 2 — tokenization, in the browser

`frontend/lib/tokenize.ts::tokenizeCard` is a **mock standing in for a real
processor's client-side SDK** (a hosted field). It runs entirely in the browser
and returns exactly three values:

```
{ processor_token: "tok_mock_<uuid>", last4: "1111", brand: "visa" }
```

The `cvv` parameter is named `_cvv`: read, never returned, never logged, never
sent. The PAN is reduced to its last four digits and a brand prefix test. This
is the boundary the whole design rests on — **in a real deployment the PAN and
CVV go from the browser to the processor's own servers and never touch Meridian
code at all**, and the mock reproduces that shape rather than pretending to be
a PCI-attested tokenizer (see §5, boundary B1).

### Step 3 — the API boundary refuses the fields outright

`POST /payments` binds `services/payment-service/app/schemas.py::PaymentIn`,
which has `model_config = {"extra": "forbid"}` and no `pan`, `cvv` or
`card_number` field. A client that sends one gets a **422, not a silent drop**.

The fields it does accept are shape-constrained precisely because a permitted
field is otherwise a channel for the same data:

| Field | Constraint | Why the constraint exists |
|---|---|---|
| `last4` | `^\d{4}$` | The only card digits that may cross |
| `brand` | `^[A-Za-z][A-Za-z ]{0,19}$` | Card brands are words; a digit run here would be stored |
| `method` | `Literal["card", "ach"]` | Persisted verbatim in the same INSERT |
| `loan_id` | `int`, ≤ 2147483647 | An unbounded int accepts a PAN-shaped number |
| `idempotency_key` | rejected if `redactor.looks_sensitive` | Persisted on the row; redaction covers logs, not storage |

`processor_token` is the one free-form string, and closing it took two changes
rather than the one this document originally claimed:

* `PaymentIn` now refuses a token carrying the card or SSN shapes the redactor
  knows — the same rule as `idempotency_key`, for a different reason. That field
  is constrained because it is **stored**; this one because it is
  **transmitted**.
* `processor.authorize_charge` refuses the same shapes before the stub/real
  split, so the guard covers both paths.

The second one is the point. The original version of this document said the
field was "not a hole" because an unrecognised token is declined by the stub
processor — but that check only runs when no processor is configured. With
`PROCESSOR_API_KEY` set, the value was posted verbatim as `json={"token": ...}`,
which put a card number in an outbound request body. Found in review (PR #51,
PAY-FLOW-001), and the test that had "proved" the field safe could never have
reached the code that sends it.

**Cost of the check, stated:** a real processor whose token format contains a
Luhn-valid or nine-digit run would be refused. A token is an opaque correlator
by definition; if a live format collides, the fix is the format or an explicit
allowance, not deleting the guard.

### Step 4 — what payment-service writes

`services/payment-service/app/payments.py::charge` writes one `payments` row:

```
loan_id, last4, brand, amount, method, idempotency_key, auth_status
```

then, in a single UPDATE on capture: `authorization_id`, `captured_at`,
`processor_ref`, `capture_source`. Later, `applied_at` / `apply_*`.

**`processor_token` is never persisted.** A vaulted token is itself sensitive,
so it is used for the authorization call and discarded — ADR 0008.

The `payments` table has no `pan` and no `cvv` column on either schema path
(fresh init or full migration chain).

### Step 5 — what payment-service logs

One INFO line per charge, built through `redactor.redact_dict`, which replaces
sensitive **keys** (`pan`, `cvv`, `ssn`, `card_number`, `processor_token`,
`name`, …) and applies Luhn-validated PAN, SSN and CVV **patterns** to every
other string value. The cardholder name is not passed to the logger at all
(D5d) — the redactor entry is the backstop, not the primary guard.

### Step 6 — what crosses to servicing

`POST /accounts/{loan_id}/apply-payment` carries `{"amount", "payment_id"}`.
Nothing card-shaped is in the body, and there is no card field to omit.

### Step 7 — reconciliation

`services/servicing-service/app/reconciliation.py` reads `loan_id`,
`processor_ref` and `amount` from `payments`, and `loan_id`, `processor_ref`,
`amount`, `settlement_date`, `type` from the settlement CSV. A break record
carries `loan_id`, `processor_ref`, `kind`, `ledger`, `settlement`,
`difference` — and those, plus counts and a file digest, are what
`reconciliation_runs` retains. **No card field is read, compared or recorded.**

### Step 8 — caches

There is no application-controlled cache in either service: no cache module, no
`lru_cache`, no in-process response store. The only process-lifetime state in
payment-service is `processor._stub_authorizations`, the mock processor's own
idempotency-key store, which holds `Authorization` tuples —
`authorization_id`, `captured_at`, `processor_ref`. Infrastructure caches are a
boundary, not a claim: see §5, B4.

---

## 3. Trust boundaries

| # | Boundary | What crosses | Enforced by |
|---|---|---|---|
| T1 | Browser → processor | PAN, CVV | The processor's SDK. Outside Meridian code. Mocked here |
| T2 | Browser → payment-service | token, last4, brand, amount, loan_id, key | `PaymentIn`, `extra="forbid"` |
| T3 | payment-service → processor | token, amount, idempotency key | `PaymentIn`'s token validator, then `processor.authorize_charge`'s own refusal before either path builds a request |
| T4 | payment-service → servicing | amount, payment_id | `_apply_via_servicing` |
| T5 | payment-service → its log file | redacted dict | `redactor.redact_dict` |
| T6 | payments table → reconciliation | loan_id, processor_ref, amount | The SELECT list in `reconciliation.py` |

## 4. The tests that execute these claims

| Claim | Test |
|---|---|
| A synthetic PAN pushed through **every** allowed field reaches no SQL parameter and no log record | `services/payment-service/tests/test_pan_cvv_never_enter_the_payment_path.py` |
| A synthetic CVV, in four phrasings, pushed through **every** allowed field, likewise | same file — widened after review found the evidence table claiming more than the sweep covered (TEST-CLAIM-001) |
| A card-shaped `processor_token` is refused at the API boundary, before any row is written | same file |
| With a real processor configured, a card-shaped token never reaches `httpx.post` — asserted at the transport, not on the exception | same file; mutation-checked by disabling the guard, which fails that case |
| The named fields `pan`/`cvv`/`ssn` are refused with 422 and nothing is written | `services/payment-service/tests/test_charge_flow.py` |
| `processor_token` is never persisted and is redacted in the log | `services/payment-service/tests/test_charge_flow.py` |
| The redactor masks PAN (Luhn), CVV and SSN shapes, by key and by pattern | `services/payment-service/tests/test_redactor.py` |
| The cardholder name reaches no log record | `services/payment-service/tests/test_cardholder_name_not_logged.py` |
| Neither schema path has a PAN or CVV column, and the migrated path really did have them first | `db/tests/test_no_card_data_on_either_schema_path.py` |
| No service source reads `payments.pan` / `payments.cvv` | `db/tools/check_no_pan_readers.py` |
| Reconciliation reads, compares and records no card field | `services/servicing-service/tests/test_reconciliation_export_carries_no_card_data.py` |

## 5. Boundaries this does NOT prove

Named as unproven rather than left to be assumed. Each is a real gap, not a
formality.

**B1 — there is no real processor, and our own frontend handles the card.**
`frontend/lib/tokenize.ts` is a mock. It demonstrates the boundary; it is not a
PCI-attested tokenizer, and no statement here transfers to whichever SDK
replaces it. Two consequences, both open:

* the PAN and the CVV are arguments to **Meridian code** — a function in this
  repository, running in the borrower's browser. A real hosted field would put
  the input element itself on the processor's origin, so the values would never
  enter a script we wrote. Until that swap, "no Meridian code handles them" is
  false and this document does not claim it;
* substituting a real processor is a change to T1 and to T3, and needs its own
  evidence. The T3 guard added in PR #51 keeps card-shaped data out of the
  outbound body regardless of which processor is behind it, but it constrains
  what we send, not what the SDK does.

**B2 — logs outside the application.** Everything above is about
application-level logging call sites. Reverse-proxy access logs, container
runtime logs, and platform/aggregator logs are not configured in this
repository and were not tested. `docs/DEBT.md` D5a records the same scope
limit.

**B3 — the static reader check can be defeated.**
`db/tools/check_no_pan_readers.py` scans service source at one revision. It
cannot see which images are running (`docs/RUNBOOK-pan-cvv-contract.md` says so
explicitly), and dynamic access — `getattr(payment, col)`, `SELECT *` into a
dict — is outside what it reads.

**B4 — caches outside our control.** The claim in §2 step 8 is about
application code. CDN, reverse-proxy and browser caches are not covered. The
browser holds the PAN in a JavaScript variable for the life of the tokenize
call, and browser memory, autofill stores and crash dumps are outside anything
this repository can assert.

**B5 — pre-migration backups.** `0031` dropped the columns from the live
schema. Any database backup, snapshot or dump taken **before** that migration
still contains real `payments.pan` and `payments.cvv` values, and nothing in
this repository can attest that those artefacts were purged. That is an
operations task with an operations owner, and it is open.

**B6 — this is not a PCI assessment.** No SAQ, no ASV scan, no network
segmentation evidence. The claims here are about code paths in this repository.
