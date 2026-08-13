# Meridian Lending — Roadmap

What the client asked for, what was actually found, what got fixed, why it
mattered. One row per finding.

**Domains:** Finance · Origination · KYC · Decisioning · Disclosures · Payments · Servicing

## Definition of done

From the Week 1–4 client review: **on `main`, reachable, reviewed.** Anything
short of that is inventory, not delivery. So the status key distinguishes them:

| | Means |
|---|---|
| ✅ **Landed** | Merged to `main`, reachable in the running app, with a test that fails without it |
| 🟠 **Built in an open PR** | Real working code with tests, on an open PR. Not delivered yet — this is inventory |
| ⚠️ **Training-only / mock** | The contract is real; no vendor behind it |
| ❌ **Stale or incorrect claim** | Corrected in place, with what it was |
| 🟡 **Partial** | Some conditions met, named individually so the gap is visible |
| ⬜ **Open** | Not built. Either not started, or deliberately deferred with a reason |

Every `D<n>` / `RF-<n>` citation in the tables below is defined in
[`DEBT.md`](DEBT.md) — the debt register.

Week numbers below are the curriculum's, not feature boundaries — the review
called that out, so each heading now carries the **feature** it delivered.
Several weeks' work also shipped out of order (Week 7 and Week 8 pieces landed
during Week 4); those are marked where they occur.

## Status at a glance

> **Check PR and CI status live. This file does not carry it.**
>
> Pull-request numbers, open/merged states, CI run ids and per-suite test counts
> expire the moment anyone merges or pushes, and a stale one reads exactly like a
> current one. Run `gh pr list` and look at the latest CI run. Everything this
> file measured on a particular day lives in **one** place — *Audit snapshot*
> below — dated, fenced, and not to be trusted as current.
>
> The durable content is the rest: the Weeks 1–6 matrix and its acceptance
> criteria, the grouped gap list, and the next action. Those describe what must be
> true, which does not change when a branch does.

| Week | Feature | Status |
|---|---|---|
| 1 | Safe LLM engine (client, redactor, secrets cleanup) | ✅ Landed |
| 2 | RAG retrieval + corpus hygiene | ✅ Landed |
| 3 | AI scorer wrapper + append-only decision memory | ✅ Landed |
| 4 | Auto-disclosure on approval + KG traversal | ✅ Landed |
| 5 | Card tokenization + payment reconciliation | ✅ Landed |
| 6 | Servicing RBAC / ledger / maker-checker | 🟡 RBAC landed; ledger and maker-checker open |
| 7 | Trace ID + scoped reconciliation control | 🟡 Prometheus/Grafana landed early; the reconciliation control and the trace id are open |
| 8 | Model governance + fair-lending screen | 🟡 Model card, ZIP screen, prompt-injection guard landed; disparity monitoring open |
| 9 | BSA/AML — UBO + sanctions screening | ⬜ Open (spec not written) |
| 10 | Retention-aware redaction + handoff package | ⬜ Open |
| — | **Not on any brief:** test + CI infrastructure, browser E2E, migration parity, the bureau/decision-attempt seams, this register | ✅ Landed |

Counts quoted inside the weekly sections further down are deliberately
point-in-time — "41 tests at the time", "102 tests passing" — and are left as
written. They record what was true when a week closed, which is the only thing
that makes them useful.

## Owed to the client

The three answers the Week 1–4 review asked for. **All three are delivered** —
this section is now a record of where each one lives, not a list of obligations.
It said "three answers outstanding" while every row below already read ✅, which
is the kind of heading that outlives the thing it counts; the rows are kept
because the artefact each points at is the answer, and a reader owed one of them
should be able to find it without reading the PR history.

