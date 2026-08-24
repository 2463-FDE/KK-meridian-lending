# Spec 0004 — KYC/AML specialization: beneficial ownership and sanctions screening

- **Status:** Accepted as a **specification**. Nothing in it is built, and that is
  the Week 9 deliverable: spec plus ADR, screening-vendor integration scoped and
  not written. A section that describes code says so with a citation; every other
  section is a requirement.
- **Date:** 2026-08-24
- **Domain:** KYC
- **Authority for the requirement:** Week 9 client brief (Dana, VP Lending Ops) —
  KYC "tightened" during onboarding, delivered as *"a KYC/AML specialization spec
  — a `beneficial_owners` table design, a `SanctionsScreeningProvider` interface,
  ongoing-monitoring/SAR trigger points, a screening-integration ADR, and
  acceptance criteria"*. The brief's own framing — *"we verify the applicant's
  identity already, so I think we're mostly there — just make it look thorough
  for the launch"* — is the thing this document exists to answer, and the answer
  is no.
- **Bears on:** [`docs/DEBT.md`](../docs/DEBT.md) **D11** — KYC is CIP-only: no
  OFAC/sanctions screening, no UBO, no ongoing monitoring, no SAR path. This spec
  does not close D11. It specifies what closing it would require and records who
  each missing decision belongs to.
- **Companion:** [`adr/0012-sanctions-screening-integration.md`](../adr/0012-sanctions-screening-integration.md)
  — where the screen runs, what it may block, and the provider seam.

**Regulatory anchors, and what they are here for.** This is a local training
build. Naming a rule identifies what a control is *modelled on*; it is not a
statement that Meridian complies with it, and no control here has been reviewed
by counsel.

| Anchor | What it covers | Used here for |
|---|---|---|
| [31 CFR 1020.220](https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1020/subpart-B/section-1020.220) | CIP — identify and verify each customer | the four factors `run_cip` already checks |
| [31 CFR 1010.230](https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1010/subpart-B/section-1010.230) | CDD Rule beneficial ownership for legal entity customers: each individual owning **25% or more** equity, **plus one control person** | the `beneficial_owners` model in §2 |
| [31 CFR 1010.610](https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1010/subpart-F/section-1010.610) / [1020.320](https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1020/subpart-C/section-1020.320) | correspondent due diligence; suspicious activity reporting | the SAR **boundary** in §6 — the trigger points, never the filing rules |
| OFAC sanctions programs (31 CFR chapter V) and the SDN List | U.S. persons may not deal in blocked property or with blocked persons; strict liability, no de minimis | why §3 screening is fail-closed |

Two things these anchors do **not** settle, and this document does not pretend
otherwise:

- **Which of them binds Meridian.** CIP, CDD and SAR obligations attach to
  defined categories of financial institution. Meridian's charter, licensing and
  regulator are not recorded anywhere in this repository. **COMPLIANCE-BLOCKED.**
