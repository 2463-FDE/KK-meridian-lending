# Meridian Lending — From Findings to Verifiable Controls

Three slides. The bullets are what goes on screen; the notes are what I say.

**Opening statement**

> My focus was not only fixing defects. I made the important claims independently
> checkable. I will show one control gap I raised, one sensitive-data defect I
> closed and one regulated calculation I proved.

Each slide reads in the same order: **Finding → Action → Evidence → Remaining
boundary**. The last row is not a disclaimer — it is the part that tells you what
you still cannot rely on.

---

## Slide 1 — A control gap raised proactively

### On screen

**Self-approval was identified and specified, but it is not yet controlled.**

**Finding**

- Servicing money routes authenticate the **calling service**
- They do not enforce separation between the person **proposing** and the person
  **approving** a money change
- One account can adjust a balance or waive a fee alone

**Action**

- Wrote the maker-checker behaviour as a specification: role matrix, EARS
  requirements, Gherkin scenarios
- Specification is **open in PR #28**, not on `main`

**Evidence**

- `services/servicing-service/app/main.py` — the routes accept a role header and
  never read it
- `docs/DEBT.md` **D8** — the gap, tracked
- `specs/0002-maker-checker-self-approval.md` — the specification, **open — PR #28**

**Remaining boundary**

- Status: **Identified and specified — not implemented**
- A specification is not a control. Nothing enforces separation at present
- Scoped to application staff paths; a direct database write bypasses it entirely

### Notes

I want to be precise about what I did and did not do, because the flattering
version of this slide is dishonest.

I did not discover this gap — it was already in the debt register. What I found is
that its *shape* is worse than the register said: the endpoints accept a staff
role header and then ignore it. That is worse than not accepting one, because it
reads like an authorisation check to anyone skimming the signature.

PR #22 closed the network half — both routes now require the internal service
token. That is real, and it answers a different question: **who can reach the
endpoint**, not **who may authorise the movement**. Conflating those two is how a
hardening change gets mistaken for a control.

The part of the specification I would defend hardest is the limitation: every
service connects as the schema-owning role, so a `REVOKE` from the owner does not
stick, and a direct database `INSERT` bypasses maker-checker completely. That
makes it a control on staff paths and **not** a defence against a compromised
database credential. Leaving that out would have let the first reader believe we
have more than we do.

The status is *not implemented*, and I will not call a specification a control.

---

## Slide 2 — Sensitive card data removed

### On screen

**PAN/CVV storage was removed, with evidence covering fresh and migrated databases.**

**Finding**

- `payments.pan` and `payments.cvv` stored a full card number and a security code
- Storing a security code after authorization is a flat PCI-DSS prohibition,
  regardless of encryption

**Action**

- **Expand** phase preserved safe `last4` display for readers still running
- **Contract** phase dropped both columns
- Corrected the documentation and the logging/storage claims that described the
  old shape

**Evidence**

- `db/migrations/0031_drop_payments_pan_cvv.sql` — columns dropped from existing
  databases
- `db/init/001_schema.sql` — fresh installs never create them
- `db/tests/test_no_card_data_on_either_schema_path.py` — **fresh and migrated
  paths both tested, and asserted to agree**
- `db/tests/test_readme_schema_claims.py` — documentation held to the schema

**Remaining boundary**

- This closes the **storage** defect. It does **not** prove PCI-DSS compliance
- Outside this evidence: logs beyond the application layer, historical backups,
  and caches

### Notes

The two-phase rollout is the part worth explaining. Dropping the columns in one
step would have broken any instance still reading them, so the expand phase
back-filled `last4` first and the contract phase removed the columns only once no
reader needed them.

On the evidence: the deck used to say this was verified on a fresh database and a
migrated one. Everything it cited proved half of that — the migration covers
existing databases, the init file covers new ones, and nothing compared the two.
The combination was the claim, and it was the only part nobody had checked. There
is now a test that builds both schemas from the real files and asserts neither has
a column that could hold a card number, that both kept `last4`, and that the
legacy fixture genuinely started with the columns — so the removal is not proven
by having nothing to remove.

I want the boundary heard clearly. Backups taken before the contract step still
contain real card numbers. That is a retention question, not a schema question,
and this work does not answer it.

