# ADR 0012: Sanctions screening behind a provider seam, blocking at the CIP gate

- **Status:** Accepted as a **decision**, not as an implementation. Nothing here
  is built. The Week 9 brief asks for a spec and an ADR; this records where the
  screen runs, what it may block, and the shape a vendor integration must take,
  so that whoever wires up a provider is not also inventing the architecture at
  the same time.
- **Date:** 2026-08-24
- **Depends on:** [`specs/0004-kyc-aml-ubo-and-sanctions-screening.md`](../specs/0004-kyc-aml-ubo-and-sanctions-screening.md),
  which carries the data model, the acceptance criteria and the full
  blocked-authority table. This ADR does not repeat them.
- **Bears on:** [`docs/DEBT.md`](../docs/DEBT.md) **D11** — KYC is CIP-only: no
  OFAC/sanctions screening, no UBO, no ongoing monitoring, no SAR path. D11 stays
  open; this is the design it has been missing, not its closure.
- **Follows:** ADR 0006 (an unavailable model fails closed rather than falling
  back — `RF-1`) and the provider seam in
  `services/decision-service/app/bureau.py`, which is the pattern this reuses
  deliberately.

## Context

`services/kyc-service` performs CIP and stops. `run_cip` checks that four fields
on the stored applicant are non-empty; there is no external cross-reference of
any kind. `CipCheckOut.sanctions_screened` and `.ubo_captured` are **hardcoded
`False`** in `services/kyc-service/app/schemas.py`, and `kyc_checks` has no
column either one could be written to — the schema says so in a comment.

So a sanctioned party clears onboarding today, and an entity clears with no
natural person named. `db/init/002_seed.sql` line 17 is the worked example:
`Northgate Holdings LLC`, EIN only, no SSN, no DOB, `outcome: approve`.

Three things make this a decision worth recording now, before any vendor exists:

1. **The last vendor boundary was got wrong the first time.** The credit-bureau
   pull sent an SSN as a URL query parameter and had no idempotency key, so an
   ambiguous timeout produced a second billed hard pull. Both defects were fixed
   by putting the call behind `BureauClient` (`decision-service/app/bureau.py`).
   Screening has the same shape, and the brief's own note asks for the same
   abstraction so the mistake is not repeated with a different vendor.
2. **The failure mode is asymmetric.** A screening service that is down and
   silently skipped onboards an unscreened party. A screening service that is
   down and blocks stops onboarding. Only one of those is recoverable, and which
   way it falls has to be decided in the architecture rather than discovered in
   an incident.
3. **Almost every *policy* input is missing** — disposition, thresholds, SAR
   rules, cadence. A design that needs those answers before it can be written at
   all would stall; a design that invents them would ship a control nobody
   approved. This ADR is scoped to what is decidable without them.

## Decision

### 1. The screen is a separate provider boundary inside `kyc-service`

`services/kyc-service/app/screening.py` — which **does not exist on `main`** and
arrives with the implementation, not with this ADR — holds
`SanctionsScreeningProvider`, `ScreeningResult`, a deterministic
`StubScreeningProvider` for dev and test, and an HTTP implementation that pins
the requirements of a real integration without claiming to work. Exactly the
four pieces `bureau.py` has, for exactly the same reasons.

**Not a new service.** `kyc-service` already owns identity evidence, is not
host-published, and requires `X-Internal-Token` on the route that writes CIP
results. A fifth service would add a trust boundary without adding a control.

**Not inline `httpx` in the router.** That is what `decision.py` did before ADR
0006's review, and the seam is what made the SSN-in-query-string and
double-pull defects fixable in one place.

### 2. The screen blocks at the existing CIP gate, and nowhere new

`origination-service/app/routers/applications.py::_kyc_rows_for` already gates
the decision on a `kyc_checks` row, and `_attempt_kyc_recheck` already runs CIP
for an application that has none. That gate is real, tested and load-bearing.