- **Whether Meridian has a beneficial-ownership *reporting* obligation** (the
  Corporate Transparency Act's BOI regime) as distinct from the *collection*
  obligation modelled here. They are different duties with different scopes, and
  the BOI regime's scope has changed since it took effect. Nothing in this spec
  models BOI reporting. **COMPLIANCE-BLOCKED.**

## Context — what exists on `main` today

Read from the code, not from the brief.

| Concern | Where it lives | State |
|---|---|---|
| CIP factor checks | `services/kyc-service/app/kyc.py::run_cip` | presence checks only — `bool(applicant.get("name"))` and three more. No external cross-reference of any kind, unlike `decision-service/app/bureau.py` which at least has a provider seam |
| Which record CIP reads | `services/kyc-service/app/routers/kyc.py` | the **stored** applicant row, not the request body, and the application/applicant pairing is verified first — a caller holding the internal token cannot mint identity evidence for a stranger |
| Entity pass rule | same file | an entity clears on `name_verified and address_verified` alone. A natural person needs all four factors |
| Sanctions state | `services/kyc-service/app/schemas.py::CipCheckOut` | `sanctions_screened: bool = False`, **hardcoded**, not computed. Deliberately visible rather than absent |
| UBO state | same | `ubo_captured: bool = False`, hardcoded, same reason |
| Persistence | `db/init/001_schema.sql`, `kyc_checks` | four factor booleans plus `cip_passed`. The schema comment says it out loud: *"no sanctions_screened, no ubo_identified, no ongoing_monitoring columns"* |
| Entity applicants in the schema | `applicants` | `is_entity`, `ein`; no owner, no ownership percentage, no control person, and no table to put one in |
| The worked example | `db/init/002_seed.sql` line 17 | `Northgate Holdings LLC`, EIN only, no SSN, no DOB — clears CIP on a company name and an address, with no natural person identified anywhere |
| What consumes the verdict | `services/origination-service/app/routers/applications.py::_kyc_rows_for` | reads `cip_passed` from `kyc_checks`; a missing row blocks the decision, and `_attempt_kyc_recheck` runs CIP once for an application that has none |

**So the honest statement of today's position:** identity verification is a
presence check over stored fields, an entity can onboard with no human ever
named, and the two fields that would say whether screening happened are
constants. The gate downstream is real and works — it is gating on a question
that is narrower than the one the brief assumes it answers.

## §1. CIP is not CDD, and neither is AML

The brief's "we're mostly there" treats one control as the whole program. These
are different questions, and doing CIP more thoroughly does not turn it into any
of the others.

| | Question it answers | On `main` |
|---|---|---|
| **CIP** | is this customer who they say they are? | presence checks, `run_cip` |
| **CDD** | who is behind this customer, and what should we expect of them? | **nothing.** No beneficial owner, no expected activity, no risk rating |
| **Sanctions screening** | is this party — or an owner of it — one we are forbidden to deal with? | **nothing** |
| **Ongoing monitoring** | has any of the above changed since onboarding? | **nothing.** `kyc_checks` is a point-in-time row with no re-check trigger |
| **SAR** | must this be reported, and to whom? | **nothing**, and see §6 |

A requirement follows from that table rather than from a preference:
**`cip_passed` MUST NOT be presented, in any API response, document or screen, as
evidence of KYC/AML compliance.** It is one factor check. The current
`CipCheckOut.notes` string already says so, and that sentence is now contract:
*"CIP only; no sanctions/OFAC, no UBO, no ongoing monitoring, no SAR path."*

## §2. Beneficial ownership — the model

### 2.1 What must be captured

For every applicant where `is_entity` is true:

- **each individual holding 25% or more of the equity interests**, and
- **one control person** — an individual with significant responsibility to
  control, manage or direct the entity — *whether or not* anyone meets the 25%
  test.

The second half is the part a "percentages only" design loses. An entity with
five 20% owners has **no** 25% owner and still has a control person, and a model
that only records percentages records nobody for it.

### 2.2 Table design — `beneficial_owners`

Design only. **No migration is written**; the next free number at the time of
writing is `0044`.

| Column | Type | Rule |
|---|---|---|
| `id` | `SERIAL PRIMARY KEY` | |
| `applicant_id` | `INTEGER NOT NULL REFERENCES applicants(id)` | the entity this owner belongs to. Indexed |
| `person_name` | `TEXT NOT NULL` | the individual. Never an entity name — see 2.3 |
| `dob` | `DATE` | required for a screenable identity; nullable in the column so an incomplete capture can be *recorded as incomplete* rather than refused into nonexistence |
| `ssn_or_itin` | `TEXT` | same PII class as `applicants.ssn`, and subject to the same treatment (`docs/DEBT.md` D5) — it is **not** in scope of this spec to introduce a second plaintext identity column without the redaction design Week 10 owns. **CLIENT-BLOCKED** on whether it is collected at all |
| `address` | `TEXT` | |
| `ownership_pct` | `NUMERIC(5,2)` | `CHECK (ownership_pct > 0 AND ownership_pct <= 100)`. `NULL` is permitted **only** when `is_control_person` is true: a control person may hold no equity |
| `is_control_person` | `BOOLEAN NOT NULL DEFAULT FALSE` | |
| `capture_source` | `TEXT NOT NULL` | who asserted this — the applicant's own certification, a document, a registry. Provenance is part of the record, not metadata about it |
| `captured_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `created_by` | `TEXT NOT NULL` | the verified human or service principal that recorded it |

Constraints that carry the rule rather than describing it:

- `CHECK (ownership_pct IS NOT NULL OR is_control_person)` — a row must state an
  equity interest or a control role. A row that says neither records nothing.
- **At most one control person per entity** — a partial unique index
  (`UNIQUE (applicant_id) WHERE is_control_person`). The CDD Rule asks for one;
  "several people are sort of in charge" is the answer that means nobody was
  identified.
- **Total ownership may exceed 100% only if someone made a mistake**, so
  `SUM(ownership_pct) <= 100` per entity is an *application-level* check, not a
  table constraint — it is not expressible per-row, and enforcing it with a
  trigger would block a legitimate two-statement correction. It is an acceptance
  criterion (§7), not a `CHECK`.
- **Append-mostly, with provenance.** A correction supersedes rather than
  overwrites, following `decision_events` (`db/init/004_decision_events.sql`).
  Whether that is a trigger-enforced append-only table or a versioned row is an
  implementation choice ADR 0012 does not need to settle.

### 2.3 The entity → owner relationship, including the part that recurses

An owner of an entity may itself be an entity. `beneficial_owners.person_name`
is deliberately typed for a **natural person**, because the regulation's object
is a human being: an ownership chain must be walked until individuals are
reached.

This spec requires that the chain be **recorded**, not that it be resolved
automatically:

- an intermediate entity owner is captured as its own `applicants` row with
  `is_entity = true`, and its owners as `beneficial_owners` rows against that
  row;
- the effective ownership of an individual through a chain is a **computed**
  question, not a stored column — storing it would create a second source of
  truth that goes stale the moment one link changes;
- how deep the walk must go, and what to do about a chain that cannot be
  resolved (a foreign parent, a trust, a nominee), is **CLIENT-BLOCKED**. There
  is a defensible answer at 25% multiplied through the chain and a defensible
  answer at any-link-25%, and choosing between them is a policy decision, not an
  engineering one.

`adr/0009-graph-store-for-identity-traversal.md` already measured traversal cost
for identity links and concluded the relational path is adequate at this scale;
that conclusion holds here and no graph store is proposed.

## §3. Sanctions screening — evidence and state

### 3.1 The state that must exist

Today `sanctions_screened` is a constant in a response model. It must become a
**recorded fact about a screen that ran**, and the record must answer four
questions a reviewer will ask: who was screened, against what, when, and with
what outcome.

Design, no migration written:

| Column (on a `sanctions_screenings` table) | Rule |
|---|---|
| `subject_type` | `applicant` or `beneficial_owner` — an entity's owners are screened individually, not implied by screening the entity |
| `subject_id` | FK to the row screened |
| `provider` | the provider identity, from configuration |
| `list_version` | the provider's own identifier for the list state used. **A screen with no list version is not evidence**: "clear against the SDN List" without saying which day's list is a claim that cannot be reproduced |
| `request_key` | the caller's idempotency key — see §4 |
| `provider_reference` | the provider's non-sensitive handle for the operation, persisted so a later review can re-fetch the result instead of re-screening. Same role as `decision_attempts.bureau_reference_id` |
| `outcome` | `clear`, `potential_match`, or `error`. **Three, not two** — see 3.2 |
| `screened_at` | when the provider answered, not when the row was written |
| `raw_response` | **NOT stored.** A hit's payload is third-party list data about named individuals; the reference id is what a re-fetch needs |

And on `kyc_checks`, replacing nothing that exists: `sanctions_screening_id`
referencing the screen that supports this CIP result, `NULL` meaning **not
established** — the same convention `cip_passed` already uses for NULL.

### 3.2 A potential match is not a decision

The provider's answer is `clear`, `potential_match` or `error`. What happens to
an application on a `potential_match` — cleared by a human as a false positive,
escalated, refused, held — is **disposition policy**. It is not specified here.

- who may clear a potential match, and on what evidence: **COMPLIANCE-BLOCKED**
- what the applicant is told while a match is pending, given that a sanctions
  match may not be disclosable: **COMPLIANCE-BLOCKED**
- how long a hold may persist before the application is withdrawn:
  **CLIENT-BLOCKED**

What *is* specified: a `potential_match` MUST NOT auto-resolve to `clear`, MUST
NOT be recorded as `clear`, and MUST NOT allow `cip_passed = true` (§7).

### 3.3 Matching thresholds are not invented here

Name matching is fuzzy, and every provider expresses confidence differently.
This spec sets **no** match-score threshold, no name-distance metric and no
transliteration rule. A number here would look like a control and would be a
guess with a compliance consequence: too high and a sanctioned party clears, too
low and the queue fills until operators stop reading it — the same failure the
reconciliation control already had to undo once (`docs/DEBT.md` D7).

Threshold selection and tuning: **VENDOR-BLOCKED** (the provider's scoring
semantics are not documented in this repository) and **COMPLIANCE-BLOCKED** (the
false-negative appetite is a compliance judgement).

## §4. `SanctionsScreeningProvider` — the abstraction

Deliberately the same shape as `services/decision-service/app/bureau.py`, which
already solved this problem once for the credit bureau: a `Protocol` naming the
seam, a deterministic stub for dev and test, an HTTP implementation that pins
the requirements a real integration must meet, and a docstring stating plainly
that real-provider behaviour is unverified.

```python
@dataclass(frozen=True)
class ScreeningResult:
    outcome: str            # "clear" | "potential_match" | "error"
    list_version: str       # the provider's identifier for the list state used
    reference_id: str       # non-sensitive handle, safe to persist and log
    match_count: int        # how many candidates; NOT a score, NOT a verdict


class SanctionsScreeningProvider(Protocol):
    """The seam a real screening vendor must satisfy.

    `request_key` is the caller's idempotency key. An implementation MUST
    forward it to the provider so that repeating a call with the same key
    returns the ORIGINAL screen rather than performing a new one.
    """

    async def screen(
        self,
        *,
        name: str,
        dob: str | None,
        address: str | None,
        request_key: str,
    ) -> ScreeningResult:
        ...
```

Requirements on any implementation:

1. **Identity data goes in the request body, never a query string.** This is the
   defect `bureau.py` was written to fix (`?ssn=...` lands in the provider's
   access logs and every proxy in between); repeating it with a different vendor
   is the mistake the brief's own "same abstraction shape" note is trying to
   prevent.
2. **No raw provider payload is persisted or logged.** Outcome, list version,
   reference id and candidate count only.
3. **The stub honours the same idempotency contract as the real provider**, so a
   retry can be tested without a vendor.
4. **The stub is not a sanctions list.** It MUST NOT ship with names that
   resemble real SDN entries; its `potential_match` case is triggered by an
   obviously synthetic marker. Committing anything that looks like list data
   would create a file people mistake for the list.
5. **Which vendor, and the contract for list currency** (how often the list is
   refreshed, and what staleness makes a screen invalid): **VENDOR-BLOCKED**. No
   provider is selected, and `provider` is configuration.

## §5. Idempotency, retries and failure

The bureau path already proved what goes wrong without this: an ambiguous
timeout, a retry, and a second independently-billed operation against a real
person. Screening has the same shape and one additional consequence — a
duplicate screen writes a second piece of evidence about the same subject, and
two evidence rows that disagree are worse than one.

- **Idempotency key.** Every screen carries a `request_key`, stable across
  retries of the same logical onboarding step and regenerated for a genuinely new
  one. Same rule, and the same reasoning, as
  `origination-service/app/decision_state.py::start_decision_attempt`.
- **A replay returns the original screen.** Not a new screen with a matching
  key, and not a cached verdict — the original operation, with its original
  `list_version`.
- **Fail closed.** A provider timeout, transport error, malformed response,
  unparseable outcome or missing `list_version` records `outcome = 'error'` and
  **MUST NOT** produce `cip_passed = true`. There is no degraded mode in which
  screening is skipped and onboarding continues; "the vendor was down" is not a
  reason to onboard an unscreened party, and it is the exact shape of the
  fallback `decision-service` already removed (RF-1: a missing model must fail
  closed rather than fall back to a stub score).
- **Fail closed atomically.** A refusal must not leave a `kyc_checks` row
  claiming a screen that did not complete, nor a screening row for a CIP result
  that was never written. Whichever write happens first, neither half may survive
  alone — the rule spec 0003 §1.4 already states for adverse-action reasons.
- **A stub in a non-development environment is a configuration error, not a
  fallback.** `ALLOW_MODEL_STUB` and the `-stub` model-version suffix are the
  precedent: the stub is selectable and always identifies itself in what it
  writes.

## §6. Ongoing monitoring and the SAR boundary

### 6.1 Trigger points, which is what a spec can honestly give

Monitoring is not "run the screen again sometimes". It is a set of events that
make prior evidence stale. Those events are identifiable from this repository's
own data model, and they are the deliverable:

| Trigger | Why it invalidates prior evidence |
|---|---|
| A new application by an existing applicant | `kyc_checks.application_id` exists precisely because a prior applicant-level pass does not cover a new application (`db/migrations/0032`) |
| A change to an applicant's identity fields (`name`, `dob`, `address`, `ssn`) | the screen was performed against the old values |
| Any `beneficial_owners` insert, supersede or ownership change | a new owner has never been screened |
| A list refresh by the provider | a party clear against last month's list is not clear against today's; this is the trigger that makes `list_version` load-bearing |
| A boarded loan reaching servicing | the relationship continues after onboarding, which is the whole point of *ongoing* |

**Frequency, and re-screening the existing portfolio, are not set here.** A
cadence is a cost and coverage decision: **CLIENT-BLOCKED** for the business
appetite, **OPS-BLOCKED** for who runs the recurring job and receives its
output — and that second one is the same gap `docs/DEBT.md` D7 already carries.
No scheduler is specified, and none should be built before there is somewhere
for its findings to go.

### 6.2 The SAR boundary, stated as a boundary

This spec identifies **where a SAR question would arise**. It specifies no SAR
rules whatsoever.

Not specified, and not to be inferred from anything above:

- what activity is reportable — **COMPLIANCE-BLOCKED**
- filing thresholds, deadlines and the continuing-activity rule —
  **COMPLIANCE-BLOCKED**
- who decides to file, and who signs — **COMPLIANCE-BLOCKED** for the authority,
  **OPS-BLOCKED** for the on-call path
- tipping-off constraints and what may be shown in the UI while a report is
  contemplated — **COMPLIANCE-BLOCKED**

What the system may do without any of those answers is narrow, and it is worth
saying because it is genuinely useful: **preserve the evidence**. The screening
record, the ownership record and their provenance are what any future
investigation reads, and a design that overwrites them forecloses a decision
nobody has made yet. That is the only SAR-adjacent requirement here.

## §7. Acceptance criteria

Criteria for the implementation this spec does not perform. Each is checkable.

1. **No applicant reaches `cip_passed = true` without a completed sanctions
   screen returning `clear`** for the applicant and, where `is_entity` is true,
   for every recorded beneficial owner. Asserted on the persisted row, not on a
   response field.
2. **An entity with no recorded control person cannot reach `cip_passed = true`.**
   The Northgate Holdings LLC seed row is the fixture: it must fail under the new
   rule, and a test asserting today's pass is the honest starting point.
3. **A `potential_match` never becomes `clear`** by retry, timeout, absent
   disposition or any default. It blocks `cip_passed` and waits for a decision
   this repository does not model.
4. **A provider error blocks rather than skips**, and leaves no partial state:
   no `kyc_checks` row claiming a screen that did not complete, no screening row
   for a CIP result that was never written.
5. **A retried screen with the same `request_key` performs one screen**, and the
   replay returns the original `list_version` and `reference_id`. Asserted
   against a stub that counts real screens, as
   `StubBureauClient.pull_count` already allows for the bureau.
6. **A screen with no `list_version` is not evidence** — it is rejected, not
   stored as `clear`.
7. **Ownership arithmetic is checked**: recorded `ownership_pct` for one entity
   sums to at most 100, and a row with neither an ownership percentage nor a
   control-person flag is refused.
8. **No raw provider response and no beneficial-owner PII reaches a log line.**
   The existing test shape is `kyc-service/tests/test_kyc_pii_not_logged.py`,
   which executes the logging path rather than reading the code.
9. **`cip_passed` is never described as KYC/AML compliance** in any response,
   document or screen — §1.
10. **Nothing in the repository contains sanctions-list-like data**, including
    fixtures.

## Non-goals

- **Integrating a real screening vendor.** No provider is selected, no endpoint
  is configured, no contract exists. VENDOR-BLOCKED.
- **Match thresholds, scoring or transliteration rules** — §3.3.
- **Sanctions-match disposition policy**, including who may clear a false
  positive — §3.2.
- **SAR rules, triggers, deadlines, ownership or escalation** — §6.2.
- **A monitoring scheduler**, or any alerting destination for its output. The
  Week 7 lesson applies: rules that fire where nobody receives them are silence
  dressed as coverage (`docs/DEBT.md` D7).
- **A risk-rating model** for customers or geographies. Nothing in this
  repository has the data or the authority for one.
- **Making CIP look thorough.** The brief asks for this in as many words. A
  screen that always returns clear, a `ubo_captured` flag set by hand, or an
  `is_entity` applicant marked verified with no human named would all satisfy the
  request and would be worse than the current honest gap — because the current
  gap is visible in `CipCheckOut` and in `docs/DEBT.md` D11, and a fake control
  is not.
- **Closing D11.** This is the specification. D11 stays open, and its status
  should be read alongside this document.
- **Beneficial-ownership *reporting*** (the CTA/BOI regime), which is a different
  obligation from the collection modelled here. COMPLIANCE-BLOCKED.

## Blocked, and by whom

| What | Label | The decision needed |
|---|---|---|
| Which BSA/AML obligations bind Meridian at all | **COMPLIANCE-BLOCKED** | charter, licensing, regulator — none recorded in this repository |
| Sanctions-match disposition, and who may clear one | **COMPLIANCE-BLOCKED** | authority to clear, and the evidence standard |
| SAR rules, deadlines, signing authority, tipping-off limits | **COMPLIANCE-BLOCKED** | the whole regime; nothing is modelled |
| False-negative appetite behind any match threshold | **COMPLIANCE-BLOCKED** | the appetite, before a number can mean anything |
| Screening vendor selection and list-currency contract | **VENDOR-BLOCKED** | which provider, refresh cadence, staleness limit |
| Provider match-scoring semantics | **VENDOR-BLOCKED** | documented scoring, before a threshold is chosen |
| Whether owner SSN/ITIN is collected at all | **CLIENT-BLOCKED** | data-minimisation decision, and it interacts with Week 10's redaction design |
| Ownership-chain depth and the multiply-through-the-chain rule | **CLIENT-BLOCKED** | which of two defensible rules Meridian uses |
| Re-screening cadence and portfolio backfill | **CLIENT-BLOCKED** | business appetite and cost |
| Who runs the monitoring job and receives its findings | **OPS-BLOCKED** | an owner and an escalation path — the same gap as D7 |

**Nothing in the tables above may be filled in by this repository.** Every one of
them has at least two defensible answers, and picking one in code is how the
maker-checker thresholds became a finding (`docs/DEBT.md` D8) before they were
approved.

## Evidence and test strategy

- `db/tests/test_week9_kyc_aml_spec.py` guards the claims this document and
  ADR 0012 make about themselves: that CIP is not presented as full KYC/AML, that
  no match threshold or SAR rule is invented, that every unavailable authority
  carries a label, that the provider seam is specified with idempotency and
  fail-closed behaviour, and that neither document claims a vendor is integrated.
- The implementation's own tests are §7. They are not written, because the
  implementation is not written.
- Existing tests this spec must not break: `kyc-service/tests/test_cip.py`,
  `test_kyc_api.py`, `test_kyc_pii_not_logged.py`,
  `test_kyc_requires_internal_token.py`.

## What a reader should take from this

Today an LLC can onboard with no human named and no party screened, and the two
fields that would say so are constants. That is D11, it is deliberate, and it is
recorded. This document says what closing it requires, and marks the ten
decisions that are not this repository's to make. The next honest step is not
code — it is answers to the four COMPLIANCE-BLOCKED rows, because every design
below them changes depending on what they say.