| Question | Status |
|---|---|
| What a graph database would buy the disclosure chain that foreign keys do not | ✅ **Answered** — [`adr/0009`](../adr/0009-graph-store-for-identity-traversal.md), with a **reproducible** benchmark: [`db/bench/graph_traversal_benchmark.py`](../db/bench/graph_traversal_benchmark.py) commits the generator, index DDL, exact query and timing method. The traversal `kg.py` cannot express is *every applicant reachable through any shared identity attribute, to unbounded depth* — address, phone, email, ssn, ein, employer. PostgreSQL **can** express it with a recursive CTE. The benchmark now compares three implementations of the same traversal (root-scoped frontier expansion, a prebuilt indexed edge table with construction timed separately, and the original global-edge build kept only as a pessimistic baseline), aborts if their reachability counts disagree, and asserts the posting table and edge table are the same relation as sets before timing anything. From one run (`db/bench/results.json` + `db/bench/run-output.txt`, both written by a single invocation and carrying the same `run_id` 2026-08-10T17:26Z-rows10000-depth5-root1, 10k applicants, PostgreSQL 16.14): depth 3 answers in **0.047s**, depth 4 in **0.155s**, depth 5 in **0.322s** (2,944 applicants reached); the genuinely *unbounded* walk — keyed on the applicant alone, so it terminates when the component is exhausted rather than needing a depth bound — returns the whole 10,000-applicant component in **0.723s**; and the traversal as the ADR defines it, unbounded reachability **with the connecting path for every applicant, each hop labelled with the attribute that justifies it**, takes **1.023s** (frontier/visited walk keeping one predecessor and one attribute each; 205 routes sampled and validated against the labelled edge relation, 0 invalid). An earlier figure timed `count(*)` only and returned bare applicant IDs, neither of which supported the claim. The pessimistic baseline is not a one-time adjacency cost either: `AS MATERIALIZED` stops the relation being rebuilt per hop, not rescanned, and the plan records 5 scans of 553,928 rows. The depth-bounded form cannot answer the unbounded question by removing its bound: it keys on (id, depth) and would never terminate, which is a separate review finding. Two earlier sets of figures were benchmark defects, not PostgreSQL's cost — first a global adjacency rebuild per query ("~3s to depth 3 / 72.3s at depth 4"), then simple-path enumeration through a cyclic graph reported as reachability ("16.9–38.7s at depth 4, no return at depth 5"). **There is no depth cliff**; the refusal stands because nothing in production needs the query at all, with a written trigger to revisit (Week 9 beneficial ownership, a weighted-path problem rather than a latency one) |
| An independent source for the TILA expected values | ✅ **Answered** — [`services/disclosure-service/tests/test_ffiec_external_oracle.py`](../services/disclosure-service/tests/test_ffiec_external_oracle.py) transcribes an APR this repository did not calculate, from the FFIEC APR Computational Tool (verified 2026-08-09, output PDF linked from PR #10's review thread), and is deliberately in a file of its own so it cannot be mistaken for one of the internal checks. `DEBT.md` D15 records the defect it caught: `compute_apr` was a simple add-on ratio, understating the disclosed APR by 4.39pp — 35× the Reg Z tolerance — and no self-consistent test could have found it. *This row read `⬜ Open` until the 2026-08-11 audit, describing `test_apr.py`'s `_decimal_apr()` re-implementation as the only reference available. That was true before PR #10 merged on 2026-08-10 and false after; `DEBT.md` D15 already said "Fixed", so this file and the debt register disagreed on `main` for a day* |
| The roadmap and debt register the ADRs keep citing | ✅ **Landed.** This file, plus [`DEBT.md`](DEBT.md) — the `D`/`RF` register, which had never existed in any form. All 16 citations in the tracked tree now resolve |

## Weeks 1–6 audit — traceability matrix

**Re-verified 2026-08-13 against `main` at `1528226`.** Every row was re-checked
against the code on `main` — not against a PR title, a comment, or an earlier row
of this file. `Done` requires all five: on `main`, acceptance criteria met,
tested, reachable where applicable, documented.

**No row cites a pull request as evidence.** Work in flight is not evidence of
anything, and a row that says "landing on #NN" becomes wrong twice — once when it
merges and once when the number is reused in someone's memory. Where the audit
found a gap closed, the row names the code and the test that close it; where it
found one open, it says what remains. Check `gh pr list` for what is in flight.

**Week numbering.** The week headings in this file are the curriculum's. Where a
feature landed in a different week than its brief, both labels appear in the
Week column as `brief → shipped`. No row is guessed: where the two disagree it is
because a merged PR says so.

**Counts (31 requirements): 24 Done · 5 Partial · 2 Not started · 0 Blocked ·
0 Deferred.** Four rows moved to Done and two out of Not started since the
2026-08-11 pass, all of them by work that has since merged to `main`.

| Week | Feature/requirement | Acceptance criteria | Status | Code/commit evidence | Test/CI evidence | Remaining gap | Priority | Next action |
|---|---|---|---|---|---|---|---|---|
| 1 | PII redactor before any log or LLM call | PAN/CVV/SSN never reach a log line or a prompt | **Done** | `loan-assistant/app/redactor.py`; ported copy in `payment-service/app/redactor.py` | `payment-service/tests/test_redactor.py`, `test_cardholder_name_not_logged.py` (6 tests, mutation-verified), `test_kyc_pii_not_logged.py`, `test_intake_pii_not_logged.py` — all green locally + CI | None. Scope limit stated in `DEBT.md` D5a: application-level call sites only; no reverse-proxy or container-runtime logging was testable here | — | — |
| 1 | Production LLM client (timeout, retry, cost guard, structured output) | Fail-closed on a missing/unreachable model | **Done** | `loan-assistant/app/llm_client.py` | `loan-assistant/tests/test_llm_client.py`; 153 tests green | None | — | — |
| 1 | Secrets: no hardcoded keys, `.env` untracked | No key fallback in any `config.py`; `.env` not in `git ls-files`; CI scans history | **Done** | `git ls-files .env` → empty; grep for fallback literals → 0 hits across 8 services | CI's `secrets` job runs gitleaks over the full history on every push and pull request | None. `DEBT.md` D18 records why history was not rewritten and that the committed values are published test PANs, not real data | — | — |
| 1 | Money as `Decimal`, not `float` | Compute in Decimal; store `NUMERIC` | **Partial** | `db/migrations/0005` (14 columns → `NUMERIC`); `apr.py`, `offer.py`, `schedule.py`, `balance.py`, `delinquency.py` | `test_money.py`, `test_schedule.py`, `test_amount_financed_rounding.py`; `db/tests/test_schema_parity.py` (CI) | **D1** — the disclosure *read* path still coerces to `float` (`disclosure-service/app/routers/offers.py:459-490, 532-534`). Write path is exact; redisplay is not | Medium | Close D1: keep the read path in Decimal to the serializer boundary |
| 1 → 5 | Stop storing PAN/CVV | Columns absent from a migrated *and* a fresh database | **Done** | `db/migrations/0031` (contract), `0029` (back-fill gate), `db/init/001_schema.sql` creates neither | `db/tests/test_0031_contract_gate.py`, `test_expand_contract_pan_cvv.py`, `db/tools/check_no_pan_readers.py`, `test_payments_sql_is_static.py`; CI `db-migrations` green | None for the defect (`DEBT.md` D5b/D13 closed). **`README.md` still says the opposite** — see the Medium gap below | Medium | Correct README (separate concern, separate PR) |
| 1 | README states the real compliance position | No PCI-DSS claim; named gaps are gaps that exist | **Done** | `README.md` states there is no `payments.pan` and no `payments.cvv`; the surviving mentions are in a clearly marked history block describing what the vendor delivery did | `db/tests/test_readme_schema_claims.py` — a README claim about the schema must match the schema, the way `test_docs_match_the_logging_code.py` holds the logging claims | None | — | — |
| 2 | RAG corpus hygiene gate | SSN/PAN/DOB-bearing records blocked offline, before the embedder | **Done** | `loan-assistant/app/corpus.py`; `adr/0005` | `tests/test_corpus.py`, `test_embeddings.py`; 153 green | None | — | — |
| 2 | Retrieval eval harness incl. the #6012 denial case | Graded on real retrieval output, no answer key as input | **Done** | `loan-assistant/app/rag_eval.py` | `tests/test_policy_chat.py`, `test_main.py`; CI `backend (loan-assistant)` green | None | — | — |
| 2 | Policy chat declines what it cannot ground | `answerable:false` on out-of-corpus questions | **Done** | `loan-assistant/app/policy_chat.py`, `prompt_injection.py` | `tests/test_policy_chat.py` | None | — | — |
| 2 | Policy and code agree on the underwriting rules | The assistant does not cite controls the code lacks | **Done** | `policies/underwriting_guidelines.md` no longer publishes cutoffs the system never applied: the DTI section states it is **defined, not currently applied**, and says why a DTI computed from income alone would not be a DTI | `db/tests/test_policy_matches_implemented_cutoffs.py` — the policy may not publish a threshold no code evaluates | None. The product decision was to amend the policy rather than implement the cutoff; implementing a real DTI needs a debt figure the intake does not collect | — | — |
| 3 | Specific adverse-action reason per denial | Reason derived from the real driving input, never a fixed string | **Done** | `decision-service/app/decision.py::_reason_codes`; `GENERIC_REASONS` absent | `tests/test_decision.py`, `test_decisions_router.py`; 40 green | Mapping picks between two categories only and has no fixture tests against known cases — tracked under Week 8, not Weeks 1–6 | — | — |
| 3 | Append-only decision memory | Inputs, score, version, features, reasons persisted; not editable after the fact | **Done** | `db/init/004_decision_events.sql` — `reject_decision_events_mutation()` trigger raises on UPDATE/DELETE; `db/migrations/0004` | `db/tests/test_decision_attempt_lifecycle.py`, `test_decision_single_writer_concurrency.py` (CI, real Postgres) | None | — | — |
| 3 | Decisioning chain non-blocking | No synchronous vendor hop on the request thread | **Done** | `decision.py` `async def` throughout, `httpx.AsyncClient` | `tests/test_bureau_idempotency.py`, `test_readiness.py` | Origination's call *into* decision-service is still sync — a different service's thread budget, out of this row's scope and stated as such | — | — |
| 3 | Explicit graph with per-step trace | Three nodes as a real `StateGraph` | **Done** | `decision-service/app/graph.py:28,106` | 40 tests green; LangSmith project `2463-fde` | None | — | — |
| 4 | One source of truth for the fee constant | One declaration; APR computed from it | **Done** | `disclosure-service/app/fees.py`; `apr.py`/`offer.py` import it | `tests/test_apr.py`, `test_offers.py` | None (`DEBT.md` D6 closed) | — | — |
| 4 | Offer links to the decision + fee version that produced it | FK `offers.decision_id`; `fee_pct_used` snapshotted | **Done** | `db/init/001_schema.sql:115-116`; `db/migrations/0008`, `0009`, `0011` | `db/tests/test_0011_offers_backfill.py`, `test_offer_creation_concurrency.py`, `test_seed_offer_consistency.py` | None | — | — |
| 4 | Disclosure auto-generates on approval | Offer exists the moment underwriting approves, with no manual step | **Done** | `origination-service/app/disclosure_graph.py` (two-node LangGraph), called from `run_decision()` | `frontend/e2e/approved-workflow.spec.ts`, `offer-disclosure-ui.spec.ts`, `regeneration-reprices-the-offer.spec.ts`; CI `e2e` green | None | — | — |
| 4 | Loan-history traversal | applicant → application → decision → offers in one call, staff-only | **Done** | `origination-service/app/kg.py`; `GET /applications/{id}/history` | `test_staff_gated_routes_require_internal_token.py`; `adr/0009` + `db/bench/graph_traversal_benchmark.py` answer the graph-store question with a measurement | None | — | — |
| 4 | Internal services not reachable around the gateway | No host port **and** `X-Internal-Token`, for every service with no auth of its own | **Done** | `docker-compose.yml` publishes no port for `kyc-service`; `kyc-service/app/routers/kyc.py:85-90` requires the token and refuses an unset one; `gateway/app/main.py:314-372` makes `/kyc/*` staff-only **and read-only** — a POST is refused with 405, so the gateway can no longer sign an anonymous caller's write | `gateway/tests/test_decision_service_not_host_published.py` (now including `kyc-service`), `kyc-service/tests/`, `gateway/tests/test_proxy_security.py`; CI green on `main` | None for reachability. What CIP actually checks is `DEBT.md` **D11**, a different and deliberately scoped gap belonging to Week 9 | — | — |
| 4 | Intake validation and field persistence | Phone/SSN format-checked; every submitted field persisted | **Done** | PR #7 (base `kalab-week4-disclosure-automation`, reached `main` via #6) | `origination-service/tests/test_validation.py`; 210 tests | None | — | — |
| 5 | Payment idempotency | A retried POST charges once; a same-key retry with different terms 409s | **Done** | `payment-service/app/payments.py::charge`; `db/migrations/0007`, `0010`, `0012`, `0013` | `tests/test_charge_flow.py`, `test_apply_payment_idempotency.py`, `test_reconcile_real_postgres.py` (CI, real Postgres) | None (`DEBT.md` D2 closed) | — | — |
| 5 | Card tokenization / PCI scope reduction | Service receives a token + last4 + brand; never a PAN, CVV or SSN; token never persisted | **Done** | `frontend/lib/tokenize.ts`; `PaymentIn` with `extra="forbid"`; `db/migrations/0016`; `adr/0008` supersedes `adr/0003` | `test_charge_flow.py`, `test_charge_no_pan.py`, `test_docs_match_the_logging_code.py` | None for the defect. ⚠️ The tokenization boundary is **mocked** — no real processor. PCI-DSS compliance is *not* claimed and needs a QSA | — | — |
| 5 | Captured-but-unapplied payments are recoverable | A durable, self-draining work item, not a hope that the client retries | **Done** | `payment-service/app/reconcile.py`; `db/migrations/0028` | `tests/test_reconciler_lifecycle.py`, `test_reconcile_real_postgres.py` | None | — | — |
| 5 | Spec package committed before it is cited | The cited path resolves | **Done** | `specs/0001-online-payments-idempotency-tokenization.md` | `db/tests/test_docs_citations_resolve.py` | None. History: the original spec was cited for weeks and had never been committed on any branch | — | — |
| 6 | Money-moving servicing actions are role-gated | Only csr/admin may adjust a balance or waive a fee, enforced server-side | **Partial** | `gateway/app/main.py` — `can_move_money()`, underwriter excluded. `servicing-service/app/main.py` now calls `_require_internal()` on **all four** money routes: `apply-payment`, `adjust-balance`, `waive-fee`, `late-fee` | `servicing-service/tests/test_money_routes_require_internal_token.py`, `test_internal_token_startup_validation.py`; `gateway/tests/test_auth_and_routes.py`, `test_proxy_security.py` | The service-side **token** boundary is closed. The **role** boundary is not: servicing still reads no role of its own, so the csr/admin restriction is enforced at one hop only. `DEBT.md` **D8** stays open for that and for the missing second approver | High | Take the role from the authenticated principal server-side, never from a client-supplied header. A written specification for this arrives with `specs/0002-maker-checker-self-approval.md`, which does not exist on `main` |
| 6 | Append-only ledger; balance as a projection | "Show me every change and who made it" is answerable | **Not started** | No ledger table on `main`: grep across `db/init`, `db/migrations` and `servicing-service/app` → 0 hits. `balance.adjust_balance()` docstring: "No ledger entry; the prior value is gone forever" | None — nothing on `main` to test | The whole feature. `balances.balance` is still one mutable column | High | The design decision is made and written; what remains is the schema and the write-path conversion. `DEBT.md` **D8** |
| 6 | Maker-checker on money-affecting actions | No single account can move money unilaterally | **Not started** | Grep for maker-checker/second-approver across `services/` and `db/` → only the three comments recording its absence | None | The whole feature | High | Blocked on the ledger above — an approval record needs somewhere to live. `DEBT.md` D8 |
| 6 | Lost-update proof on the shared column | One failing test pinning the real race | **Done** | `balance.apply_payment()` is still an unlocked read-modify-write — the proof is the deliverable, not the fix | `servicing-service/tests/test_balance_lost_update_real_postgres.py` — `xfail(strict=True)` cases against real PostgreSQL, plus a passing one pinning that the brief's own repro (payment + waiver) does **not** collide, because those touch different columns | None for the proof. The defect it pins is `DEBT.md` **D3**, still open | High | Close D3 — via the ledger, or `SELECT … FOR UPDATE` if that design is rejected |
| 6 | Legacy-comprehension ADR (RBAC + maker-checker + ledger) | An accepted ADR proposing the three | **Not started** | `adr/` on `main` holds 0001–0009; none covers servicing authz or the ledger | `db/tests/test_docs_citations_resolve.py` passes because nothing on `main` cites a missing ADR | Week 6's stated deliverable. The corrected concurrency analysis exists as prose in this file and as an executable proof in `test_balance_lost_update_real_postgres.py` | Medium | Land the ADR |
| 6 | Payment waterfall (fees → interest → principal) | A payment is applied in the regulated order | **Partial** | `balance.py:38` applies straight off principal | `test_apply_payment_idempotency.py` pins today's behaviour, not the correct one | `DEBT.md` **D14**, open | Medium | Sequence after the ledger — a waterfall needs per-component rows |
| 6 | `loans.apr` names what it holds | The column and the UI agree on which regulated rate is displayed | **Partial** | API renamed (`note_rate_pct` alias); three frontend views relabelled; seeds corrected | `db/tests/test_seed_offer_consistency.py::test_loans_apr_holds_the_note_rate_not_the_disclosed_apr` | `DEBT.md` **D19** — the column is still literally `loans.apr`; anyone reading SQL, a dump or `db/init/001_schema.sql` meets the wrong name | Medium | Rename via migration + every query/model/fixture that names it |

### Remaining gaps, grouped

Every acceptance criterion below is written so that meeting it is checkable by
someone who did not write it.

**Closed since the 2026-08-11 pass**, with what closed each one, because a gap
list that only ever grows stops being read:

| Gap | Closed by | Verify it stayed closed |
|---|---|---|
| **G-KYC** — the CIP handler was reachable unauthenticated, on two routes | `kyc-service` has no host port and `POST /kyc/check` requires `X-Internal-Token` and refuses an unset one; the gateway's `/kyc/*` relay is staff-only **and read-only**, so a POST is refused with 405 rather than signed on an anonymous caller's behalf | `gateway/tests/test_decision_service_not_host_published.py` (now parametrized over `kyc-service` too), `gateway/tests/test_proxy_security.py`, `kyc-service/tests/` |
| **G-SERVICING-TOKEN** — servicing's money routes checked no token | `_require_internal()` on **all four**: `apply-payment`, `adjust-balance`, `waive-fee`, `late-fee` | `servicing-service/tests/test_money_routes_require_internal_token.py`, `test_internal_token_startup_validation.py` |
| **G-D3-PROOF** — Week 6's failing lost-update test was never written | `servicing-service/tests/test_balance_lost_update_real_postgres.py` | The `xfail(strict=True)` cases fail the suite the day someone "fixes" the race without removing them |
| **G-README** — the README claimed dropped columns still existed | README states the schema as it is | `db/tests/test_readme_schema_claims.py` |
| **G-DTI** — the policy published cutoffs the code never applied | The DTI section is marked defined-but-not-applied | `db/tests/test_policy_matches_implemented_cutoffs.py` |

**`apply-payment` belongs in that servicing-token list and was missing from it.**
The earlier acceptance criteria named `adjust-balance`, `waive-fee` and
`late-fee` only. `POST /accounts/{id}/apply-payment` reduces a loan balance
directly and is intended for payment-service alone — leaving it out would have
declared the service-side money boundary closed with its highest-traffic money
route still open. All four are covered now, and the test parametrizes over all
four so a fifth route added without a check fails it.

#### High

- **G-INTAKE-401** — a KYC authorization failure is indistinguishable from a timeout at intake. `submit_application` wraps the kyc-service call in `except Exception`, which is right for a timeout and wrong for a 401: an unset or rotated `INTERNAL_SERVICE_TOKEN` would silently stop verifying identity on **every** application while intake still returns 200 with all four CIP flags false and no `kyc_checks` row. *AC:* a 401/403 from kyc-service fails the request or raises an alarm, distinctly from a timeout; a test asserts the two are handled differently. *Evidence:* a test in `origination-service/tests/` that a 401 does not produce a 200 with all-false CIP. *Dependency:* none.
- **G-SERVICING-ROLE** — servicing reads no role of its own, so the csr/admin restriction on money movement is enforced at the gateway hop only (`DEBT.md` D8). *AC:* the role comes from the authenticated server-side principal, never from a client-supplied header, and a caller holding the internal token cannot elevate itself by setting one. *Evidence:* a test asserting a spoofed `X-User-Role` does not grant authority. *Dependency:* none.
- **G-LEDGER** — no append-only ledger; balance is one mutable column (`DEBT.md` D8). *AC:* every balance change writes an immutable row carrying actor, reason and a signed delta; the displayed balance is derived from those rows; a trigger rejects UPDATE/DELETE, as `decision_events` does. *Evidence:* a migration, a `db/tests` case proving the trigger, and a per-loan parity test that the projection equals the ledger sum. *Dependency:* none — the design decision is made.
- **G-MAKER-CHECKER** — no second approver on any money-affecting action (`DEBT.md` D8). *AC:* an adjustment or waiver is recorded as a proposal until a second, different staff account approves it; the same account cannot do both, with no exception for an administrator; a rejected proposal is retained. *Evidence:* a test proving self-approval is rejected and one proving a rejected proposal survives. *Dependency:* **G-LEDGER** — the approval writes a ledger entry, so it needs somewhere to write.
- **G-D3** — `apply_payment` is an unlocked read-modify-write; two concurrent calls lose one (`DEBT.md` D3). The failing test now exists; the fix does not. *AC:* the `xfail(strict=True)` cases in `test_balance_lost_update_real_postgres.py` pass, and the marker is removed in the same commit. *Dependency:* none — either the ledger or `SELECT … FOR UPDATE` closes it.
- **G-D7** — reconciliation is not a control (`DEBT.md` D7). *AC:* it runs on a schedule, compares at transaction level rather than netting totals, records every run, and fails with a distinct code when it could not compare anything. *Evidence:* a run table, a non-zero exit on a break, and a test that a run over nothing is an error rather than a pass. *Dependency:* none.

#### Medium

- **G-D1** — the disclosure read path rebuilds the display schedule in `float` (`DEBT.md` D1, *Partly fixed*). *AC:* Decimal to the serializer boundary; a redisplay test asserting the schedule matches the disclosed payment exactly.
- **G-ADR-0010** — Week 6's legacy-comprehension ADR is not on `main`. *AC:* an accepted ADR covering servicing RBAC, maker-checker and the ledger, including the corrected concurrency repro.
- **G-D19** — `loans.apr` holds the note rate (`DEBT.md` D19). API and UI are corrected; the column is not. *AC:* rename via migration, plus every query, model and fixture that names it.
- **G-D14** — no payment waterfall (`DEBT.md` D14). *Dependency:* G-LEDGER — a waterfall needs per-component rows.

#### Debt

Register entries in scope for Weeks 1–6 and still open, cited by their own IDs —
no new numbers minted here: **D1** (float read path, partly fixed), **D3**
(unlocked read-modify-write), **D7** (reconciliation is not a control), **D8**
(no role check / no second approver / no ledger entry), **D14** (no waterfall),
**D19** (column name, partly fixed), **D20** (bounded, not fixed — the static-SQL
premise is enforced by test instead).

**D11** (KYC is CIP-only) is open by deliberate scope limit and belongs to Week 9.
G-KYC was a *different* defect — about reachability, not about what CIP checks —
and closing it did not touch D11.

## Start next

**G-INTAKE-401 — a KYC authorization failure must not read as a hiccup.**

It is first because it is a live control failure with no dependency on anything
else in this file. `submit_application` wraps the kyc-service call in
`except Exception`, which is right for a timeout — a network blip must not 500 an
application — and wrong for a 401. An unset or rotated `INTERNAL_SERVICE_TOKEN`
would stop verifying identity on **every** application while intake kept
returning 200 with all four CIP flags false and no `kyc_checks` row written.
Identity verification would be off, and nothing would say so.

It needs no decision from anyone: the two cases are already distinguishable at the
call site, and the fix is to treat them differently and assert that in a test.

After it, in order:

1. **G-D3** — the lost-update race. The failing test is on `main`; the fix is not.
   Closing it removes the `xfail(strict=True)` markers in the same commit, which
   is what stops the proof quietly becoming decoration.
2. **G-D7** — reconciliation as a control that can fail.
3. **G-LEDGER**, then **G-MAKER-CHECKER**, which depends on it — an approval
   writes a ledger entry, so it needs somewhere to write.

### Historical note — G-KYC, and why it took two passes

Kept because the sequence is the lesson, not because the status is current.
**G-KYC is closed**: `kyc-service` publishes no host port, `POST /kyc/check`
requires the internal token and refuses an unset one, and the gateway's `/kyc/*`
relay is staff-only and read-only.

The first attempt closed the `kyc-service` half only. An adversarial review and a
live check against a running stack then found the handler still reachable — the
**gateway's** `/kyc/{path}` route required no session and stamped the trusted
`X-Internal-Token` itself, so an anonymous caller reached the same handler through
port 8000, the one port deliberately published to the host:

```
curl -X POST localhost:8000/kyc/kyc/check   -d '{"application_id":1,"applicant_id":1,"name":"Forged Owner", ... }'
→ 200 {"check_id":92,"cip_passed":true, ... }
```

That wrote a `kyc_checks` row for a real applicant with `name_verified=t`, with no
session and no token. The row was deleted after verification. Root cause was
**gateway authorization**, not `kyc-service`.

**CI was 22/22 green on that branch and caught none of it**, because no test
exercised the gateway's `/kyc/*` route at all. Two related defects came with it:
`_proxy` kept a client-supplied `x-internal-token` and the client's copy won, so
any caller could force a 401 on every internal-token route; and the resulting 401
landed in intake's `except Exception` swallow — which is **G-INTAKE-401** above,
the one part of this that is still open.

This section previously described the work as "one concern". It was two — a
`kyc-service` change and a gateway authorization change — and calling it one is
precisely what let half of it ship as though it were whole. Both halves landed
together in the end, because splitting a fix from the defect it closes would have
left the change claiming something untrue.

## Audit snapshot — 2026-08-13

**Everything in this section is a measurement, not guidance.** It expires the
moment anyone merges or pushes, and it is fenced here so the rest of the file can
be read without wondering which parts have gone stale. **Do not cite it as
current status** — run the commands.

- **Base:** `main` at `1528226`, level with `origin/main` when the audit ran.
- **How to reproduce:** `python -m pytest -q` per service (with `DATABASE_URL`
  set, or the real-PostgreSQL cases skip and a skip reads like a pass),
  `python -m pytest db/tests -q`, and `npm run test:e2e` in `frontend/` with the
  rate-limit overlay `docker-compose.e2e.yml` — without it, later browser specs
  trip the shipped 120-request control and fail on unrelated assertions.
- **Suites at that commit:** eight backend services, the `db/tests` migration and
  documentation suite, and the Playwright specs under `frontend/e2e/`. Per-suite
  counts are deliberately **not** transcribed here: they were wrong within a day
  every previous time this file carried them, and the command above is both
  shorter and correct.
- **Pull requests and CI:** not recorded. `gh pr list` and the Actions tab are the
  only accurate sources, and a number written down here is wrong as soon as
  someone merges. The merged history is in `git log` and on GitHub.

Per-week test counts appear inside the weekly sections below as they were when
that week shipped. They are historical, labelled as such, and left alone.

---

## How the app works (fast reference)

8 backend services + Postgres + Redis + Next.js frontend, all behind one gateway (port 8000). Frontend on port 3000.

The gateway is the only host-reachable API. *This is worth stating carefully,
because it was false for most of this engagement and the file claimed it anyway:
`docker-compose.yml` also published `kyc-service` on host port 8003, and that
service checked no `X-Internal-Token`, so `POST localhost:8003/kyc/check` reached
the CIP handler with no authentication at all. `ARCHITECTURE.md` had recorded the
gap since PR #6 while this file asserted the opposite.* The port is gone, the
handler requires the token, and the gateway's own `/kyc/*` relay is staff-only and
read-only — see the Weeks 1–6 audit above.

**Services**
| Service | Job |
|---|---|
| **gateway** | Session auth (login → Redis token), role-based proxy to everything else. Strips inbound `X-User-*` headers, sets its own trusted ones from the verified session. |
| **origination-service (LOS)** | Application intake, KYC trigger, decision trigger, staff manual-review resolution, offer accept/fund. System of record for applications/applicants/offers/loans-at-birth. |
| **decision-service** | Credit pull (via the `BureauClient` seam) + AI scoring model (LangGraph, 3 nodes). **Compute-only — persists nothing** since PR #6; it returns a proposed outcome and origination writes `decisions` + `decision_events` together. |
| **disclosure-service** | Auto-generates the loan offer/disclosure (APR, fee schedule) the instant an application is approved (automated or manually-reviewed). |
| **kyc-service** | Identity verification stub. |
| **servicing-service (LSS)** | Post-funding: balance, payments, adjustments, fee waivers, reconciliation. |
| **payment-service** | Card/ACH charge capture, idempotency, hands the balance-apply off to servicing. |
| **loan-assistant** | Claude-backed. Policy Q&A (anyone) + AI application summary (staff only — pulls financials from origination-service). |

**Loan lifecycle**
1. **Apply** — `POST /los/applications` → KYC auto-runs → one-time `access_token` minted (proves ownership for step 2 with no account needed). `applications.status` starts `submitted`.
2. **Decision** — `POST /los/applications/{id}/decision` with that token → decision-service scores it (`model_score = bureau_score*0.9 + income/1000`) against the three policy bands:
   - **≥ 660 → approve** — `status` → `approved`, disclosure auto-generates an offer, an `accept_token` is minted.
   - **600–659 → refer** — `status` → `in_review`. No offer, no accept_token — accept is blocked (422) until staff resolves it.
   - **< 600 → deny** — `status` → `denied`, adverse-action reason returned, accept blocked.
   A rerun of an already-decided application is staff-only, and now also blocked (422) once the application is funded or has a manual review on record — scoring is deterministic, so a rerun used to silently reset a resolved decision back to the automated result.
3. **Manual review** (refer only) — `POST /los/applications/{id}/review` (staff-only), body `{outcome: approve|deny, reason}` → resolves a `refer` into a real approve/deny, exactly like the automated path (offer + accept_token minted on approve). Audited in `manual_reviews` (who/what/why), kept separate from decision-service's own model-only audit trail (`decision_events`).
4. **Accept** — `POST /los/applications/{id}/accept` with `accept_token` → atomically funds the application (`status` → `funded`) + boards a loan into servicing (DB-transaction guarded, no double-fund on a race). **Approve ≠ funded** — this is a separate, explicit step; a decision alone never moves money.
5. **Charge** — `POST /payments` → idempotency-key-guarded insert → charge captured → balance applied via servicing (itself idempotent by payment_id).
6. **Service** — balance/past-due read via `/lss/accounts/{loan}/balance`; csr/admin can adjust-balance/waive-fee.

**Roles:** `borrower`, `csr` (Servicing Rep), `underwriter`, `admin`. Staff-only backend routes require a session role **and** a second shared secret (`X-Internal-Token`) — closes the case where a role header alone could be spoofed if a port were ever reopened. Frontend nav/route guards are UI convenience only; the backend is the real gate.

**Demo logins:** `admin`, `underwriter`, `csr`, `maria` (borrower, `applicant_id` 1). Credentials
are in `db/init/002_seed.sql` and on the login page — not repeated here.

---

## Quick test — the 3 agents (fast sanity check)

Stack must be up (`docker compose up -d`). All 3 confirmed live-working this session.

| Agent | Week built | Where | Steps | Expect |
|---|---|---|---|---|
| **Decision Graph** (LangGraph, 3 nodes: pull credit → score → finalize; `finalize` returns the proposed outcome, it does not persist) | Week 3 | `/apply` (public, no login) or staff `/underwriting/[appId]` → "Run decision" | Submit an application, income + amount matter (score ≈ `bureau_score*0.9 + income/1000`) | Weak profile → `refer`/`deny` with a real reason code. Strong profile (income ≥100k, modest amount) → `approve` |
| **Disclosure Graph** (2-agent hand-off: read record → build offer) | Week 4 | Same app, `/underwriting/[appId]` "Offer" card | Nothing to click — fires automatically the instant Decision Graph returns `approve` | Real APR/finance-charge/monthly-payment numbers appear with no manual step, `decision_id` links back to the exact decision |
| **Assistant Agent** (retrieval + Bedrock LLM) | Week 2 (retrieval) / Week 3 (agent wrap) | Log in `csr`/`underwriter`/`admin` → `/policy-chat` | Ask a policy question | See catch-fast questions below |

**Policy questions to catch fast (assistant agent):**
- *Should answer* — "What is the maximum loan amount for a personal installment loan?" → must say **$50,000** and cite `underwriting_guidelines.md`. Wrong number or no citation = retrieval or ingestion broke.
- *Should answer* — "What model score is required to approve?" → must say **≥660**, citing `underwriting_guidelines.md`. Confirms it is reading the real score band the Decision Graph enforces, not stale/generic text.
  **Careful with the answer it gives about DTI.** The policy document also names a DTI ≤43% cutoff and fraud-flag rules, and the assistant will quote them — but **the code implements neither** (`monthly_debt` is hardcoded to `0` by origination before decision-service ever sees it, and no fraud check exists anywhere). The assistant is correctly quoting policy; the policy describes a system that was never built. See `adr/0007-underwriting-policy-dti-fraud-gap.md`.
- *Should decline, not guess* — anything not in `policies/` (e.g. "what's the CEO's favorite color?") → must return `answerable:false` with the honest-decline message. If it answers this instead of declining, it's hallucinating — treat as a broken guardrail, not a feature.

Login page (`/login`) lists all seeded demo creds. Full curl-only script (no browser): `test_agents.sh` (see this session's scratch dir) — hits all 3 in one run, no manual placeholder-swapping.

---

## Week 1 — LLM Engineering for Production
### Feature: safe LLM engine — client, PII redactor, secrets cleanup, Decimal money

**Domains touched:** Payments · Origination · Decisioning · Finance

**Client ask (Dana):** Polish the application form. Board wants "AI" — build a
small assistant that summarizes an application for a loan officer. "Payments
work fine today, don't worry about that part."

**What client handed over:** the repo, "keys included." Bureau/processor keys
hardcoded in `config.py` + committed `.env`. A payment log line with raw
PAN/CVV/SSN. Float-based money math. A README claiming PCI-DSS compliance.

| # | Domain | What needed fixing | Fixed? | Why it mattered |
|---|---|---|---|---|
| 1 | Payments | `payment-service.log` writes full PAN, CVV, SSN in plaintext on every charge | ✅ **Closed, all four halves — the last one by PR #15.** ✅ `payment-service.charge()` redacts via a ported copy of `services/loan-assistant/app/redactor.py` before logging. ✅ `servicing-service/app/payments.py` logs `loan_id`/`amount`/`method` only, and receives a processor token rather than a PAN (ADR 0008). ✅ Origination's intake logs `app_id`/`applicant_id`; the "request middleware logging full POST bodies" named here **never existed** — that claim came from a copy-pasted docstring, which is the D5c defect reproducing itself inside this roadmap. ✅ Storage: the `pan`/`cvv` columns are **gone** — `db/migrations/0031` dropped them and `db/init/001_schema.sql` no longer creates them, so neither a migrated nor a fresh database has them (PR #15). The writers went first: the application in PR #8, the seeds in PR #11, with `0029` back-filling `last4` so payment history still displays and `0031` refusing to run until that was complete and an operator acknowledged the drop. Closes D5b/D13. *Two earlier versions of this line were wrong in opposite directions — it claimed the seeds "still write real values into them, so every fresh database contains card data" after PR #11 had stopped them, and before that it had understated the seeds entirely.* **And the log file itself stayed committed to the repo until 2026-08-05** — the code was fixed weeks before the artifact it produced was removed, which is the closure gap the client review led with. See `DEBT.md` | CVV storage/logging is an absolute PCI-DSS violation, no exceptions — a leaked log is a breach, not a bug |
| 2 | Origination / Decisioning | Bureau + core-banking + processor keys hardcoded in `config.py`, also committed in root `.env` | ✅ Effectively closed — `.env` untracked, hardcoded fallbacks removed from all 7 services. **Confirmed with the project owner: these were training placeholders (`EXAMPLE-LEAKED-KEY-rotate-me`), never real provider accounts** — so there's no live credential to rotate, and the old values still in git history aren't a real security exposure, just cosmetic (a reviewer seeing placeholder-labeled strings in `git log`). A history rewrite remains available on request but isn't fixing an actual vulnerability here | For a *real* deployment this would be a genuine breach risk (a leaked bureau key pulling real credit data under Meridian's name) — confirmed not the case for this training instance specifically |
| 3 | Finance | Money stored/computed as `float` everywhere (`0.1 + 0.2` problem) | ✅ Fixed, both layers. **The separate defect this row used to point at -- exact arithmetic is not the same as the right formula -- is fixed too: `compute_apr` has used the actuarial present-value solve since PR #10 merged, checked against an independent FFIEC vector. The row previously warned that `main` still shipped the wrong formula, which was true until 2026-08-10.** Details:<br>• **Computation** — `disclosure-service` + `servicing-service` compute in `Decimal` throughout (`apr.py`, `offer.py`, `schedule.py`, `balance.py`, `delinquency.py`); `payment-service.charge()` quantizes to exact cents before storing/forwarding<br>• **Storage** — all 14 money columns migrated `DOUBLE PRECISION` → `NUMERIC` (`db/migrations/0005_money_columns_to_numeric.sql`), applied live against a populated 307-row DB, no data loss. `asdecimal=False` on the ORM models keeps it storage-only, no Decimal ripple<br>• **Regression caught + fixed** — post-migration live test broke `run_decision()`: raw-psycopg2 reads of a `NUMERIC` column return `Decimal` (unaffected by `asdecimal`), and forwarding that via `httpx.post(json=...)` crashed (`Decimal is not JSON serializable`). Fixed with `float(...)` at the forward boundary; audited every other cross-service call site, none else affected<br>• All 182 backend tests pass *(at the time — see Verification baseline for current)* | Rounding error compounds across balance updates and APR calculations — this exact fault line also caused a real Reg Z disclosure violation. Schema fix alone surfaced a live bug only end-to-end testing against a real populated DB would catch |
| 4 | Payments | README claims "PCI-DSS compliant," schema has plaintext `pan`/`cvv` columns | ✅ Fixed:<br>• Removed the false "PCI-DSS compliant" claim<br>• README now states plainly it's **not** compliant and names the specific gaps (raw PAN/CVV storage, plaintext logging half still open) | a false claim is worse than an honest gap |

**Built this week (the actual deliverable):**
- `services/loan-assistant/app/llm_client.py` — production LLM client: timeout, retry, cost guard, structured output.
- `services/loan-assistant/app/redactor.py` — strips PAN/CVV/SSN before anything is logged or sent to the LLM.
- LOS↔LSS seam map — `ARCHITECTURE.md`.
- This debt log, naming rows 1/2/3 above in business terms (the brief's own ask).

No AI feature/route shipped yet — deliberately. Week 1 built the safe engine
underneath it, not the feature itself.

---

## Week 2 — RAG & Knowledge Retrieval
### Feature: policy retrieval + corpus hygiene gate

**Domains touched:** Decisioning · Payments (PII) · Origination

**Client ask (Dana):** Loan officers keep asking compliance the same
underwriting-policy questions. Build a helper that answers from `policies/` +
past decisions. Handed over `kb_dump/` "for context." One officer asked "why
was app #6012 denied?" and got nothing back. Keep cost low (Pro-plan budget).

**What client handed over:** `policies/` (clean, embeddable). `kb_dump/
applications.jsonl` — raw `ssn`, `pan`, `dob` on every record, unredacted. A
`decisions` table that's `(app_id, outcome)` only.

| # | Domain | What needed fixing | Fixed? | Why it mattered |
|---|---|---|---|---|
| 1 | Payments (PII) | `kb_dump/applications.jsonl` has raw SSN/PAN/DOB — embedding it as-is puts card numbers and SSNs straight into the vector store | ✅ Fixed:<br>• Added a hygiene gate in `corpus.py` — regex-based detector for SSN/PAN/DOB patterns<br>• Runs offline, no LLM call<br>• Blocks flagged records before anything reaches the embedder | `corpus.py`'s hygiene gate blocks it offline before anything reaches an embedding |
| 2 | Decisioning | "Why was #6012 denied?" returns 0 results — not a retrieval bug, no reason was ever recorded anywhere for any denial | ✅ Fixed:<br>• This week: documented as a required fix in an ADR (no retrieval fix possible — no data existed to retrieve)<br>• Actually closed **Week 3**: `decision_events` table now records the real reason behind every denial<br>• "#6012 denied" now has an actual answer to retrieve | Retrieval can't answer a question the system never stored the answer to — a search fix would have been the wrong fix entirely |
| 3 | Decisioning | If a regulator asked for the reason behind one specific denial, it couldn't be produced | ✅ Fixed:<br>• Same root cause as #2 — no denial reason was ever recorded anywhere<br>• Closed Week 3 via `decision_events` — a specific reason is now persisted per decision, producible on request | ECOA/Reg B requires a specific adverse-action reason on record, not just an approve/deny outcome |
| 4 | Origination | No definition of what belongs in a retrieval corpus vs. what must be redacted/excluded | ✅ Fixed:<br>• Wrote `adr/0005-rag-corpus-hygiene.md` — defines what belongs in the retrieval corpus vs. what must be redacted/excluded<br>• Enforced by `corpus.py`'s hygiene gate, not left as a per-ingest judgment call | `adr/0005-rag-corpus-hygiene.md` makes it a documented rule, not a judgment call per ingest |

**Built this week (the actual deliverable):**
- `services/loan-assistant/app/rag_eval.py` — retrieval eval harness, fixed query set including the literal #6012 case.
- `services/loan-assistant/app/corpus.py` — corpus loader + PII hygiene gate (offline, regex-based).
- `services/loan-assistant/app/embeddings.py` — local TF-IDF retriever, embeddings cached (never re-embedded per run — the quota constraint from the brief).
- `adr/0005-rag-corpus-hygiene.md` — corpus hygiene decision.
- Two review rounds, both fixed before merge: the eval grader first checked against ground truth instead of what retrieval actually returned, then was found to still accept the expected answer as an input (proving it could find a fact it was handed, not that it works blind) — fixed to grade real retrieval output with no answer key. A third, post-merge fix closed a denial-paraphrase case that cleared generic policy-vocabulary coverage without ever mentioning #6012.
- 41/41 tests pass. Merged to `main` (PR #4).

**Found live through the new policy-chat feature, no brief prompted it —
`adr/0007`:** a staff member asked what the decisioning policy is and got back
`underwriting_guidelines.md#5.0` verbatim, which names a **DTI ≤ 43% cutoff and
fraud-flag rules that the code has never implemented**. `monthly_debt` is
hardcoded to `0` by origination before decision-service ever sees it, and no
fraud check exists anywhere in the codebase — re-verified today.

The retrieval is working correctly; that is what makes this worth recording.
The assistant faithfully quotes an approved policy document describing controls
that do not exist, which is a worse failure than a retrieval bug: a staff member
now has a citation for a rule nothing enforces. ⬜ **Open** — the gap is
documented in the ADR, and neither the policy nor the code has been changed to
agree with the other. Whoever closes it has to pick which one is wrong.

*Shipped separately, same week, ahead of this brief:* the AI-summary feature
from Week 1's engine went live (`/applications/{id}/summary` route, gateway
proxy, frontend card), plus the leaked-key cleanup from Week 1's Problem 2
(`.env` untracked, hardcoded fallbacks dropped) and a CI secrets-scanning gate
so a rotated key can't leak again unnoticed.

---

## Week 3 — Single-Agent Design + Memory
### Feature: AI scorer wrapper + append-only decision memory

**Domains touched:** Decisioning · Finance (performance)

**Client ask (Dana):** Licensed a new, "more accurate" AI credit scorer. Wrap
it in an assistant that decisions an application and reports the result.
"Slows down at high volume, but accuracy is what matters." Move fast.

**What client handed over:** the credit pull → bureau call → model run as one
synchronous chain on the request thread ("timeouts at >20 concurrent apps").
Sample denial output with `adverse_action_reason: 'purchasing history'` —
the same string recurring across many different denials. No decision record
persisted at all (no inputs, no model features, no timestamp).

| # | Domain | What needed fixing | Fixed? | Why it mattered |
|---|---|---|---|---|
| 1 | Decisioning | Every denial got the identical hardcoded reason ("purchasing history"), never actually connected to the model's real inputs (bureau score vs. income) | ✅ Fixed:<br>• Added `_reason_codes()` — maps the actual driving input (bureau score vs. income shortfall) to a specific reason<br>• Replaced the single hardcoded "purchasing history" string used for every denial | **12 CFR 1002.9** (Reg B): an adverse-action notice must state the *specific, accurate* reason — no AI exemption. (Cited here rather than CFPB Circular 2023-03, which was **withdrawn 12 May 2025**; enforcement anchor: Massachusetts AG v. Earnest Operations, $2.5M, 10 July 2025.) A canned string isn't legally defensible, accurate model or not |
| 2 | Decisioning | No record of a decision's inputs, model version, or timestamp — nothing to prove *why* Meridian denied someone if disputed later | ✅ Fixed:<br>• Added append-only `decision_events` table — persists inputs, model score/version, top features, reason codes per decision<br>• Enforced with a DB trigger so a record can't be edited or deleted after the fact, not just access-controlled | If a denied applicant disputes the reason, "we don't have that data anymore" is not a defensible answer to a regulator or a fair-lending challenge |
| 3 | Decisioning / Finance | Credit pull + bureau call + model run all synchronous on one request thread; already timing out above ~20 concurrent applications | ✅ Fixed — `_pull_credit`/`_call_ai_scorer`/`_run_model`/`decide` are all `async def` now, using `httpx.AsyncClient` instead of blocking `httpx.get`/`.post`; the `POST /decisions` route is `async def` too. A request waiting on Experian or the AI scorer no longer holds a thread-pool worker for the call's full duration — the event loop's own async I/O handles far more concurrent in-flight vendor calls than FastAPI's default sync-route thread pool ever could. Verified live via `TestClient` (real 200 through the actual async route) plus 31 passing tests (`pytest-asyncio` added). **Scope note:** this fixes decision-service's own internal chain only — `origination-service`'s call *into* decision-service is still synchronous, a separate service's own thread-pool budget, out of scope here. The DB write in `decide()` also stays synchronous (fast local Postgres insert, not the external-vendor bottleneck this targeted) | The new AI-scorer call had added a *second* synchronous network hop on the same thread, making the existing bottleneck worse, not better — both hops are non-blocking now |
| 4 | Decisioning | The pull-credit/score/persist chain was a plain async function body — no explicit graph, no per-step trace | ✅ Fixed:<br>• `app/graph.py` — the exact same three steps (`_pull_credit` → `_run_model` → persist) as a real LangGraph `StateGraph`, not inline code<br>• `decide()`'s signature, output shape, and every fail-closed exception are unchanged — the graph's nodes call the identical functions decide() always called, so all 31 existing tests pass untouched<br>• LangSmith tracing comes for free: `langgraph` pulls in `langchain-core`, which auto-instruments when `LANGSMITH_TRACING`/`LANGSMITH_API_KEY` are set (already true via the shared `.env`, project `2463-fde`) — no separate wiring needed<br>• Live-verified: a real decision (app 7307, approve/672) ran through the graph end to end | Each step (bureau pull, scoring, persistence) is now individually traceable and has an explicit boundary to extend later — a retry policy or a conditional branch on one step doesn't mean editing a function body |

**Direct answer to the client's own priority question** ("accuracy vs. correct
and provable reason — which matters more"): the provable reason matters more
for compliance specifically — an accurate score with a fabricated reason still
fails Reg B; this is a compliance floor, not a tradeoff to weigh against model
quality.

**Built this week (the actual deliverable):**
- `services/decision-service/app/decision.py` — `_call_ai_scorer()` (fail-closed tool call to the licensed model, same contract as the existing bureau call), `_reason_codes()` (maps the actual driving input — bureau score vs. income shortfall — to a specific reason, not a fixed string).
- `db/init/004_decision_events.sql` — append-only `decision_events` table (inputs, model score/version, top features, reason codes), enforced with a database trigger so it can't be edited or deleted after the fact, not just access-controlled.
- `adr/0006-adverse-action-reason-mapping.md` — the reason-mapping decision, plus the sync→async note answering the client's own performance concern (documented direction, not built this week).

**Review rounds, all fixed, all on the same PR:**
- Round 1 — non-transactional audit write (decision could commit without its audit row), reason-code authority unclear, a missing migration file.
- Round 2 — a shared-DB-connection concurrency bug that could let one request's rollback erase another request's already-committed decision; an `LLM_PROVIDER` config typo silently picking the wrong vendor; policy chat trusting an unvalidated model response.
- Round 3 (self-initiated adversarial pass) — a real scorer response with an *empty* reason list was silently falling back to a locally-guessed reason (fabricating exactly the kind of ungrounded reason this week exists to eliminate) — fixed to fail closed instead; the audit record's `top_features` field was also being fabricated from a local formula for real vendor responses that never actually reported feature attributions — now recorded as `null` for a real response rather than a guessed number.
- Total at the time: 102 tests passing (31 decision-service + 71 loan-assistant). PR #5 — **merged to `main`** (2026-07-30), after a final review pass found and fixed three more money/credit-path bugs on the same branch: anonymous-caller credit pull on the FIRST decision call (not just reruns), negative/NaN/Infinity payment amounts, and un-deduped payment retries.

---

## Week 4 — Multi-Agent + Knowledge Graphs
### Feature: auto-disclosure on approval + loan-history traversal

**Domains touched:** Disclosures · Decisioning · Finance

**Client ask (Dana):** Automate offer + TILA disclosure generation right after
approval — it's still fully manual today. "The numbers look basically right to
me. Fee/APR rules are scattered around the code a bit, but it works."

**What client handed over:** `apr.py`'s own docstring worked example (principal
18000, 7.99%, 48mo): float APR 7.142% vs. correct Decimal APR 7.157%. The
origination-fee % copy-pasted into three files, drifted: `apr.py` 0.025,
`fees.py` 0.030, `offer.py` 0.03.

| # | Domain | What needed fixing | Fixed? | Why it mattered |
|---|---|---|---|---|
| 1 | Disclosures | The fee constant is copy-pasted into 3 files and has drifted — `apr.py` (0.025) doesn't match `fees.py`/`offer.py` (0.030) or the published `policies/fee_schedule.md` | ✅ Fixed — `apr.py` and `offer.py` now import `ORIGINATION_FEE_PCT` from `fees.py` (one source of truth) instead of redeclaring it; `compute_apr(18000, 7.99, 48)` now correctly returns 5.196% (was 5.041% with the wrong 0.025 fee), confirmed against `test_apr.py`'s own Decimal reference | `apr.py`'s wrong value is the one that computes the number that ships on the real TILA disclosure — a real Reg Z tolerance breach, not a rounding nit |
| 2 | Disclosures | No link from a disclosure back to the exact decision + fee-constant version that produced it (`offers` has no `decision_id`, no snapshot of which rule version ran) | ✅ Fixed:<br>• `offers.decision_id` — real FK to `decisions(app_id)` (`db/migrations/0008_offer_decision_link.sql` — renumbered after merging with Week 3's own `0006`/`0007`), not a loose reference; an offer can't be linked to a decision that doesn't exist<br>• `offers.fee_pct_used` — snapshots `ORIGINATION_FEE_PCT` at offer-creation time, so a later change to the constant can never retroactively change what an existing offer is proven to have used<br>• Both origination-service's manual `POST /los/offer` and the new auto-generated path (see #3) populate them | If `ORIGINATION_FEE_PCT` changes next quarter, there's now a real record of what fee was actually used for a loan originated today |
| 3 | Disclosures | Auto-generating the offer + disclosure on approval is still fully manual | ✅ Fixed:<br>• `run_decision()` now calls disclosure-service's `/offers` automatically the moment `decision-service` returns `approve`, using the application's own requested amount/term<br>• Best-effort — a disclosure-service hiccup logs a warning and doesn't fail the decision that already happened; the loan officer can still build it manually via `POST /los/offer`<br>• Verified live end-to-end: application 7302 (approve, score 672) auto-produced offer id 186 with `decision_id=7302`, `fee_pct_used=0.0300`, and an APR/schedule that were correct *for the formula then in use* — ⚠️ that formula is itself wrong, see Owed to the client<br>• All 182 backend tests still pass *(at the time)* | Directly answers the automation ask — the offer + TILA disclosure now exist the moment underwriting approves, with an auditable link back to that exact decision |

**A correction worth stating plainly — the client's own attached numbers don't
match what the code actually computes:** re-running `apr.compute_apr(18000,
7.99, 48)` returns **5.041%**, not the 7.142% the docstring's worked example
claims — the docstring's own example doesn't match its own code. Running that
*same* formula in float vs. Decimal gives 5.041% both times — float precision
alone is negligible here (<0.0001pp), not the 0.015pp the docstring implies.
The **real** gap: the test suite's reference calculation (correct 0.030 fee)
gives 5.196% against `apr.py`'s 5.041% (wrong 0.025 fee) — a **0.155pp gap**,
which *does* breach the 0.125pp Reg Z tolerance. It's a fee-constant bug wearing
a "float rounding" costume, confirmed by actually running the code rather than
trusting the docstring's own claimed numbers.

**This week's real deliverable, stated honestly:** a KG schema doc
(borrower→application→decision→offer→disclosure, including the currently-
missing decision→offer edge), a multi-agent disclosure-assembly prototype
design (one agent traverses the KG for an approved app's decision/offer
inputs, a second assembles the disclosure from them), the corrected
TILA-tolerance finding above, and an ADR (Decimal/minor-units + one
externalized rule-config source + TILA test vectors — full rules engine
explicitly deferred to the roadmap, not this week).

**Built, past the prototype stage:**
- `app/kg.py` (origination-service) — the KG schema is real FK-linked data in
  the one shared Postgres instance already (ADR 0002), so this is a traversal
  layer over the existing tables, not a second graph-database source of truth.
  `get_loan_history(app_id)` walks applicant → application → decision (+ its
  `decision_events` audit row) → every linked offer in one call — the concrete
  "trace this loan's whole history" answer, exposed staff-only at
  `GET /applications/{app_id}/history`.
- `app/disclosure_graph.py` — the two-agent hand-off, as a real LangGraph:
  `kg_reader` walks decision→application for the approved inputs,
  `assemble_disclosure` hands them to disclosure-service's existing
  deterministic Decimal engine. Deliberately **not** an LLM computing TILA
  math — an agent here is a LangGraph orchestration node with one job, not a
  model call; regulated dollar math doesn't get to be "agentic."
- `run_decision()`'s auto-offer-on-approval (Week 4 row 3, above) now runs
  through this graph instead of a direct HTTP call.
- Live-verified end to end: app 7307 (approve/672) → `kg_reader` found the
  persisted decision → `assemble_disclosure` produced offer 188 with
  `decision_id=7307`, `fee_pct_used=0.03`; `/history` returned the full graph
  in one call. All 191 backend tests pass *(at the time — see Verification baseline)*.

**Found live-testing this week's build, no client brief prompted these — all
fixed:**
- **Phone/SSN accepted anything** — `ApplicationIn.phone`/`.ssn` had zero format
  validation, client or server. Now require a 10-digit US phone / 9-digit SSN,
  normalized after stripping formatting. 5 new tests, live-verified: garbage
  input rejected with 422, valid formats accepted and normalized.
- **Real applicant data was silently discarded at intake** — `create_application()`'s
  INSERT statements never included `email`, `phone`, `employer`, `job_title`, or
  `employment_years`, despite the schema and API accepting all of them. Every
  application ever submitted had `employment_years = null`, which is exactly why
  every AI-summary request failed its no-data guardrail. Both INSERTs now include
  the full submitted payload; live-verified all fields now persist.
- **Stale sessions silently passed the staff-only page guard** — `RequireRole`
  only checked a cached `user` object in localStorage, never the token itself.
  A cached user can outlive its actual session (store restart, TTL expiry,
  revocation), so the guard let a dead token through and the real API call then
  failed with a bare "not authenticated" instead of a login redirect. It now
  calls `GET /auth/me` on mount and redirects to login on any failure.

**Status (2026-08-05): ✅ Landed.** PR #6
(`kalab-week4-disclosure-automation` → `main`) **merged**, merge commit
`ca1dbf9`, CI 22/22 green on its final head. All four table rows and all three
"found live-testing" fixes re-verified against the merged code.

Landing it took eight review cycles. The last three closed defects this week's
own table does not cover, because they were found in the review rather than the
brief: decision reruns performing bureau and audit side effects before losing a
finality race (fixed with a leased `decision_attempts` reservation), an
anonymous manual-review/PII disclosure, a plaintext submission token, PII in
intake and KYC logs, incomplete offer terms rendering as a real disclosure, four
migrations that could not replay onto a fresh database, and no browser coverage
at all for the manual-review path. Details in the PR; the point for this file is
that the week's *brief* shipped in week four and the week's *quality bar* took
until week five.

**Review rounds on this branch, all fixed:** the same three findings the
Week 3 PR needed (anonymous first-decision credit pull, negative/NaN/Infinity
payment amounts, un-deduped payment retries) recurred here independently —
this branch had diverged from `main` before Week 3's fixes landed there, so
merging `main` in surfaced the same gaps a second time on this branch's own
code, plus two more real bugs a closer review caught: a payment retry that
could reconcile against the *request's* `loan_id` instead of the originally
stored one, and a servicing-side idempotency marker that could commit without
the balance actually moving (both fixed: reconciles against the stored row,
409s on a mismatched key reuse, and marker+balance-update now commit
atomically). A staff-role check trusted `X-User-Role` alone with no way to
verify the caller actually came through the gateway — fixed with the same
`X-Internal-Token` pattern already used for the decision-service call.

**Scope note — this branch also carries work from later weeks, done ahead of
schedule:** gateway rate limiting (Week 7), a model card, a ZIP-level
fair-lending check, and a prompt-injection guard on policy-chat (all Week 8).
Flagging this now so Weeks 7/8 don't get re-built from scratch when their
turn comes — check `services/gateway/app/rate_limit.py`,
`docs/model_card.md`, `services/origination-service/app/fair_lending.py`, and
`services/loan-assistant/app/prompt_injection.py` for what's already done
before starting those weeks' own briefs.

---

## Week 5 — Spec-Driven Dev & Problem Scoping
### Feature: payment idempotency + card tokenization

**Domains touched:** Payments

**Client ask (Dana):** Let customers pay online (card + ACH) — "just add a
payment form." A few "charged twice" complaints, but "I think people are just
confused." Attached the last vendor's prototype handler. "Keep it simple."

**What client handed over:** three "charged twice" support tickets. A payment
log showing a slow (2.4s) `POST /payments`, a client retry, and a second POST
410ms later — both inserted. `payments.py` stores the full `pan` and logs the
`cvv` at INFO. No idempotency key or client request ID anywhere in the schema.

| # | Domain | What needed fixing | Fixed? | Why it mattered |
|---|---|---|---|---|
| 1 | Payments | "Charged twice" is real, not confusion — `payments.py` has zero dedupe; a retried POST unconditionally inserts a second row and applies the amount a second time | ✅ Fixed, past the spec stage — `idempotency_key` is now required at the API boundary, enforced by a partial unique index + `INSERT ... ON CONFLICT`, with the original result replayed on a repeat request (`services/payment-service/app/payments.py::charge()`) | The client's own framing ("people are just confused") was wrong — this is a real, reproducible double-charge bug, not a support-ticket misunderstanding |
| 2 | Payments | No idempotency-key contract exists — nothing tells the server a retried request is the same request | ✅ Fixed — atomic DB-level dedupe: `Idempotency-Key` required in `PaymentIn`, `UNIQUE` partial index (`db/migrations/0007`), `INSERT ... ON CONFLICT ... RETURNING` is the atomic check-and-write. A same-key retry with a *different* `loan_id`/`amount` gets a 409, not silently honored either way. A charge that captures but never confirms applying to the balance is tracked separately (`applied_at`, `db/migrations/0012`) and reconciled by the next retry — `servicing-service`'s own apply-payment is idempotent by `payment_id` too (`payment_applications`, `db/migrations/0013`), with the marker + balance update committed atomically | A safe retry needs to be dedup'd atomically at the database layer — check-then-insert in application code has its own race condition |
| 3 | Payments | Full PAN/CVV stored in the `payments` table; an unrelated SSN field accepted on the payment endpoint at all | ✅ **Landed** (PR #8, merged 2026-08-05; ADR 0008 is on `main`) — `PaymentIn` no longer has `pan`/`cvv`/`ssn` fields at all; the payment form tokenizes the card client-side (`frontend/lib/tokenize.ts`, a mock standing in for a real processor SDK) before it ever reaches a Meridian server. `pan`/`cvv` are **gone**: PR #15's `db/migrations/0031` dropped them and `db/init/001_schema.sql` no longer creates them, so neither a migrated nor a fresh database has them. *This clause used to say the columns stayed nullable and dead-going-forward for historical rows -- true until the contract step landed.* | CVV storage is an absolute PCI-DSS violation, no exceptions; SSN had no functional reason to be on a payment-capture endpoint at all — that was GLBA-covered data creeping into a PCI-scoped flow for nothing |
| 4 | Payments | No design for shrinking PCI scope — the current design touches raw PAN/CVV directly, maximizing scope | ✅ **Landed** (PR #8) — `payment-service` accepts only `processor_token` + `last4` + `brand`; the token is used transiently and never persisted (only `last4`/`brand` reach the `payments` row, `db/migrations/0016`). No real processor is integrated in this training app, so the tokenization boundary itself is mocked — the contract (opaque token in, only display fields ever stored) is real and is what a real processor integration would slot behind | Moves the service toward the lightest PCI SAQ tier instead of the current design, which did the opposite |

**Original deliverable, as originally described:** a spec package
(`specs/0001-online-payments-idempotency-tokenization.md`) — idempotency-key
design, PCI-tokenization + no-SAD-storage design, acceptance criteria, and
test vectors. **Spec only, no code changes to `payment-service`**, by design.

**Correction (2026-07-30):** that spec file did not exist anywhere in this
repo's git history, on any branch — confirmed via `git log --all`. Either it
was never actually committed or was lost at some point; either way, citing it
as delivered would have been repeating a claim this repo couldn't back up.
Rows 1 and 2 have since shipped as real code regardless (not written from
that spec, if it ever existed — landed via later security-review passes on
`kalab-week3-decision-memory` / `kalab-week4-disclosure-automation` /
`kalab-input-validation-fixes`, all merged). **Re-authored:**
`specs/0001-online-payments-idempotency-tokenization.md` documents Part 1
(idempotency) retroactively as-built, and Part 2 (tokenization) as a design
with acceptance criteria.

**Status (2026-08-06): ✅ Landed.** All four rows are on `main` — PR #8 merged
2026-08-05 21:34. `payment-service`'s `PaymentIn` has no `pan`/`cvv`/`ssn`
fields; card capture tokenizes client-side (`frontend/lib/tokenize.ts`, ⚠️ mocked
— no real processor integrated in this training app) before anything reaches a
Meridian server; new rows store only `last4`/`brand` (`db/migrations/0016`) and
the processor token is never persisted.

**Status on `main` now that PR #8 has merged:** neither `payment-service` nor
`servicing-service` inserts `pan`/`cvv` any more, and neither logs PAN/CVV/SSN --
both receive a processor token instead. This paragraph previously said the
opposite; it described the state while PR #8 was still open and was not updated
when it merged.

What remains: nothing, for this defect. The seed writers went in PR #11 (both
files insert `last4`/`brand` only), and PR #15 completed the contract step --
`db/migrations/0031` dropped `payments.pan`/`.cvv` and `db/init/001_schema.sql`
stopped creating them, so a freshly created database has no card columns at all.
`DEBT.md` D5b/D13 are closed. What is NOT claimed by that: PCI-DSS compliance,
which needs a QSA assessment and a real processor rather than an empty column.

**Also on PR #8, beyond this week's original scope** — added during review, not
from the brief: a captured payment could be authorized on the card and never
credited to the loan balance, recoverable only if the client happened to retry
the same idempotency key. `applied_at IS NULL` was queried nowhere in the
repository. `payment-service/app/reconcile.py` + `db/migrations/0028` make that
row a durable, self-draining work item with claim-safe concurrency, capped
backoff, an operator report and two Prometheus gauges.

Head `53ca666`, base `main`, CI 22/22 — **merged**. The two documents this file
cites, `specs/0001-online-payments-idempotency-tokenization.md` and
`adr/0008-tokenize-card-data-stop-storing-pan-cvv.md`, are on `main`. *This
paragraph previously said they existed only on the branch; true while it was
open, false once it merged.*

---

## Week 6 — AI-Augmented SDLC (Legacy Comprehension)
### Feature: servicing RBAC, maker-checker, append-only ledger

**Domains touched:** Servicing

**Client ask (Dana):** A servicing dashboard so reps can adjust balances,
waive fees, fix mistakes. "Reps are trusted folks — don't over-engineer
permissions, just make it usable."

**What client handed over:** `adjust-balance`/`waive-fee` accept any
authenticated user, no role check, no second approver. A single mutable
`balance` column, no ledger. A claimed repro: "a payment and a concurrent
fee-waiver both read `balance=500`, both write → the final balance is wrong."
The gateway forwards `X-User-Role`; servicing never reads it. The frontend
hides the Servicing nav from borrowers but enforces nothing server-side.

| # | Domain | What needed fixing | Fixed? | Why it mattered |
|---|---|---|---|---|
| 1 | Servicing | Any authenticated user, any role, could adjust any balance or waive any fee — no role check, no second approver | 🟡 Partially fixed, self-directed, ahead of this week's own build: the gateway now enforces staff-only on these actions server-side (41 passing tests, live-verified) | "Trusted reps" isn't a permissions model — a compromised or careless single account could zero out any loan with no second check |
| 2 | Servicing | No ledger — `balance` is overwritten in place; the prior value is gone the instant the next write lands, so "show me every change and who made it" is unanswerable | ⬜ Open | An append-only ledger (balance as a computed projection, not a mutable column) is the only way to answer a controller's audit question at all |
| 3 | Servicing | No maker-checker / segregation of duties on any money-affecting action | ⬜ Open | A single person moving money with no second approver is a real internal-controls gap, not just a technical one |
| 4 | Servicing / Gateway | Frontend hides the Servicing nav from borrowers, but the actual endpoints accept any authenticated caller regardless — security-by-UI-obscurity | 🟡 Partially fixed — the gateway now enforces the real check server-side (see #1); the UI hiding was already harmless once the backend check exists, but was previously the *only* thing stopping anyone | Hiding a button is not a permission system — the real gate has to live where the request actually lands |

**A correction worth stating plainly — the client's own reproduction is
verified wrong, checked directly against `servicing-service/app/balance.py`:**
`apply_payment()` writes the `balance` column; `waive_fee()` writes the
**`past_due`** column — different columns, so that exact pairing was never
going to collide the way described. The real lost-update repro needs two
operations on the *same* column: two concurrent `apply_payment` calls, or
`apply_payment` + `adjust_balance` together (both write `balance`). Either
pairing genuinely loses an update; the payment+waiver pairing specifically
does not, and shipping a "failing test" built on the brief's own wrong
scenario would have proven nothing.

**This week's real deliverable, stated honestly:** a legacy-comprehension
report (who can call each money-affecting endpoint today, how balance
actually mutates, the corrected concurrency scenario above), characterization
tests pinning today's behavior as a baseline, one failing test proving the
*correctly-paired* lost update, and an ADR proposing RBAC + maker-checker +
an append-only ledger. **Not the dashboard build — and the maker-checker
step and the ledger are still fully unbuilt.** Only the RBAC/ownership half
of the ADR's own proposed fix shipped, and it shipped self-directed, at the
gateway, ahead of this week's own official scope.

---

## Week 7 — Observability / SRE / Guardrails
### Feature: cross-service trace ID + scoped reconciliation control

**Domains touched:** Payments · Servicing · Finance

**Client ask (Dana):** "Payments feel flaky," finance grumbles about month-end
"noise" they just write off. Wants visibility so Dana isn't the last to know
when something's off. Attached a month of payments plus the processor's
settlement file — "mostly tie out."

**What client handed over:** `reconciliation.py`'s `ledger_total()`/
`settlement_total()` stub — never scheduled, reports no breaks. Raw payment
logs with no trace IDs. Finance's own line: "month-end is always a little
noisy, we just adjust to the bank number."

| # | Domain | What needed fixing | Fixed? | Why it mattered |
|---|---|---|---|---|
| 1 | Payments / Finance | `ledger_total()` sums **every row ever inserted into `payments`**, no date or loan filter; `settlement_total()` sums a fixed CSV covering one specific 7-day window across 3 loans — comparing an all-time, all-loan number to a 1-week, 3-loan number was never going to tie out | ⬜ Open | This isn't noise — the two numbers were never measuring the same population; a correctly-scoped comparison is required before "does it tie out" is even a meaningful question |
| 2 | Finance | Whether the gap is random or a real, directional leak | ⬜ Open, but verified directional for the comparable slice: the seed data only carries payment rows through 06-03 while `settlement.csv` has captures for the same loans through 06-07 — settlement consistently shows *more* captures than the ledger has rows for | Directional, one-sided gaps are the signature of a real leak, not rounding noise — worth escalating, not writing off |
| 3 | Payments | A real double-fund event exists in this exact month's data (loan 5582, two identical $410.50 charges 2 seconds apart) and nothing flags it | ⬜ Open | Confirmed: no trace ID or request ID connects the two `POST /payments` attempts — indistinguishable from two genuine charges without a human manually diffing rows by loan_id + amount + near-identical timestamp |
| 4 | Payments | `payments` has **no `processor_ref` column at all** — even a correctly-scoped reconciliation could only match rows approximately (loan_id + amount + nearby date), never definitively by charge reference | ⬜ Open | This is itself part of why nobody can produce an exact break-report today, not just a missing job. Without the processor's own reference on the row there is no join key, so a comparison can only net totals per loan — and two wrong transactions on one loan then cancel, which is a control that reports success for having hidden its own findings |

*The four rows above are measured against `main` at the audit base, and this is
the table most likely to move first: `DEBT.md` **D7** is open and the work that
closes it changes rows 1 and 4 together. Check `git log db/migrations` and
`gh pr list` rather than reading a ⬜ here as current.*

**This week's real deliverable, stated honestly:** **one** instrumented path
(a shared trace/correlation ID connecting `payment-service`'s `charge()` to
`servicing-service`'s `apply_payment` — today the two hops share nothing that
would let anyone connect them in logs) and **one** control (a reconciliation
job correctly scoped to `settlement.csv`'s actual date range and loan set,
producing a break-report, plus one alert on a reconciliation break). Run
against a sampled month (matching the settlement file), not full history —
per the brief's own quota note. **One path, one control — not full
observability.**

**Status (2026-08-05): 🟡 Partial — and the partial piece is not the piece this
week scoped.** A Prometheus + Grafana stack landed early (`monitoring/`,
scraping `/metrics` off all eight services) during the Week 4 branch, so this
week is no longer "not started". But metrics are not what the four rows above
ask for. Both of Week 7's own deliverables are still **⬜ Open**: there is no
shared trace/correlation ID connecting `payment-service.charge()` to
`servicing-service.apply_payment`, and `reconciliation.py`'s `ledger_total()` /
`settlement_total()` still take no date or loan filter — re-verified today — so
rows 1 to 4 all stand exactly as written.

One row moved for a different reason: PR #8's reconciler now detects and reports
captured-but-unapplied payments, which is a real partial answer to row 3's
"nothing flags it". It flags the *unapplied* case, not the *double-charge*
case, and it is on `main` now that PR #8 has merged. *Previously read "not on
`main` yet".*

---

## Week 8 — Security / Governance / Responsible AI
### Feature: model governance + fair-lending screen

**Domains touched:** Decisioning · KYC (data governance)

**Client ask (Dana):** The board loves the AI scorer — roll it out to more
products and put together a marketing page on "how advanced" the underwriting
is. "Wider is better, right?"

**What client handed over:** decision logs claimed to show every denial
carrying the same generic reason from a hardcoded `GENERIC_REASONS` list. No
model card. No fair-lending disparity check. An attached ZIP-level
approval-rate breakdown showing a pattern.

| # | Domain | What needed fixing | Fixed? | Why it mattered |
|---|---|---|---|---|
| 1 | Decisioning | Client's claim: every denial gets one of two hardcoded strings regardless of the real driver | ✅ **Already fixed — checked directly against the current code, the client's own attached logs are stale.** `decision.py` no longer has `GENERIC_REASONS` anywhere; Week 3's real fix already replaced it with an input-driven mapping (bureau-score shortfall vs. income shortfall). The still-real gap: that mapping only ever picks between **two** categories, and it isn't fixture-tested against known cases yet — that's this week's actual remaining work | Citing a stale finding as current would be its own credibility problem — the fix already shipped, the remaining gap is narrower than the brief assumes |
| 2 | Decisioning | Does the ZIP-level approval-rate pattern warrant a disparate-impact look? | ✅ **Now checkable — the blocker is gone.** When this row was written no ZIP field existed anywhere; `applicants.zip_code` was added (`db/migrations/0014`) and `fair_lending.py` computes a ZIP3-level four-fifths-rule screen over recorded approval outcomes, staff-only at `GET /applications/fair-lending/zip-analysis`. Local/training-only — never run against real applicants | "Can't check" was itself a reason to pause before "wider." That reason no longer holds, which means the screen's *output* now has to be looked at rather than the check being unavailable |
| 3 | Decisioning | No model card, no record of which features/model version produced any given decision, no fairness testing ever performed | 🟡 **Two of three closed.** `docs/model_card.md` documents the model, its inputs, its bands and its limits; `decision_events` records the exact model version and score behind every decision (Week 3). **Still fully open: no fairness testing of the model itself** — no protected-class or proxy analysis of its scores, no vendor fairness documentation. The ZIP screen in row 2 is an *outcome* monitor, not model validation, and the model card says so explicitly | Two thirds of the answer now exists. The remaining third is the one a regulator would actually press on, so it is called out rather than folded into a green tick |
| 4 | Decisioning | What "responsible AI" requires beyond a marketing page | ⬜ Open — reason accuracy is partially there (see #1), a model card and a monitoring spec are not | A marketing claim of "advanced underwriting" with zero governance behind it is the same pattern as the README's earlier false PCI claim — a claim not backed by what the system can prove |

**This week's real deliverable, stated honestly:** a reason-code →
specific-reason mapping, **fixture-tested only** (per the brief's own quota
note — no live-model spam), a model card
(shipped as `docs/model_card.md`) documenting the model, its
decision bands, and its known limitations, and a fair-lending monitoring spec
(denial-reason accuracy + disparity-check design, explicitly naming the
missing-ZIP-field prerequisite). **Governance artifacts — not a model
rebuild, not a wider rollout, not the marketing page. Not yet started — plan
only, pending go-ahead to build.**

---

## Week 9 — Client Specialization Track: Lending Compliance / BSA-AML
### Feature: beneficial ownership + sanctions screening

**Domains touched:** KYC

**Client ask (Dana):** Launching a new product, wants KYC "tightened" during
onboarding. "We verify the applicant's identity already, so I think we're
mostly there — just make it look thorough for the launch."

**What client handed over:** `kyc-service/app/kyc.py` — CIP only (name/DOB/
address/SSN presence checks), nothing past it. A sample onboarding: an LLC
applicant cleared with no beneficial owner ever identified.

| # | Domain | What needed fixing | Fixed? | Why it mattered |
|---|---|---|---|---|
| 1 | KYC | What `kyc-service` actually checks vs. full KYC/AML | ⬜ Open, confirmed by reading the code directly: `run_cip()` does presence checks only (`bool(applicant.get("name"))` etc.) — there isn't even a stubbed external cross-reference, unlike `decision-service`'s bureau call | "We verify ID" and "KYC is handled" are not the same claim — CIP is the *first* step of BSA/AML, not the whole program |
| 2 | KYC | For the LLC applicant, who are the beneficial owners, were they screened? | ⬜ Unanswerable by design — confirmed: **no beneficial-owner field exists anywhere** in `applicants` or `kyc_checks`; the seeded entity applicant (`Northgate Holdings LLC`, EIN only, no SSN/DOB) cleared with `outcome: approve` on a company name, address, and EIN alone | An LLC can onboard today with zero real person ever identified, let alone screened — there's nowhere in the schema to even record the answer |
| 3 | KYC | Could a sanctioned party clear onboarding today? | ⬜ Yes, trivially confirmed: `cip_passed` only requires non-empty name and address strings; `sanctions_screened=False`/`ubo_captured=False` are hardcoded directly in the API response, not computed from any real check | No OFAC/SDN cross-reference exists anywhere in the codebase — nothing would catch a sanctioned party at any point in the flow |
| 4 | KYC | CIP vs. CDD/ongoing-monitoring/SAR — different questions, not the same check done more thoroughly | ⬜ Open (spec this week, not built) | The compliance worksheet's "we check ID ✓" next to a blank "sanctions screen?" field is CIP mistaken for the whole program — exactly what "we're mostly there" gets wrong |

**This week's real deliverable, stated honestly:** a KYC/AML specialization
spec — a `beneficial_owners` table design (owner name, ownership %,
control-person flag, enforcing FinCEN's 25%-plus-one-control-person rule), a
`SanctionsScreeningProvider` interface (same abstraction shape as
`decision-service`'s planned `CreditBureauClient`, so this doesn't repeat that
same hardcoding mistake with a different vendor), ongoing-monitoring/SAR
trigger points, a screening-integration ADR, and acceptance criteria (e.g. "no
applicant reaches `cip_passed = true` without a completed sanctions screen
returning clear"). **Spec + ADR only — screening-vendor integration scoped,
not built. Not yet started — plan only, pending go-ahead to write it.**

---

## Week 10 — Project Work & Client Showcase (capstone)
### Feature: retention-aware redaction + handoff package

**Domains touched:** Finance · Decisioning · KYC · Payments · Servicing (all — this is the integration week)

**Client ask (Dana):** A clean "delete my data" button — "full hard delete,
the works" — for privacy requests. Then package the whole engagement for a
board presentation, in risk/money/defensibility terms.

**What client handed over:** a retention map assembled across prior weeks —
Reg B (adverse-action reasons, ~25-month retention) and SOX (financial
records, multi-year) both legally require *keeping* records that a hard
delete would destroy.

| # | Domain | What needed fixing | Fixed? | Why it mattered |
|---|---|---|---|---|
| 1 | Finance | "Hard-delete everything" vs. the retention map | ⬜ Open, confirmed: nothing today separates identifying PII from legally-required evidence — `decisions`/`offers`/`loans`/`balances`/`payments`/`kyc_checks` mix both in the same rows, no field-level split to delete around | A literal hard-delete button would destroy exactly the records the law requires keeping — it's not a privacy feature, it's a compliance-violation generator |
| 2 | Decisioning | Deleting a denied applicant's record would take the Week 3 `decision_events` audit trail with it | ⬜ Open | That audit trail is the specific evidence that a denial reason was accurate and non-discriminatory — deleting it on request means Meridian can no longer defend that decision if a fair-lending challenge comes later |
| 3 | Finance | How to honor a privacy request without erasing legally-required records | ⬜ Open — design only: redact/tokenize directly-identifying fields (SSN, PAN, email, phone, free-text address, name) while retaining structured fields (outcome, reason codes, score, timestamps, financial amounts, KYC-performed booleans) against an identifier no longer linkable to the real person | "Erase identity, keep the regulator-facing facts" is the actual fix — an all-or-nothing button was never going to satisfy both privacy and compliance at once |
| 4 | Finance | Which records are safe to delete vs. must survive | ⬜ Open — safe: raw SSN/PAN/CVV/email/phone/DOB/address once nothing else needs them attached to an identity; must survive (redacted): decision outcome+reason+score+timestamp (Reg B), financial/loan/offer/payment records (SOX), KYC-performed evidence (BSA), and — once built — the Week 6 append-only ledger | Makes "tightened privacy" checkable against a real policy table instead of a single destructive button |

**The controls-and-audit-trail review this week actually calls for — corrected
against the current repo, not just repeated from the plan's own words** (the
same check applied to Week 8's stale claim above, now applied to this week's
own central task): the assumption that "Weeks 3/6/7 are all spec-only, not
built" is **not fully accurate**, checked today:
- **Week 3** — `decision_events` is real, tested, shipped code, **merged to
  `main`** (PR #5, 2026-07-30). Landed, not spec.
- **Week 6** — the RBAC/ownership half of the ADR's proposed fix shipped,
  self-directed, at the gateway (41 tests at the time, live-verified). The
  maker-checker step and the append-only ledger are still genuinely unbuilt.
- **Week 7** — no longer untouched: the Prometheus/Grafana stack landed early.
  But neither thing Week 7 actually scoped (a cross-service trace ID, a scoped
  reconciliation control) exists, so its own rows are all still open.
- **Week 4** — merged to `main` (PR #6, 2026-08-05), including the
  auto-disclosure chain this capstone depends on.
- **Week 5** — built, CI-green and **merged** (PR #8, 2026-08-05). A showcase must not
  present tokenization as delivered while it sits on an open branch -- which is
  no longer the situation: PR #8 merged on 2026-08-05 and tokenization is on
  `main`, so a showcase may present it as delivered because it is.

A showcase that just repeats "Weeks 3/6/7 are specs" would understate real,
verifiable progress — the honest version says exactly which piece of each
week is built vs. planned, not a blanket status for all three.

**This week's real deliverable, stated honestly:** a retention-aware
redaction policy table mapping every field across `applicants`/`applications`/
`kyc_checks`/`decisions`/`offers`/`loans`/`balances`/`payments`/`audit_logs` to
its legal basis and retain-vs-redact status, the corrected controls review
above, a handoff package (debt register, ADR catalog, risk-prioritized roadmap —
**not yet committed, so not cited as a path**), and a board-framed showcase (likewise
— fair-lending defensibility, money-correctness, audit-readiness). **Not yet
started — plan only, pending go-ahead to build.**

---

---

## Not on any week's brief

Work that shipped without a client brief asking for it. It sits outside the
week structure, which is exactly why it kept going unrecorded — including the
one finding the Week 1–4 client review singled out as the most valuable thing
in the engagement.

### Test and CI infrastructure — ✅ Landed

**CI had never run a single test.** `pytest` was not in any service's
requirements and the test step carried `|| true` on top of `continue-on-error`,
so every "backend (X)" check reported success without executing anything, for
every PR, for the whole engagement. A defect in the process that was hiding
every other defect. Each service now has `requirements-dev.txt`, the masking is
gone, and a real failure is visible.

Everything else in `.github/workflows/ci.yml` was also built here, none of it
briefed. All blocking unless noted:

| Job | What it catches |
|---|---|
| `secrets` | gitleaks over full history. Has already caught a real commit — a plausible-looking token in a test fixture |
| `backend` | All eight suites, with a real PostgreSQL service so the `skipif(not DATABASE_URL)` tests actually run instead of silently skipping |
| `db-migrations` | Every migration applied to real PostgreSQL, plus the parity suite below |
| `docker-build` | `docker compose build` on a clean checkout — every Dockerfile used to `COPY` a gitignored CA bundle and fail |
| `e2e` | Full stack up, Playwright drives the browser, Postgres rows verified directly |
| `frontend` | `npm run build` |
| `dependency-audit`, `dependency-audit-frontend` | pip-audit / npm audit. **Non-blocking** — first run's findings are not triaged, so a green tick here is not a clean-scan result |

**Migration parity** (`db/tests/test_migration_paths_converge.py`) builds the
schema four ways — fresh init; legacy plus migrations; fresh init plus
migrations; migrations applied twice — and asserts they converge on the same
columns, unique constraints, CHECK constraints *with validation state*,
indexes, foreign keys and defaults. Four migrations could not replay onto a
fresh database at all before this; the legacy-versus-fresh comparison then found
five indexes that `db/init` creates and no migration ever did.

**Browser end-to-end** (`frontend/e2e/`, **12** spec files, re-counted
2026-08-11): approved, denied, existing-offer, both halves of the manual-review
path, the review-step edit affordance, the submission edit lock, the offer
disclosure UI, the payment-plan display, the reconstructed-schedule warning,
regeneration repricing the offer, and the summary's external signal. *This read
7 until the audit; five specs were added by PRs #10–#14 without the count being
updated.* The count is of `*.spec.ts` files in that directory — `fixtures.ts` is
shared helpers, not a spec. Each verifies
PostgreSQL rows directly rather than trusting the screen. The interactive
verification these replace was never checked in, so it was not repeatable by
anyone else — the same closure gap as the roadmap itself.

### Review-driven architecture work — ✅ Landed (PR #6)

Not on any brief; came out of the review cycles on the Week 4 branch.

- **`decision-service/app/bureau.py`** — the `BureauClient` seam. This is the
  abstraction RF-21 predicted would be needed ("same shape as the planned
  `CreditBureauClient`"); it now exists, carrying an idempotency key so a retry
  after an ambiguous timeout recovers the original pull instead of billing a
  second hard credit inquiry. Honest limit: no real bureau is integrated, so the
  contract is verified against our own stub.
- **`origination-service/app/decision_state.py`** — the leased
  `decision_attempts` reservation (`db/migrations/0023`) that lets the external
  decision call happen with no transaction held while still guaranteeing at most
  one in-flight attempt per application, with a fixed global lock order
  (`applications` → `decision_attempts`) to avoid deadlock.

### Documentation — ✅ Landed

- [`DEBT.md`](DEBT.md) — the D/RF register. Never existed in any form before,
  despite being cited across the ADRs, the runbook and the source.
- `adr/0001` (record architecture decisions) and `adr/0004` (decompose the
  origination monolith) predate the week structure and are referenced from
  `ARCHITECTURE.md` rather than from any week here.
- `adr/0007` — see the DTI/fraud finding under Week 2.
- `adr/0009` — the graph-store answer owed from the Week 1–4 review. Written
  against a measurement rather than an opinion: the recursive CTE that expresses
  the traversal `kg.py` cannot, and the depth at which it stops being usable.

## Keeping this file honest

All 10 weeks logged. When a PR merges or a status changes, update **two**
places: the week's own status line, and the **Status at a glance** table at the
top. The at-a-glance table is the one thing that must never be stale — it is
what anyone skimming this file reads, and a wrong tick there is worse than no
table at all.

Four rules that this file has broken before, and which later passes fixed:

1. **Never mark a row ✅ for work on an unmerged branch.** Use 🟠. Week 5 claimed
   all four rows closed while two of them sat on an open PR.
2. **Never cite a file as delivered without checking it is on `main`.** Week 5
   cited a spec and an ADR that exist only on a feature branch, and an earlier
   version cited a spec that had never been committed anywhere at all.
3. **Never carry a status forward from a code comment.** Week 1's PII row said
   origination's middleware still logged full request bodies. No such middleware
   exists — the claim came from a docstring and was repeated twice before anyone
   grepped for the call site (`DEBT.md` D5c).
4. **Never leave a 🟠 standing after the PR it was waiting on merges.** Week 5
   sat at 🔵 for a day after PR #8 landed, and its "none of that is true on
   `main`" paragraph became actively misleading rather than merely stale. ✅ is
   the *correct* marker once the PR lands — the defect is the stale marker
   outliving the merge, not the tick replacing it.

5. **Never let this file carry a PR inventory.** Every previous version did, and
   every one of them was wrong within a day — a footer saying "no PR open" above a
   table listing four, a row marking a PR open that had merged hours earlier, a CI
   run id cited as proof long after the branch moved. The rule that replaced them
   is simpler than getting the inventory right: **the file does not carry PR or CI
   status at all.** Volatile measurements live in *Audit snapshot*, dated and
   fenced; everything else states what must be true, which does not change when a
   branch does.

Last full accuracy pass: **2026-08-13**, against `main` at `1528226`. That is the
base the Weeks 1–6 matrix and the gap list were re-checked against, and it is what
the footer is for — a freshness marker naming the commit, and nothing about which
pull requests happened to be open while it was written. **For what is in flight,
run `gh pr list`.**

*Two earlier versions of this footer are worth recording, because rule 5 is what
they cost. One read "with PRs #1–#17 all merged and no PR open" while the section
above it listed four open — a freshness marker contradicting the document it
certifies, which undermines every other claim on the page including the correct
ones. The cause was writing the footer against the state the audit ran in and the
table against the state it produced: the audit's own output changed the thing it
was reporting on. The other read 2026-08-10 against `e5f9d52` and described #17 as
"this pass" — #17 merged at 18:28 UTC that day, so the footer outlived its own PR,
rule 4 failing on the line that states it. Neither is possible now: the footer
names a commit and says nothing about pull requests.*
