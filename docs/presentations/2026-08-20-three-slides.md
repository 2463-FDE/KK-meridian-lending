# Meridian Lending — Four Statuses, Said Separately

Three slides, regenerated from the repository. Bullets are what goes on screen.
Notes are what I say.

**Provenance.** The claims below rest on `92908ce` (the card-path proof),
`81c16bb74` (the cross-service identifier) and `ee8f733c4` (the allocation
read). Naming the evidence commits rather than a single head, because a head sha
in a header goes stale on the next commit and then points a reader at a tree
that does not support what they are reading — which is the failure this deck
teaches people to check for.

**Opening statement**

> Four things. Separation of duties: done. Auditable traces: done. Cross-service
> traceability: done this week, and I will show you the identifier. Duplicate-capture
> detection: still open, and it is waiting on a decision from you.

Last demo the feedback was that the visuals improved and the reading did not. So
every on-screen line here is short enough to read at a glance, and a claim that
needs a paragraph is in the notes where it belongs.

Each slide: **Show → Evidence → Where it stops.**

---

## Slide 1 — Propose, then fail to approve it yourself

### On screen

**Money movements need a second person. Live, not in a diagram.**

**Show**

- A staff user proposes a balance adjustment
- Accepted. **Balance unchanged.**
- The same person approves it — **refused**
- A second staff user approves — **one ledger row, approver named**

**Evidence**

- `db/migrations/0037_resolve_pending_movement.sql` — the refusal is in the schema
- `db/tests/test_0037_resolve_pending_movement.py` — 23 cases, real two-connection race
- `services/servicing-service/tests/test_maker_checker_api.py` — 40 cases
- `scripts/check_self_approval.sh` — proves it on a **running** system

**Where it stops**

- Staff paths only. A direct database write bypasses it
- Thresholds are **demo configuration**, not Lending Operations policy

### Notes

The refusal lives in the database function, not in application code, and the
acting human is a signed assertion the gateway mints — servicing verifies it and
cannot forge one. That asymmetry is what makes the role check mean anything.

Two details worth saying out loud because they are what a sceptical reviewer
asks. First, the race: two connections, both approving the same proposal at the
same moment, and exactly one wins. That is a real test against real PostgreSQL,
not a mock. Second, the runtime check — a passing CI suite proves the code in the
repository; `check_self_approval.sh` proves the control is live in the stack you
are looking at. Those are different claims and last week only one of them was
covered.

On the boundary: the thresholds — 500 and 5000 — were approved by the project
owner for this environment on 2026-08-16. They are configuration, and this week I
made that mechanical: four copies of those figures exist across the ADR, the
spec, `.env.example` and CI, and a test now reads the ADR's own table and fails if
any copy drifts from it. Before that, CI could have run every suite against a
limit nobody approved while the guard stayed green. Lending Operations still has
to set or approve each figure before production.

---

## Slide 2 — Follow a synthetic card through the payment path

### On screen

**You asked what happens to the CVV and the card number. Traced, not asserted.**

**Show**

- One capture with a **synthetic test card**
- Then: the stored row, the log lines, the reconciliation export
- **No card number. No security code. Anywhere.**

**Evidence**

- `docs/PAN-CVV-DATA-FLOW.md` — the flow, value by value, six trust boundaries
- `services/payment-service/tests/test_pan_cvv_never_enter_the_payment_path.py` — 44 cases
- `services/servicing-service/tests/test_reconciliation_export_carries_no_card_data.py` — 6 cases
- `db/tests/test_no_card_data_on_either_schema_path.py` — both schema paths

**Where it stops**

- **Pre-migration backups still hold real card data.** Not ours to close
- Logs outside the application were not tested
- The tokenizer is a **mock**, not a PCI-attested SDK
- Our own frontend code receives both values. A real hosted field would not
- **This is not a PCI assessment**

### Notes