So screening attaches to the verdict the gate already reads rather than
introducing a second gate: `cip_passed` becomes true only when the four factors
pass **and** a completed screen returning `clear` supports it, for the applicant
and for every recorded beneficial owner (spec 0004 §7.1).

The consequence is deliberate and worth stating plainly: **the seeded
`Northgate Holdings LLC` application stops passing** once this is implemented,
because no control person is recorded for it. That is the gap becoming visible,
not a regression.

### 3. Fail closed, with no degraded mode

A provider timeout, transport error, malformed response, unparseable outcome or
missing list version records `outcome = 'error'` and refuses the CIP result. No
"screening unavailable, proceeding" path exists, and none may be added.

This is the same rule ADR 0006 already settled for the scorer (`RF-1`: a missing
or unreachable model fails closed rather than falling back to a stub score that
could be mistaken for a real vendor response). A skipped screen is worse than a
missing score, because the record would then show a verified applicant with no
screen behind it.

The refusal is atomic with respect to the audit trail: no `kyc_checks` row
claiming a screen that did not complete, and no screening row for a CIP result
that was never written — the rule spec 0003 §1.4 states for adverse-action
reasons, applied here.

### 4. Idempotency is the caller's key, forwarded to the provider

Every screen carries a `request_key`, stable across retries of the same logical
onboarding step and regenerated for a genuinely new one, forwarded to the
provider so a repeat returns the **original** operation. `StubScreeningProvider`
honours the same contract and counts real screens, the way
`StubBureauClient.pull_count` does, so a retry test needs no vendor.

Two reasons this matters more than it did for the bureau: a duplicate screen
writes a second piece of evidence about the same subject, and two evidence rows
that disagree are worse than one; and a screen is only meaningful against a
stated `list_version`, which a naive retry would silently change.

### 5. Evidence is stored; the provider's payload is not

Persisted: subject, provider, `list_version`, `request_key`, provider reference,
outcome, screening time. Not persisted and not logged: the raw response. A hit's
payload is third-party list data about named individuals, and the reference id is
what a later review needs to re-fetch it.

**A screen with no `list_version` is not evidence** and is rejected rather than
stored as `clear`. "Clear against the SDN List" without saying which day's list
is a claim that cannot be reproduced.

### 6. The stub is not a list, and says what it is

`StubScreeningProvider` MUST NOT ship names resembling real SDN entries; its
`potential_match` case triggers on an obviously synthetic marker. Committing
anything that looks like sanctions-list data would create a file people mistake
for the list. As with `ALLOW_MODEL_STUB` and the `-stub` model-version suffix,
the stub is explicitly selected and identifies itself in what it writes; a stub
outside a development environment is a configuration error, not a fallback.

### 7. Ownership is recorded relationally, not in a graph store

`beneficial_owners` rows against `applicants`, with an intermediate entity owner
captured as its own `applicants` row (spec 0004 §2.3). ADR 0009 already measured
traversal cost for identity links at this scale and concluded the relational path
is adequate; that conclusion holds and no graph store is introduced.

Effective ownership through a chain stays **computed, never stored** — a stored
figure is a second source of truth that goes stale the moment one link changes.

## What this ADR deliberately does not decide

Each of these has at least two defensible answers, and picking one in code is how
the maker-checker limits became a finding before they were approved
(`docs/DEBT.md` D8). Full table in spec 0004 *Blocked, and by whom*.

| Not decided | Label |
|---|---|
| Which BSA/AML obligations bind Meridian at all | **COMPLIANCE-BLOCKED** |
| What happens to an application on a `potential_match`, and who may clear one | **COMPLIANCE-BLOCKED** |
| Any match-score threshold, name-distance metric or transliteration rule | **COMPLIANCE-BLOCKED** (appetite) and **VENDOR-BLOCKED** (scoring semantics) |
| SAR rules, deadlines, signing authority, tipping-off constraints | **COMPLIANCE-BLOCKED** |
| Which vendor, and the list-currency/staleness contract | **VENDOR-BLOCKED** |
| Whether owner SSN/ITIN is collected at all | **CLIENT-BLOCKED** |
| Ownership-chain depth and the multiply-through-the-chain rule | **CLIENT-BLOCKED** |
| Re-screening cadence and portfolio backfill | **CLIENT-BLOCKED** |
| Who runs the monitoring job and receives its findings | **OPS-BLOCKED** — the same gap as D7 |

