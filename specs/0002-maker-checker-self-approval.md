# Spec 0002 — Maker-checker for servicing money movements

- **Status:** Draft, for review before implementation begins
- **Date:** 2026-08-12
- **Closes:** `docs/DEBT.md` **D8** — fee waiver / balance adjust is available to
  *any* authenticated user, with no role check, no second approver and no ledger
  entry
- **Depends on:** ADR 0010 (the ledger gives a proposal somewhere to point), ADR
  0011 (the design this spec makes testable)
- **Written before the code**, deliberately. D8 has been open since the original
  vendor delivery, and the reason it stayed open is that "add an approval step"
  is not a requirement — it is a wish. This says what must be true.

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

**Threshold:** `MAKER_CHECKER_ADMIN_THRESHOLD`, default **$500.00**, applied to
`ABS(amount)`. Configurable, but not to infinity — a threshold that disables the
second tier must be a deliberate, reviewable change rather than an environment
variable nobody reads.

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
- **REQ-ID-3** — Servicing SHALL trust `X-User-Id` / `X-User-Role` **only** when
  the request also carries a valid `X-Internal-Token`. Without it those headers
  are anonymous client input. The token proves the request came through the
  gateway; the headers carry what the gateway resolved. **Neither is sufficient
  alone**, and treating the headers as trusted because they are "internal" is the
  assumption this requirement exists to forbid.
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

### Why REQ-ID-3 is the one that gets skipped

Servicing's money routes take `x_user_role` as a header today and never read it.
The tempting implementation is to start reading it — and that is a bypass, not a
fix: any caller holding the internal token could then set `X-User-Role: admin` and
approve. The token is a *service* credential shared by every backend, not a *user*
credential, so it authenticates the caller as "a service on this network" and says
nothing about which human is acting.

That is why the identity must be the gateway-resolved session and the token must
be checked as well. One without the other is:

| Missing | Consequence |
|---|---|
| Token not checked | Anyone on the network sets `X-User-Id` to any staff account and approves |
| Headers not stripped at the gateway | A browser client supplies its own `X-User-Id`, and the gateway forwards it |
| Role read without the token | The role header becomes a privilege escalation primitive |

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
- **AC-12** — THE SYSTEM SHALL retain the audit fields of a resolved proposal
  immutably; `requested_by`, `resolved_by`, `resolved_at` and the linked ledger
  entry SHALL NOT be updatable after resolution.

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
```

## 5. Audit requirements

Every resolved proposal must answer, from stored data and without inference:

| Question | Answered by |
|---|---|
| Who asked? | `pending_movements.requested_by` |
| Who allowed it? | `pending_movements.resolved_by`, copied to `ledger_entries.actor_id` |
| Why? | `pending_movements.reason`, required and non-empty |
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
