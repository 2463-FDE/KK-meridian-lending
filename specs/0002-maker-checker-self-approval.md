# Spec 0002 — Maker-checker for servicing money movements

- **Status:** Draft, for review before implementation begins
- **Date:** 2026-08-12
- **Closes:** `docs/DEBT.md` **D8** — fee waiver / balance adjust is available to
  *any* authenticated user, with no role check, no second approver and no ledger
  entry
- **Depends on:** ADR 0010 (the ledger gives a proposal somewhere to point) and
  ADR 0011 (the database design this spec makes testable), both on PR #20. This
  spec is written against 0011 as finalised there: the proposal carries
  `requested_role` and `resolved_role`, its substance including `reason` and
  `requested_at` is frozen by the transition trigger, and the commit-time rule
  re-reads the proposal rather than the queued row
- **Written before the code**, deliberately. D8 has been open since the original
  vendor delivery, and the reason it stayed open is that "add an approval step"
  is not a requirement — it is a wish. This says what must be true.

> ### This document specifies the control. It does not implement it.
>
> **Nothing described below exists yet.** There is no `pending_movements` table,
> no approval endpoint, no role check on either money route, and no ledger. D8 is
> **open**, and it stays open until the implementation PR lands and its tests
> pass — merging this changes nothing about what the running system permits.
>
> `db/tests/test_spec_0002_describes_the_real_system.py` asserts that, from both
> directions: §1's description of today's system must stay true, and this
> document must not claim the control is in place. When maker-checker is
> implemented those tests fail, and that failure is the instruction to rewrite §1
> as *what it was* — not to delete the test.
>
> The distinction matters here more than usual. This codebase has twice shipped a
> document that read as a description of a working control and was a description
> of an intention: a policy publishing DTI cutoffs nothing evaluated, and a
> reconciliation "control" that never ran. A spec that is mistaken for an
> implementation is the same failure a third time.

---

## 1. Current state

Two endpoints move money with no approval of any kind:

| Endpoint | What it does | Who may call it today |
|---|---|---|
| `POST /accounts/{loan_id}/adjust-balance` | Sets `balances.balance` to an arbitrary value | Any caller holding the internal service token |
| `POST /accounts/{loan_id}/waive-fee` | Reduces `balances.past_due` | Any caller holding the internal service token |

Verified against `services/servicing-service/app/main.py`: both take
`x_user_role` as an **optional header and never read it**. The comment on
`adjust_balance` says so in the source — *"ANY authenticated user. No role check,
no second approver, no ledger entry."*

What that means concretely, and why it is D8 rather than a hardening nicety:

- **no second person** — one staff account can zero a borrower's balance alone;
- **no record of who** — the prior value is overwritten in place, so after the
  fact the system cannot say what the balance was or who changed it;
- **no record of why** — there is no reason field on either path;
- **no role restriction** — a CSR and an administrator are indistinguishable to
  these endpoints.

PR #22 closed the *network* half of this: both routes now require
`X-Internal-Token`, so they are not reachable from outside the compose network.
That answers **who can reach the endpoint** and says nothing about **who may
authorise the movement**. The two are independent; closing either leaves the
other open.

## 2. Target state

A money movement proposed by staff is a **request** until a *different*
authorised person approves it. Approval is what writes the ledger entry;
proposing writes nothing to `balances`.

```
CSR raises an adjustment  ──▶  pending_movements row      (balance unchanged)
                                      │
              approver (a different account) resolves it
                                      │
                     ┌────────────────┴────────────────┐
                 approved                           rejected
                     │                                  │
        one ledger_entries row                  no ledger entry;
        projection moves the balance            the proposal is retained
```

## 3. Role matrix

Roles are the ones the system already has (`_STAFF_ROLES = {csr, underwriter,
admin}`) — this spec does not invent a new role hierarchy, because an
unimplementable matrix is how the last version of this control stayed a document.

| Action | csr | underwriter | admin | Notes |
|---|:--:|:--:|:--:|---|
| Raise a proposal | ✅ | ✅ | ✅ | Any staff member may ask |
| Approve ≤ threshold | ❌ | ✅ | ✅ | A CSR may not approve any amount |
| Approve > threshold | ❌ | ❌ | ✅ | The larger the movement, the narrower the set |
| Reject | ❌ | ✅ | ✅ | Rejecting is an authorisation decision too |
| Approve **own** proposal | ❌ | ❌ | ❌ | **No exception, including admin** |
| View the queue | ✅ | ✅ | ✅ | Visibility is not authority |