The answer is stronger than "we do not store it", and the reason is where the
values die: the card is reduced in the browser before any backend is called, so
no backend service holds either value even for a moment. There is no request
body, no log line and no cache downstream of that point that could.

What makes this evidence rather than assurance is the shape of the test. It runs
a real capture with a Luhn-valid test card and then sweeps *everything* the
capture touched — every SQL statement, every bound parameter, every log record —
for the card number pushed through each caller-controlled field in turn. Not just
the field called `pan`, which was already refused. The one that could carry it.

Two things review caught that I want to name, because they are the honest part.
The token field was length-constrained only, so with a real processor configured
a card number could have gone out in the request body — the check that seemed to
cover it only ran when no processor was configured. That is closed now, and
asserted at the transport: the HTTP call is captured and must never happen.

And my first draft of the document said no Meridian process ever receives these
values, while the next page of the same document described our own browser code
taking both as arguments. That was wrong and it is corrected. The claim is about
backend services.

The boundaries on screen are the ones I cannot close from this repository.
Backups taken before the columns were dropped still contain real card numbers.
That is an operations task with an operations owner, and nothing I write here
changes it.

---

## Slide 3 — Where a payment went, and the open items said plainly

### On screen

**Fees → interest → principal is enforced. The API now returns it. The screen does not.**

**Show**

- Apply a synthetic payment
- The split is written to the ledger, one entry per component
- The API returns what that payment paid, **read from the ledger**
- On the borrower's screen: date, method, amount — **not yet the split**

**Where it stops — said separately**

- Separation of duties — **done**
- Auditable traces — **done**
- Cross-service traceability — **done.** One id, charge to ledger
- Pre-trace payments keep no id. We do not back-fill one
- Duplicate-capture detection — **open.** Deferred, pending your decision

**Evidence**

- `services/servicing-service/app/waterfall.py` — the order, from the published schedule
- `services/servicing-service/tests/test_payment_waterfall.py` — 25 cases
- `services/servicing-service/tests/test_payment_allocation_is_read_from_the_ledger.py` — the read
- `services/servicing-service/tests/test_double_capture_is_not_detected_yet.py` — the gap, pinned
- `docs/DEBT.md` D22 — the deferral, with the decision it needs

### Notes

The waterfall is real and it is not invented here — `policies/fee_schedule.md`
publishes the order and the code bills from the borrower's own signed schedule. A
payment larger than everything owed is refused rather than absorbed, because what
happens to the excess is a policy question no document here answers.

What the borrower can see is the honest half of this slide. The allocation exists
in the ledger, one row per component, keyed to the payment. No endpoint exposes
it. The payment history shows date, method, card and amount, and the schedule
table above it shows the *contract's* plan — which a borrower carrying a late fee
will read as an answer and be wrong. I did not build the fix, because whether you
want an itemised breakdown at payment time or only in history changes its shape.
That is question two for you.

On duplicate capture: reconciliation has four break kinds and none of them fires
when two captures for one loan both settle. Each carries its own settlement
reference and matches its own line. I did not build a fifth kind, and I want to
be direct about why. A legitimate repeat payment produces byte-identical
evidence to a double-fund. Any rule flags both or neither, so the window and the
false-positive appetite are yours to set. What I did instead is write the
deferral down with the owner and the follow-up, and pin the gap with a test that
fails the day someone builds detection without settling the question.

Cross-service traceability is the fourth status, and it closed this week. I am
still saying it separately from auditable traces on purpose, because last week
the two were easy to hear as one and they are different claims: auditable means
every change is recorded immutably with who and when, traceable means one
payment can be followed across process boundaries from a single identifier.

The identifier is minted before the payments row exists — deliberately, because
the processor call is the hop that most needs it and the row id does not exist
yet. It goes to the processor as a header, to servicing with the apply, and onto
every ledger entry the payment writes, so one charge returns its whole
allocation rather than one row of it. A retry adopts the original payment's id
rather than minting a second, which is the case an incident actually exercises.

