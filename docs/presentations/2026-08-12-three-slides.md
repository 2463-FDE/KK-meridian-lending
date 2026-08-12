# Three slides — 2026-08-12

Speaker notes are the point; the bullets are what goes on screen.

---

## Slide 1 — I raised a control gap before anyone asked

### On screen

**Self-approval on money movements — found, specified, not yet built**

- The servicing HTTP routes `POST /accounts/{loan_id}/adjust-balance` and
  `POST /accounts/{loan_id}/waive-fee` accept a staff role header **and never read
  it**
- One account can zero a borrower's balance alone. No second person, no record of
  who or why
- `docs/DEBT.md` **D8**, open since the vendor delivery
- Written up as `specs/0002` **before** implementation — **draft in PR #28, not
  yet on `main`**: role matrix, EARS + Gherkin criteria, failure behaviour,
  out-of-scope
- Status: **Not started** — a spec is not a control

### Notes

I want to be precise about what I did and did not do here, because the easy
version of this slide is dishonest.

I did not discover D8 — it was already in the debt register. What I found was that
the *shape* of it is worse than the register said: those endpoints accept
`x_user_role` and then ignore it, which is worse than not accepting one, because
it reads like an authorisation check to anyone skimming the signature.

PR #22 closed the network half — both routes now require the internal service
token. That is genuinely useful and it answers a different question: **who can
reach the endpoint**, not **who may authorise the movement**. Conflating those two
is how a hardening PR gets mistaken for a control.

So I wrote the spec first — it is a draft on PR #28 and not on `main`, which is
why the status line says Not started — and the part I would defend hardest is
section 9: a
direct database `INSERT` bypasses the whole thing, because every service connects
as the schema-owning role and a `REVOKE` from the owner does not stick. That means
maker-checker here is a control on the application's staff paths and **not** a
defence against a compromised database credential. If I had left that out, the
first person to read the spec would have believed we had more than we do.

The status is **Not started**. I am not going to call a specification a control.

---

## Slide 2 — PAN/CVV columns are gone. That is not PCI compliance

### On screen

**Card data removed — and the honest boundary**

- `payments.pan` / `payments.cvv` **dropped** — `0031` on existing databases,
  `db/init` never creates them
- Retained: the processor's opaque token (transient, never persisted), plus
  `last4` and `brand`
- Verified on a fresh volume **and** a migrated one — they agree
- **Still not PCI-DSS compliant**, and the README says so first
- Separate evidence still required: **logs, backups, caches**

### Notes

The defect closed here is real and serious: storing a CVV after authorization is a
flat PCI-DSS prohibition regardless of encryption.

The reason this slide exists is the sentence I had to correct. The README claimed
those columns were "still there, waiting to be dropped" for two days *after* the
migration dropped them. Nobody was careless — the migration and the sentence live
in different files, and only one of them executes.

So there is now a test that reads the claim out of the README and the truth out of
the schema and fails when they disagree, in **either** direction. A README saying
the columns are gone while they are back is the more dangerous direction.

The boundary is the part I want to say out loud. **Removing stored card data
closes a specific violation. It evaluates nothing else.** A compliance position
needs a QSA assessment, a real acquirer and a scoped cardholder-data environment;
this build has a mocked processor and no assessment of any kind.

And three things I have **not** evidenced and will not claim: **logs** — verified
at application level only, not at the reverse proxy, container runtime or
deployment platform; **backups** — any snapshot taken before `0031` still contains
those columns, and nothing here purges historical backups; **caches** — no
verification of the frontend, CDN or browser layers at all.

Each of those needs its own evidence. Saying "we removed card data" without them
is the overclaim this project has already had to correct three times.

---

## Slide 3 — Golden APR vectors and a three-service parity test

### On screen

**One disclosure, three services, one set of numbers**

- `db/golden/model_b_schedule_vectors.json` — fixed vectors, checked in
- Disclosure **generates**, servicing **bills**, the borrower **re-reads** — all
  three must produce the same schedule