**Threshold:** `MAKER_CHECKER_ADMIN_THRESHOLD`, applied to `ABS(amount)`.

**It has no default, and a missing or unparseable value fails closed.** An
earlier draft of this spec set it to `$500.00`. Nobody chose that number: it is
not in `policies/`, no stakeholder stated it, and it does not appear anywhere in
this system. A specification that invents a monetary control limit and writes it
down in the tone of a requirement is the exact failure this repository has
already had to correct in `policies/underwriting_guidelines.md`, where published
DTI cutoffs described nothing the code evaluated. A wrong number that looks
authorised is worse than a missing one, because the missing one gets asked about.

- **REQ-CFG-1** — `MAKER_CHECKER_ADMIN_THRESHOLD` SHALL be read from configuration
  and SHALL have no default value in code.
- **REQ-CFG-2** — IF it is unset, empty, unparseable or negative, THEN the service
  SHALL refuse to start, the same fail-closed treatment
  `config.validate_internal_token()` already gives `INTERNAL_SERVICE_TOKEN`. It
  SHALL NOT fall back to a built-in figure, and it SHALL NOT treat the absence as
  "no threshold".
- **REQ-CFG-3** — IF the service is running and the value cannot be resolved for a
  given resolution, THEN that resolution SHALL be refused (`503`) rather than
  evaluated against an assumed limit.
- **REQ-CFG-4** — The value SHALL be recorded on the proposal's resolution, so a
  later reader can tell which limit a decision was judged against. A history of
  approvals is unreadable if the bar moved and nothing says when — the same rule
  `reconciliation_runs.threshold_value` follows.

**Setting the number is a business decision this document does not make.** It
belongs to whoever owns servicing risk, and the implementation PR must carry
their answer rather than a placeholder.

**Why admin cannot self-approve.** The obvious objection is that an admin can do
anything anyway. That is true of the *database* and not of the *application*, and
the distinction is the control: an admin who wants to move money alone has to
leave the application to do it, which is exactly the signal an audit is looking
for. An admin self-approval exception would make the bypass indistinguishable
from ordinary work.

## 3a. Identity — where `requested_by` and `resolved_by` come from

**The whole control reduces to one question: are these two the same person?** If
identity can be asserted by the caller, "a different approver" means "a different
string in a header", and maker-checker becomes a naming convention.

### The rule

- **REQ-ID-1** — `requested_by`, `resolved_by` and the role used for the authority
  check SHALL be taken **only** from the authenticated server-side principal — the
  session the gateway resolved — and never from a value supplied by the client.
- **REQ-ID-2** — The gateway SHALL strip every inbound `X-User-*` header before
  proxying and re-stamp them from the resolved session. *(This already holds:
  `services/gateway/app/main.py::_proxy` filters `x-user-` and sets `X-User-Id` /
  `X-User-Role` from `user`.)*
- **REQ-ID-3** — The canonical human principal SHALL be the account resolved by
  the gateway from its server-side Redis session. The gateway SHALL convey that
  principal in a short-lived, audience-bound assertion signed with a gateway-only
  asymmetric private key. Servicing SHALL independently verify the signature,
  issuer, `servicing-service` audience, issued-at/expiry bounds, subject and role
  before using either identity or authority. The verification key may be shared;
  the signing key SHALL NOT be available to any other backend.
- **REQ-ID-4** — IF the principal cannot be resolved — no session, unknown user id,
  a role outside `_STAFF_ROLES` — THEN both proposing and resolving SHALL fail
  closed with `401`, and no `pending_movements` row SHALL be written and no
  resolution recorded.
- **REQ-ID-5** — The self-approval check SHALL compare **resolved principals**,
  not header values. Two requests carrying different `X-User-Id` headers but
  resolving to the same account are the same person.
- **REQ-ID-6** — The proposal's `requested_by` SHALL be written from the principal
  at creation time and SHALL be immutable thereafter. A caller SHALL NOT be able
  to set, supply or amend it.