Two bounds, on the slide rather than here. A payment captured before this
existed has no id and keeps none — we do not back-fill one, because the capture
and its authorization already happened without it and a trace covering only the
tail of a payment would look complete while being partial. And this makes a
payment followable in our own logs and tables; it is not a log aggregator, and
the processor is still a mock, so nothing echoes the id back from a real
processor's systems.

---

## Evidence links

Statuses mean different things and are not interchangeable:

- **landed** — merged to `main`
- **verified** — landed *and* covered by a test that fails if it regresses
- **deferred** — a decision is required; the gap is recorded and pinned by a test
- **open** — a named gap, not built, not deferred to anyone

Rows citing `PR #NN` resolve through [`evidence-manifest.md`](evidence-manifest.md).
A bare PR number is not durable evidence: it can be renumbered or reopened, and a
reader offline cannot check it. The manifest names what landed instead.

### Slide 1 — separation of duties

| Claim | Evidence | Status |
|---|---|---|
| Self-approval refused in the schema | `db/migrations/0037_resolve_pending_movement.sql` | **verified** |
| Two-connection approval race, real PostgreSQL | `db/tests/test_0037_resolve_pending_movement.py` | **verified** |
| Role matrix, refuse-at-creation, machine paths | `services/servicing-service/tests/test_maker_checker_api.py` | **verified** |
| A queue someone can work | `frontend/app/approvals/page.tsx` | **landed** |
| The control is live on a running system | `scripts/check_self_approval.sh` | **verified** — PR #50 |
| Thresholds have one source, drift fails CI | `db/tests/test_maker_checker_limits_have_one_source.py` | **verified** — PR #53 |
| Thresholds approved as policy | — | **open** — demo configuration only |

### Slide 2 — the card path

| Claim | Evidence | Status |
|---|---|---|
| The traced flow and its boundaries | `docs/PAN-CVV-DATA-FLOW.md` | **landed** — PR #51 |
| Synthetic capture; nothing in rows, logs or caches | `services/payment-service/tests/test_pan_cvv_never_enter_the_payment_path.py` | **verified** |
| Reconciliation reads and records no card field | `services/servicing-service/tests/test_reconciliation_export_carries_no_card_data.py` | **verified** |
| Neither schema path has a card column | `db/tests/test_no_card_data_on_either_schema_path.py` | **verified** |
| Recorded with its unproven boundaries | `docs/DEBT.md` D21 | **landed** |
| Backups, external logs, caches, PCI assessment | — | **open** — see the slide |

### Slide 3 — payment application and the open items

| Claim | Evidence | Status |
|---|---|---|
| Fees → interest → principal, from the published schedule | `services/servicing-service/app/waterfall.py` | **verified** |
| The split, per component, on the ledger | `services/servicing-service/tests/test_payment_waterfall.py` | **verified** |
| The API returns what a payment paid, read from the ledger | `services/servicing-service/tests/test_payment_allocation_is_read_from_the_ledger.py` | **verified** — PR #58 |
| The borrower's SCREEN shows it | — | **open** — waiting on question 2 |
| Double-fund raises no break today | `services/servicing-service/tests/test_double_capture_is_not_detected_yet.py` | **deferred** — PR #52 |
| The decision, its owner and the follow-up | `docs/DEBT.md` D22 | **deferred** |
| One payment traced across services | `db/migrations/0043_correlation_id.sql` | **verified** — PR #56 |

## Three questions for you

1. **Duplicate capture.** Should two settled captures on one loan for the same
   amount inside a short window raise a break — and what window? What
   false-positive appetite is acceptable?
2. **Payment application.** Is date/method/amount enough on the borrower's
   screen, or do you want fees/interest/principal itemised at payment time?
3. **Late fees.** The fee is the lesser of the flat amount or a percentage of
   arrears, so it compounds on re-assessment. Is compounding intended?