- Parity asserted against the same literal file, not against each other
- **A checked-in oracle artifact, produced independently of the production
  implementation** — not an external authority
- Money is `Decimal` to the serializer boundary (**D1**) — compared **exactly**,
  no tolerance
- Vectors now parsed with `parse_float=Decimal`: stricter, not weaker
- **Boundary:** `servicing-service`'s sibling schedule function still returns
  floats — exactness holds on the disclosure side, not yet on servicing

### Notes

The problem this solves: an amortization schedule is generated in one service,
billed by another, and redisplayed to the borrower later. Three code paths, one
contract, and nothing forcing them to agree.

Comparing the services *to each other* is the trap — it passes when all three are
wrong the same way, which is exactly what happens when a shared helper changes.

The golden file is a **checked-in oracle artifact, produced independently of the
production implementation**: the vectors were computed and reviewed as expected
Model B output and committed at `db/golden/model_b_schedule_vectors.json`, and
each service is compared to **the file** rather than to another service. It is
deliberately *not* an external authority — nobody outside this repository
certified it, and calling it a third party would overstate what it is. What it
gives is a fixed reference that does not move when the code does.

The exactness matters more than it sounds. **12 CFR 1026.18** requires the
disclosed figures to be the terms of the legal obligation, so a redisplay on a
return visit must equal what the borrower was shown. A one-cent tolerance is
precisely the thing that lets a cent move, so the comparison is Decimal equality
with no tolerance anywhere.

That change made the tests **stricter**: parsing the golden vectors as `Decimal`
means `Decimal("366.12")` is compared to `Decimal("366.12")` rather than to a
binary float that is not quite `366.12`.

One honest limitation: `servicing-service` has a sibling schedule function that
still returns floats. It bills from stored amounts, so no cent is currently at
risk, but the parity guarantee is exact on the disclosure side and not yet on the
servicing side. That is named in the PR rather than left for someone to find.

---

## Evidence links

Every claim above, with its file and its landed/open status. Anything marked
**open** is a draft on a pull request and is **not on `main`** — it is cited as
proposed work, not as something the system does.

### Slide 1 — self-approval control gap

| Claim | Evidence | Status |
|---|---|---|
| The two routes accept a role header and never read it | `services/servicing-service/app/main.py` — `adjust_balance`, `waive_fee` | **landed** on `main` |
| The gap is tracked | `docs/DEBT.md` D8 | **landed**, still **Open** |
| Both routes now require the internal token | PR #22 | **merged** |
| The specification | `specs/0002-maker-checker-self-approval.md` | **open — PR #28** |
| The control itself | — | **not started** |

### Slide 2 — PAN/CVV

| Claim | Evidence | Status |
|---|---|---|
| Columns dropped from existing databases | `db/migrations/0031_drop_payments_pan_cvv.sql` | **landed** |
| Fresh installs never create them | `db/init/001_schema.sql` | **landed** |
| Recorded as fixed | `docs/DEBT.md` D5b / D13 | **landed** |
| README claims held to the schema | `db/tests/test_readme_schema_claims.py` | **merged** — PR #23 |
| Logs, backups, caches | — | **not evidenced** — see the slide |

### Slide 3 — golden vectors and parity

| Claim | Evidence | Status |
|---|---|---|
| The oracle artifact | `db/golden/model_b_schedule_vectors.json` | **landed** |
| Disclosure-side parity | `services/disclosure-service/tests/test_golden_schedule_parity.py` | **landed** |
| Servicing-side parity | `services/servicing-service/tests/test_golden_schedule_parity.py` | **landed** |
| Decimal to the serializer boundary | PR #24, `docs/DEBT.md` D1 | **merged** |
| Servicing's sibling function still float | `services/servicing-service/app/schedule.py` | **open gap**, not tracked as its own `D` number |