- **REQ-ID-7** — `X-Internal-Token` SHALL authenticate only that the caller is a
  recognised service. It SHALL NOT authenticate a human, SHALL NOT make
  `X-User-Id` or `X-User-Role` trustworthy, and SHALL NOT substitute for a valid
  signed principal assertion. Another backend possessing the shared token must
  be unable to mint or alter the human principal.
- **REQ-ID-8** — `X-User-Id`, `X-User-Role`, body fields and query parameters are
  untrusted hints even when accompanied by a valid service token. If they differ
  from the verified assertion they SHALL be ignored for identity and authority;
  the action SHALL be refused as an attempted identity mismatch and write
  nothing.
- **REQ-ID-9** — A missing, malformed, expired, not-yet-valid, wrongly issued,
  wrongly scoped or unverifiable assertion SHALL fail closed with `401` for both
  proposal and resolution. The service SHALL NOT fall back to headers, the
  shared token, a database lookup by caller-supplied id, or an anonymous/system
  actor.
- **REQ-ID-10** — A service-to-service request with no human principal MAY use the
  existing shared token for machine endpoints, but SHALL NOT create or resolve a
  staff maker-checker proposal. Machine-originated payments and fee assessments
  remain outside this workflow as stated in §8.

### The required boundary does not exist yet

Servicing's money routes take `x_user_role` as a header today and never read it.
The tempting implementation is to start reading it — and that is a bypass, not a
fix: any caller holding the internal token could then set `X-User-Role: admin` and
approve. The token is a *service* credential shared by every backend, not a *user*
credential, so it authenticates the caller as "a service on this network" and says
nothing about which human is acting.

The running repository has no signed principal assertion, gateway-only signing
key, or servicing-side verifier today. `gateway::_proxy` strips and re-stamps
headers, but every backend knows the same `X-Internal-Token`; another backend can
therefore call servicing directly and forge those re-stamped values. This spec
requires the signed assertion above for the future implementation and does not
claim the present headers satisfy it.

| Missing | Consequence |
|---|---|
| Service token not checked | An unrecognised network caller reaches the endpoint |
| Headers not stripped at the gateway | A browser client supplies its own `X-User-Id`, and the gateway forwards it |
| Signed principal not verified | Any backend with the shared token forges a human or claims `admin` |
| Header disagrees with assertion | The header becomes a self-approval or privilege-escalation primitive unless the request refuses |

## 3b. What a proposal may say — validity at creation

**Guarding only the approval step puts the entire control on one tired human.**
The role matrix lets a CSR raise a proposal, and the endpoint being replaced sets
`balances.balance` to an arbitrary value — so without this section a CSR can
stage any balance change at all and rely on an approver catching it. Because
approval copies the proposal's fields straight into the ledger, one
rubber-stamped approval under queue pressure becomes a real, irreversible money
movement.

So a proposal that cannot be executed, or should never have entered the queue, is
refused **when it is raised** — not when it is approved. An approver should never
be shown a request that the system was always going to refuse.

### A proposal is a signed delta, never a target balance

- **REQ-VAL-1** — A proposal SHALL carry a **signed delta** on a named component,
  never a target balance. `adjust-balance` sets `balances.balance` to whatever it
  is given today; that is the shape being replaced, and it is unreviewable —
  "set the balance to 250.00" cannot be judged without knowing what it is now,
  and what it is now can change between the review and the approval.
- **REQ-VAL-2** — `component` SHALL be one of `principal`, `interest`, `fees` —
  the same vocabulary the ledger holds (ADR 0010). Any other value SHALL be
  refused at creation. Without this, the mismatch surfaces at the ledger insert,
  **after** a human has already reviewed and accepted the request.
- **REQ-VAL-3** — A `fee_waived` proposal SHALL target `fees` and nothing else. A
  waiver of principal is not a waiver; ADR 0011 refuses it at insert with
  `pending_fee_waiver_is_fees`, and this spec refuses it at the API boundary so
  the caller gets a reason rather than a constraint violation.
- **REQ-VAL-4** — The sign SHALL match the direction the entry type can move
  money. A `fee_waived` reduces what the borrower owes and SHALL be **negative**;
  an `adjustment` may be either sign, because a correction can go both ways, and
  which way is the substance of the request.