---

## Slide 3 — Regulated calculations made reproducible

### On screen

**APR and payment schedules are now independently checkable.**

**Finding**

- APR and payment schedules are disclosed to borrowers under Regulation Z
- A number that only one code path can reproduce cannot be audited

**Action**

- APR uses the **actuarial** calculation
- Contractual payment schedules are **persisted** at boarding, not inferred later
- Fixed **golden vectors** provide expected values, produced independently of the
  production implementation
- Disclosure, servicing and redisplay paths must all agree with those vectors

**Evidence**

- `db/golden/model_b_schedule_vectors.json` — the checked-in oracle artifact
- `services/disclosure-service/tests/test_golden_schedule_parity.py` — disclosure
  agrees with it
- `services/disclosure-service/tests/test_redisplay_is_exact.py` — disclosure
  money stays **Decimal** through redisplay

**Remaining boundary**

- One sibling servicing schedule function still computes in **float**
- Its output is not part of the redisplay path this evidence covers

### Notes

The golden file is an oracle artifact this team checked in — expected values
produced independently of the production implementation. It is deliberately not
described as an external authority: nobody outside this repository certified it,
and calling it a third-party attestation would overstate exactly the kind of claim
this deck exists to make checkable.

The Decimal boundary matters more than it sounds. The write paths computed in
Decimal and the read path cast every schedule row to binary float, so a value that
was exact in cents stopped being exact several statements before anything reached
a borrower. The conversion now happens once, at the serializer edge, which is
where it belonged.

The float boundary on the last line is on screen rather than in these notes on
purpose. A limitation that lives only in speaker notes is not disclosed to anyone
who reads the deck afterwards.

---

## Evidence links

Every claim above, with its file and its landed/open status. The four statuses on
this page mean different things and are not interchangeable:

- **landed** — merged to `main`
- **verified** — landed *and* covered by a test that fails if it regresses
- **specified** — written down, reviewed, and **not built**
- **open** — proposed on a pull request and **not on `main`**

Rows citing a `PR #NN` resolve through
[`evidence-manifest.md`](evidence-manifest.md), which maps each one to a file that
exists in this repository. A bare PR number is not durable evidence: it can be
renumbered or reopened, and a reader offline cannot check it at all.
`db/tests/test_presentation_claims_resolve.py` enforces both — the manifest must
cover every PR the deck cites, and a merged row's artifact must exist.

### Slide 1 — self-approval control gap

| Claim | Evidence | Status |
|---|---|---|
| The two routes accept a role header and never read it | `services/servicing-service/app/main.py` | **landed** on `main` |
| The gap is tracked | `docs/DEBT.md` D8 | **landed**, still **open gap** |
| Both routes require the internal token | PR #22 | **merged** |
| The specification | `specs/0002-maker-checker-self-approval.md` | **open — PR #28** |
| The control itself | — | **specified, not implemented** |

### Slide 2 — card data removal

| Claim | Evidence | Status |
|---|---|---|
| Columns dropped from existing databases | `db/migrations/0031_drop_payments_pan_cvv.sql` | **landed** |
| Fresh installs never create them | `db/init/001_schema.sql` | **landed** |
| Fresh **and** migrated agree — neither has a card column | `db/tests/test_no_card_data_on_either_schema_path.py` | **verified** |
| Recorded as fixed | `docs/DEBT.md` D5b / D13 | **landed** |
| Documentation held to the schema | `db/tests/test_readme_schema_claims.py` | **verified** — PR #23 |
| Logs, backups, caches | — | **not evidenced** — see the slide |

### Slide 3 — reproducible calculations

| Claim | Evidence | Status |
|---|---|---|
| The oracle artifact | `db/golden/model_b_schedule_vectors.json` | **landed** |
| Disclosure-side parity | `services/disclosure-service/tests/test_golden_schedule_parity.py` | **verified** |
| Disclosure money stays Decimal through redisplay | `services/disclosure-service/tests/test_redisplay_is_exact.py` | **verified** — PR #24 |
| One servicing schedule function still uses float | `services/servicing-service/app/schedule.py` | **open gap** |