**No vendor is selected, no endpoint is configured, and no threshold appears
anywhere in this repository.**

## Consequences

**Good.**

- The vendor boundary exists before the vendor does, so an integration is a
  provider implementation rather than a redesign — and the SSN-in-query-string
  and double-pull defects cannot recur, because the seam forbids them in one
  place.
- Blocking at the existing gate means no second enforcement point to keep in
  step. The gate is already tested.
- Fail-closed is stated before there is pressure to relax it. That pressure
  arrives during an outage, which is the worst moment to decide.
- The gap stays visible while unbuilt: `sanctions_screened=False` is still
  hardcoded, D11 is still open, and this ADR does not claim otherwise.

**Costs, and the honest ones.**

- **Onboarding becomes dependent on a third party.** Fail-closed means a provider
  outage stops onboarding entirely. That is the correct trade for sanctions and
  it is a real availability cost, and it is precisely why an operational owner
  (OPS-BLOCKED) is a prerequisite rather than a detail.
- **Entity onboarding gets slower and harder**, because someone must actually
  identify a control person. The seeded LLC failing is the first evidence of that.
- **A `potential_match` queue with no disposition policy is a dead end.**
  Implementing screening before the COMPLIANCE-BLOCKED rows are answered would
  produce applications that can neither proceed nor be refused. Sequencing
  matters: those answers come first.
- **A second PII surface.** `beneficial_owners` would hold identity data for
  people who are not the applicant, which interacts directly with Week 10's
  retention-and-redaction design. Building it before that design lands would
  create rows nobody knows how to delete.

## Alternatives considered

- **Screen inside `origination-service` at intake.** Rejected: identity evidence
  lives in `kyc-service`, and a second writer to that evidence is how the
  `POST /payments` duplicate happened (D2).
- **A separate screening service.** Rejected: a new trust boundary with no new
  control, and `kyc-service` is already the right owner and already
  token-protected.
- **Screen the entity only, not its owners.** Rejected: it is the shape of the
  current gap — an LLC clears while no human is checked.
- **Fail open on provider error, flag for later review.** Rejected: it is the
  `GENERIC_REASONS` and stub-score pattern again — a record that reads as
  verified when nothing verified it. There is no review queue to flag into
  (OPS-BLOCKED), so "flag for later" means "never".
- **Pick a threshold now and tune it later.** Rejected: a number here looks like
  a control and is a guess with a compliance consequence in both directions.
  Too loose and a sanctioned party clears; too tight and the queue fills until
  operators stop reading it, which is the failure the reconciliation control
  already had to undo once (D7).
- **Ship a "sanctions screened ✓" indicator for the launch**, which is what the
  brief asks for in as many words. Rejected: a control that is only a claim is
  the exact defect this repository has already removed twice — the README's PCI
  claim and the model card's fairness claim.

## Limitations

- Nothing here is implemented. There is no `screening.py`, no
  `sanctions_screenings` table, no `beneficial_owners` table, and no migration:
  the next free number at the time of writing is `0044`.
- The idempotency and list-version contracts are what this repository would
  **require** of a provider. As with `bureau.py`, they would be verified only
  against our own stub — real-provider behaviour is unverified and no production
  guarantee is claimed.
- This is a local training build. Naming OFAC, CIP, CDD or SAR identifies the
  rule a control is modelled on. No control here has been reviewed by counsel,
  audited or certified, and CIP-only onboarding is explicitly non-compliant with
  the programme the brief imagines it already has.