- **REQ-VAL-5** — `amount` SHALL be non-zero. A zero movement is not a correction;
  ADR 0010's ledger refuses it with `CHECK (amount <> 0)`, and a proposal that
  can only fail at approval should not occupy an approver's queue.

### Bounded, and bounded by a configured number

- **REQ-VAL-6** — `ABS(amount)` SHALL NOT exceed `MAKER_CHECKER_MAX_DELTA`. A
  proposal above it is refused **at creation**, by anyone, regardless of who might
  approve it. This is distinct from `MAKER_CHECKER_ADMIN_THRESHOLD`, which raises
  the approver bar rather than refusing the request: one says *who may say yes*,
  the other says *what may be asked*.
- **REQ-VAL-7** — `MAKER_CHECKER_MAX_DELTA` SHALL be configured with **no default**
  and SHALL fail closed exactly as REQ-CFG-2 to REQ-CFG-4 require. This document
  states no figure, for the reason §3 gives.
- **REQ-VAL-8** — A proposal SHALL NOT take the targeted component below zero. A
  waiver cannot forgive more fees than exist and an adjustment cannot drive a
  principal balance negative; both would produce a balance no reader can
  interpret. This bound needs no configured number — it follows from the data.
- **REQ-VAL-9** — The bound in REQ-VAL-8 SHALL be re-checked **at approval**
  against the balance as it then stands, not only at creation. A proposal raised
  when the fees were 80.00 and approved after they were paid down to 10.00 was
  valid when written and is not valid when executed.

### A reason, at creation

- **REQ-VAL-10** — `reason` SHALL be present and non-empty **when the proposal is
  created**, not supplied later and not defaulted. A proposal without one is
  unreviewable: the approver is being asked to authorise a number with no account
  of why. ADR 0011 makes the column `NOT NULL`; this makes it a validation the
  caller gets a message about.
- **REQ-VAL-11** — `reason` SHALL be immutable from creation, before and after
  resolution (ADR 0011's transition trigger freezes it). An editable reason is a
  note, not evidence: it would let the account of a movement be rewritten after a
  second person approved the account they were shown.

### The target must be one the system can and should move

- **REQ-VAL-12** — The `loan_id` SHALL name an existing loan **with a `balances`
  row**. A loan that exists and was never opened in servicing has nothing to
  project onto, and the movement would be approved and then land nowhere.
- **REQ-VAL-13** — The loan SHALL be in a status the implementation explicitly
  permits movements on, and **an unrecognised status SHALL refuse**. This spec
  names no status list: `loans.status` is an unconstrained `TEXT` column
  defaulting to `'current'`, and enumerating values here that the schema does not
  enforce would be inventing a vocabulary. The implementation PR SHALL declare the
  permitted set in code, with a test, and refuse anything outside it.
- **REQ-VAL-14** — The maker SHALL be authorised for the specific target, not
  merely authenticated as staff. **This system has no data model for that today**
  — there is no assignment of loans to staff anywhere in the schema — so the
  implementation PR SHALL do one of exactly two things, explicitly and in
  writing:
  1. introduce the scope (a staff-to-loan assignment, or a scope claim on the
     resolved session) and enforce it; or
  2. record the decision that any staff role may propose on any serviced loan,
     as a reviewed limitation with its reason.

  What it SHALL NOT do is leave the requirement unaddressed, which is how
  "authorised for the target" becomes "authenticated at all" without anyone
  choosing it. Whichever is chosen, an unevaluable predicate SHALL refuse.

**None of this weakens the approval step.** Every one of these checks runs again
at approval where it can (REQ-VAL-9 says so explicitly), because a proposal that
sat in a queue was validated against a system state that has since moved.

## 4. Acceptance criteria

### 4.1 EARS

- **AC-1** — WHEN a staff member submits an adjustment or fee waiver, THE SYSTEM
  SHALL create a `pending_movements` row and SHALL NOT change `balances`.
- **AC-2** — WHEN an approver resolves a pending movement as approved, THE SYSTEM
  SHALL write exactly one `ledger_entries` row whose `loan_id`, `component`,
  `amount` and `entry_type` equal the proposal's.
- **AC-3** — WHEN an approver resolves a pending movement as rejected, THE SYSTEM
  SHALL write no ledger entry and SHALL retain the proposal.
- **AC-4** — IF the resolving account equals `requested_by`, THEN THE SYSTEM SHALL
  refuse the resolution and SHALL NOT change any balance.
- **AC-5** — IF a pending movement already has a resolution, THEN THE SYSTEM SHALL
  refuse any further resolution.
- **AC-6** — IF the movement's `ABS(amount)` exceeds
  `MAKER_CHECKER_ADMIN_THRESHOLD`, THEN THE SYSTEM SHALL require an `admin`
  approver.
- **AC-7** — WHILE the write-guard on `balances` is enabled, THE SYSTEM SHALL
  refuse any direct write to `balances` from `adjust_balance` or `waive_fee`.
- **AC-8** — WHERE two approvers resolve the same proposal concurrently, THE
  SYSTEM SHALL apply exactly one resolution and exactly one ledger entry.
- **AC-9** — IF a request supplies `X-User-Id` or `X-User-Role` without a valid
  `X-Internal-Token`, THEN THE SYSTEM SHALL ignore those headers and refuse the
  action.
- **AC-10** — IF the authenticated principal cannot be resolved, THEN THE SYSTEM
  SHALL refuse both proposal and resolution and SHALL write nothing.
- **AC-11** — WHEN a proposal is created, THE SYSTEM SHALL set `requested_by` from
  the resolved principal and SHALL ignore any requester supplied in the body.
- **AC-12** — THE SYSTEM SHALL retain the substance and the audit fields of a
  proposal immutably, before and after resolution: `loan_id`, `component`,
  `amount`, `entry_type`, `reason`, `requested_by`, `requested_role`,
  `requested_at`, and after resolution `resolved_by`, `resolved_role`,
  `resolved_at` and the linked ledger entry. `reason` and `requested_at` are in
  that list because the reason is the evidence D8 says is missing, and one that
  can be rewritten after approval is a note (ADR 0011, transition trigger).

**Refused at creation.** Each of these SHALL be refused when the proposal is
raised — not at approval — and SHALL write no `pending_movements` row.

- **AC-13** — IF `component` is not one of `principal`, `interest`, `fees`, THEN
  THE SYSTEM SHALL refuse the proposal (`422`) and SHALL name the permitted set.
- **AC-14** — IF `entry_type` is `fee_waived` AND `component` is not `fees`, THEN
  THE SYSTEM SHALL refuse the proposal.
- **AC-15** — IF the sign of `amount` is not one the entry type can produce — a
  positive `fee_waived`, or a zero amount of either type — THEN THE SYSTEM SHALL
  refuse the proposal.
- **AC-16** — IF `ABS(amount)` exceeds `MAKER_CHECKER_MAX_DELTA`, THEN THE SYSTEM
  SHALL refuse the proposal regardless of the proposer's role, and SHALL NOT
  offer it to an approver.
- **AC-17** — IF `reason` is absent, empty or whitespace, THEN THE SYSTEM SHALL
  refuse the proposal. A reason SHALL NOT be defaulted or supplied later.
- **AC-18** — IF the loan does not exist, has no `balances` row, or is in a status
  the implementation does not explicitly permit movements on, THEN THE SYSTEM
  SHALL refuse the proposal. An unrecognised status SHALL refuse.
- **AC-19** — IF the proposer is not authorised for the specific target loan under
  whichever rule the implementation adopts (REQ-VAL-14), THEN THE SYSTEM SHALL
  refuse the proposal. IF that predicate cannot be evaluated, THEN THE SYSTEM
  SHALL refuse.
- **AC-20** — IF the proposal would take the targeted component below zero, THEN
  THE SYSTEM SHALL refuse it at creation AND, if it was valid at creation and is
  not at approval, at resolution.
- **AC-21** — IF `MAKER_CHECKER_ADMIN_THRESHOLD` or `MAKER_CHECKER_MAX_DELTA` is
  unset or unparseable, THEN THE SYSTEM SHALL refuse to start; and IF the value
  cannot be resolved while running, THEN THE SYSTEM SHALL refuse the action
  (`503`) rather than evaluate it against an assumed limit.
- **AC-22** — WHEN a resolution is recorded, THE SYSTEM SHALL record the threshold
  value it was judged against.
- **AC-23** — IF forged `X-User-Id` or `X-User-Role` headers accompany a valid
  shared service token but no valid signed principal assertion, THEN THE SYSTEM
  SHALL refuse the proposal or resolution and write nothing.
- **AC-24** — IF a forged identity or role header disagrees with a valid signed
  principal assertion, THEN THE SYSTEM SHALL use neither value to broaden
  authority, SHALL refuse the request, and SHALL write nothing.
- **AC-25** — IF the same verified subject proposes and resolves a movement while
  presenting different identity headers, THEN THE SYSTEM SHALL refuse it as
  self-approval.
- **AC-26** — IF the signed principal assertion is missing, malformed, expired,
  wrongly issued, wrongly scoped or has an invalid signature, THEN THE SYSTEM
  SHALL return `401` and SHALL NOT fall back to the service token or headers.
- **AC-27** — WHEN a service calls servicing with a valid shared service token but
  no validated human principal, THEN THE SYSTEM SHALL refuse maker-checker
  proposal and resolution while leaving explicitly machine-only endpoints under
  their separate authorization rules.
- **AC-28** — WHEN servicing accepts a maker-checker action, THE recorded subject
  and role SHALL equal the independently verified assertion claims derived from
  the gateway's server-side session.

### 4.2 Gherkin

```gherkin
Scenario: the requester cannot approve their own proposal
  Given a CSR "alice" has raised an adjustment of -250.00 on loan 4471
  When "alice" attempts to approve that proposal
  Then the resolution is refused
  And the proposal remains unresolved
  And the balance of loan 4471 is unchanged

Scenario: an approval moves the balance exactly once
  Given a CSR "alice" has raised an adjustment of -250.00 on loan 4471
  And the balance of loan 4471 is 1000.00
  When an underwriter "bob" approves that proposal
  Then exactly one ledger entry exists for that proposal
  And the entry records "bob" as the actor
  And the balance of loan 4471 is 750.00

Scenario: a rejection keeps the evidence and moves nothing
  Given a CSR "alice" has raised a fee waiver of -40.00 on loan 4471
  When an underwriter "bob" rejects it with a reason
  Then no ledger entry is written
  And the proposal is retained with resolution "rejected" and "bob" recorded
  And the past_due of loan 4471 is unchanged

Scenario: a large movement needs an admin
  Given a CSR "alice" has raised an adjustment of -5000.00 on loan 4471
  And MAKER_CHECKER_ADMIN_THRESHOLD is 500.00
  When an underwriter "bob" attempts to approve it
  Then the resolution is refused for insufficient authority
  When an admin "carol" approves it
  Then exactly one ledger entry exists for that proposal

Scenario: a spoofed requester header is ignored
  Given an unauthenticated client can reach servicing directly on the network
  When it posts an adjustment with header "X-User-Id: 7" and no internal token
  Then the request is refused
  And no pending movement is created

Scenario: a spoofed role header does not grant approval authority
  Given a CSR "alice" is authenticated through the gateway
  And a proposal of -5000.00 exists that requires an admin
  When "alice" sends the approval with header "X-User-Role: admin"
  Then the role is taken from her resolved session, not the header
  And the resolution is refused for insufficient authority

Scenario: a backend cannot forge a human with the shared service token
  Given payment-service possesses the valid shared internal token
  And it has no gateway-signed human principal assertion
  When it sends "X-User-Id: bob" and "X-User-Role: admin" to approve a proposal
  Then the request is refused with 401
  And the proposal remains unresolved
  And no ledger entry is written

Scenario: a forged role cannot override the signed principal
  Given the gateway signed a principal assertion for CSR "alice"
  When the request also supplies "X-User-Role: admin"
  Then the identity mismatch is refused
  And no approval is recorded

Scenario: a forged identity cannot hide self-approval
  Given the gateway signed a principal assertion for "alice"
  And "alice" created the proposal
  When she approves it with "X-User-Id: bob"
  Then the request is refused as self-approval
  And the proposal remains unresolved

Scenario: a missing human principal fails closed
  Given a request carries a valid shared internal token
  And it carries no signed human principal assertion
  When it attempts to create or resolve a proposal
  Then the request is refused with 401
  And nothing is written

Scenario: an invalid human principal fails closed
  Given a request carries an expired, wrongly scoped or invalidly signed assertion
  When it attempts to create or resolve a proposal
  Then the request is refused with 401
  And no header or shared-token fallback is used

Scenario: a machine service remains a machine service
  Given payment-service calls with its valid shared token and no human principal
  When it attempts a staff maker-checker action
  Then the action is refused
  And its separately authorised machine payment path is unaffected

Scenario: identity that cannot be resolved fails closed
  Given a request carries a valid internal token
  And "X-User-Id" names a user that does not exist
  When it attempts to approve a proposal
  Then the resolution is refused
  And the proposal remains unresolved
  And no ledger entry is written

Scenario: the same principal cannot propose and approve under two identities
  Given "alice" raised a proposal
  When a request resolving to "alice" approves it, whatever headers it carries
  Then the resolution is refused as self-approval

Scenario: audit evidence cannot be amended after resolution
  Given a proposal has been approved by "bob"
  When any caller attempts to change requested_by, resolved_by or resolved_at
  Then the change is refused
  And the original values remain

Scenario: two approvers race
  Given a CSR "alice" has raised an adjustment of -250.00 on loan 4471
  When "bob" and "carol" approve it at the same moment
  Then exactly one resolution is recorded
  And exactly one ledger entry exists for that proposal

Scenario: a component the ledger cannot hold is refused when raised
  Given a CSR "alice" raises an adjustment on loan 4471
  When the component is "escrow"
  Then the proposal is refused
  And no pending movement is created
  And no approver is ever shown the request

Scenario: a fee waiver against principal is refused when raised
  Given a CSR "alice" raises a fee waiver of -40.00 on loan 4471
  When the component is "principal"
  Then the proposal is refused
  And no pending movement is created

Scenario: a waiver in the wrong direction is refused
  Given a CSR "alice" raises a fee waiver on loan 4471
  When the amount is +40.00
  Then the proposal is refused
  And the refusal names the direction a waiver may move money

Scenario: an amount above the cap never enters the queue
  Given MAKER_CHECKER_MAX_DELTA is configured
  And a CSR "alice" raises an adjustment whose absolute amount exceeds it
  When the proposal is submitted
  Then it is refused at creation
  And it is refused for an admin proposer as well
  And no pending movement is created

Scenario: a proposal with no reason is refused
  Given a CSR "alice" raises an adjustment of -250.00 on loan 4471
  When the reason is empty or whitespace
  Then the proposal is refused
  And no pending movement is created

Scenario: a loan the system does not service is refused
  Given a CSR "alice" raises an adjustment on a loan with no balances row
  When the proposal is submitted
  Then it is refused
  And no pending movement is created

Scenario: an unrecognised loan status refuses rather than permits
  Given loan 4471 has a status the implementation does not list as permitted
  When "alice" raises an adjustment on it
  Then the proposal is refused

Scenario: a maker not authorised for the target is refused
  Given "alice" is authenticated staff
  And she is not authorised for loan 4471 under the adopted rule
  When she raises an adjustment on loan 4471
  Then the proposal is refused
  And no pending movement is created

Scenario: a proposal cannot drive a component negative
  Given loan 4471 has past_due of 40.00
  When a CSR "alice" raises a fee waiver of -100.00
  Then the proposal is refused

Scenario: a proposal valid when raised is re-checked at approval
  Given a CSR "alice" raised a fee waiver of -80.00 when past_due was 80.00
  And past_due has since been paid down to 10.00
  When an underwriter "bob" approves it
  Then the resolution is refused
  And no ledger entry is written

Scenario: a missing threshold fails closed rather than defaulting
  Given MAKER_CHECKER_ADMIN_THRESHOLD is unset
  When servicing-service starts
  Then it refuses to start
  And no built-in dollar figure is used in its place

Scenario: the reason cannot be rewritten after approval
  Given a proposal has been approved by "bob"
  When any caller attempts to change the reason or requested_at
  Then the change is refused
  And the original values remain
```

## 5. Audit requirements

Every resolved proposal must answer, from stored data and without inference:

| Question | Answered by |
|---|---|
| Who asked? | `pending_movements.requested_by` |
| Who allowed it? | `pending_movements.resolved_by`, copied to `ledger_entries.actor_id` |
| Why? | `pending_movements.reason`, required and non-empty **at creation**, immutable thereafter |
| Under which role? | `requested_role` and `resolved_role`, stored on the proposal and copied to `ledger_entries.actor_role` |
| Against which limit? | The threshold value recorded with the resolution (AC-22) |
| When? | `requested_at`, `resolved_at`, `ledger_entries.occurred_at` |
| What moved? | The ledger entry — loan, component, signed amount |
| What was refused? | Rejected proposals are retained, never deleted |

**Retention:** proposals are retained on the same footing as application records
(Reg B, ~25 months minimum; see `policies/underwriting_guidelines.md`). A rejected
proposal is evidence that a control worked and must outlive the request.

**Not logged:** the reason field is free text entered by staff and may contain
borrower details, so it is stored and **never** written to an application log.
Log lines carry ids and amounts only — the same rule the rest of this codebase
follows.

## 6. Failure behaviour

Every one of these fails **closed**. A money control that degrades to permissive
under load or error is not a control.

| Condition | Behaviour |
|---|---|
| Approver identity cannot be resolved | Refuse (`401`). Do not fall back to a role header |
| A configured limit is unset or unparseable at startup | Refuse to start. Never substitute a built-in figure |
| A configured limit cannot be resolved at runtime | Refuse (`503`). Never evaluate against an assumed limit |
| The proposal names a component, sign or amount the ledger cannot hold | Refuse (`422`) **at creation**, so no approver is shown it |
| The target loan is unknown, unserviced, or in an unpermitted status | Refuse (`422`) at creation |
| The maker's authority for the target cannot be evaluated | Refuse (`403`). An unevaluable predicate is not a pass |
| The movement would drive a component below zero | Refuse at creation **and** re-check at approval |
| The database is unreachable when resolving | Refuse (`503`). Never assume the proposal is unresolved |
| The proposal is already resolved | Refuse (`409`), and say which resolution it carries |
| The requester and approver are the same | Refuse (`403`) |
| Authority is insufficient for the amount | Refuse (`403`), naming the threshold, not the approver set |
| The ledger insert fails after approval | The whole resolution rolls back. A proposal marked approved with no entry is the one state that must never persist |

## 7. Non-functional requirements

- **Concurrency** — resolution takes a row lock on the proposal; two concurrent
  approvals produce one resolution and one entry (AC-8), proven against real
  PostgreSQL rather than a mock.
- **Latency** — resolution is one transaction, expected under 100 ms. It is a
  staff action, not a borrower-facing path; correctness dominates.
- **Availability** — the approval queue being down blocks *adjustments*, not
  payments. Payment application must not depend on this path.
- **Data** — no new PII. `pending_movements` holds ids, amounts, a reason and
  timestamps.

## 8. Out of scope, and named so nobody assumes otherwise

- **Delegation, escalation, and out-of-office approval routing.** Real approval
  workflows need them; this one refuses instead, and the operational cost of that
  is accepted rather than hidden.
- **Notification.** No email or queue alert when a proposal is raised. There is no
  notification infrastructure in this build, and inventing one here would repeat
  the mistake this spec exists to avoid — see §9.
- **Approval for machine-originated movements.** Payments, fee assessments and
  disbursements are not proposals; only `adjustment` and `fee_waived` are.
- **A UI.** The API contract is in scope; the screen is separate work.
- **Retrospective approval of historical adjustments.** Existing balances carry an
  `opening_balance` ledger entry that admits their history is unavailable
  (ADR 0010). They are not back-filled with invented approvers.

## 9. The limit of this control, stated up front

**A direct `INSERT` into `ledger_entries` bypasses all of it.** Every service
connects as the schema-owning role, so `REVOKE` does not stick (ADR 0002, ADR
0006) — the same constraint that makes the append-only trigger necessary makes
privilege-based enforcement unavailable.

What the validation trigger *does* guarantee is narrower and real: an
`adjustment` or `fee_waived` entry cannot exist without naming an approved
proposal with a distinct approver, and its loan, component, amount and type must
match that proposal field-for-field, with `actor_id` overwritten to the approver.

So: **maker-checker here is a control on the application's staff paths, not a
defence against a compromised database credential.** Anyone with direct database
access can still move money within the shape of something already approved.
Claiming otherwise would be the same overclaim this codebase has already had to
correct twice — a policy that published DTI rules nothing evaluated, and a
reconciliation "control" that never ran.
