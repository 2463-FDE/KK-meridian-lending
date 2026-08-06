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

| Week | Feature | Status |
|---|---|---|
| 1 | Safe LLM engine (client, redactor, secrets cleanup) | ✅ Landed |
| 2 | RAG retrieval + corpus hygiene | ✅ Landed |
| 3 | AI scorer wrapper + append-only decision memory | ✅ Landed |
| 4 | Auto-disclosure on approval + KG traversal | ✅ Landed |
| 5 | Card tokenization + payment reconciliation | ✅ Landed (PR #8, 2026-08-05). Column drop 🟠 PR #15 |
| 6 | Servicing RBAC / ledger / maker-checker | 🟡 RBAC landed; ledger + maker-checker open |
| 7 | Trace ID + scoped reconciliation control | ⬜ Open (Prometheus/Grafana landed early) |
| 8 | Model governance + fair-lending screen | 🟡 Model card, ZIP screen, prompt-injection guard landed; disparity monitoring open |
| 9 | BSA/AML — UBO + sanctions screening | ⬜ Open (spec not written) |
| 10 | Retention-aware redaction + handoff package | ⬜ Open |
| — | **Not on any brief:** test + CI infrastructure, browser E2E, migration parity, the bureau/decision-attempt seams, this register | ✅ Landed |

### Open pull requests

Eight open. **None has a human review**, so none is ✅. CI status is stated per
row rather than asserted for all — GitHub Actions has been in a major outage
since 2026-08-06 15:22 UTC, so a red or pending check on these is not evidence
about the branch.

| PR | Concern | Additions | Status |
|---|---|---|---|
| #10 | Actuarial APR + TILA box foots | +598 | 🟠 CI green |
| #11 | `payments.pan`/`cvv` removal — expand half | +281 | 🟠 CI green |
| #12 | Graph-store threshold answered by measurement (`adr/0009`) | +400 | 🟠 CI green |
| #13 | One grounded external signal on the officer summary | +514 | 🟠 CI green |
| #14 | Correct a wrong answer from the review screen | +211 | 🟠 CI green |
| #15 | `payments.pan`/`cvv` removal — contract half (`DROP COLUMN`) | +108 | 🟠 CI green · base is #11's branch |
| #16 | Four docstrings claiming a PII leak the code does not have | +52 | 🟠 **CI not green** — `e2e` reaped at 56m with zero steps recorded during the outage; 19 of 22 checks still queued; `mergeStateStatus` UNSTABLE |
| #17 | This status and citation pass | +114 | 🟠 **No CI run exists.** The outage swallowed the `pull_request` trigger, so unlike #16 there is nothing queued to drain — it needed a re-push to create a run. Not a path filter: `ci.yml` has none, and the docs-only PR #9 ran the full suite |

**Merge order — not arbitrary:**

1. **#16 before #17.** #17 cites `DEBT.md` D5c, which only exists on #16's branch.
2. **#11 before #15.** #15's base *is* #11's branch.
3. **#12 and #13 conflict with #17** — all three edit the same two rows of the
   "Owed to the client" table below. Whichever lands second needs a manual
   resolution, and #12/#13 mark those rows ✅ where rule 1 of *Keeping this file
   honest* requires 🟠. Take the 🟠 version.
4. #10 and #14 are independent of the rest.

## Owed to the client

Three answers outstanding from the Week 1–4 review, tracked here so they are
not lost between weeks:

| Question | Status |
|---|---|
| What a graph database would buy the disclosure chain that foreign keys do not | ✅ **Answered** — [`adr/0009`](../adr/0009-graph-store-for-identity-traversal.md), with a **reproducible** benchmark: [`db/bench/graph_traversal_benchmark.py`](../db/bench/graph_traversal_benchmark.py) commits the generator, index DDL, exact query and timing method. The traversal `kg.py` cannot express is *every applicant reachable through any shared identity attribute, to unbounded depth* — address, phone, email, ssn, ein, employer. PostgreSQL **can** express it with a recursive CTE. The benchmark now compares three implementations of the same traversal (root-scoped frontier expansion, a prebuilt indexed edge table with construction timed separately, and the original global-edge build kept only as a pessimistic baseline), aborts if their reachability counts disagree, and asserts the posting table and edge table are the same relation as sets before timing anything. From one run (`db/bench/results.json` + `db/bench/run-output.txt`, both written by a single invocation and carrying the same `run_id` 2026-08-10T17:26Z-rows10000-depth5-root1, 10k applicants, PostgreSQL 16.14): depth 3 answers in **0.047s**, depth 4 in **0.155s**, depth 5 in **0.322s** (2,944 applicants reached); the genuinely *unbounded* walk — keyed on the applicant alone, so it terminates when the component is exhausted rather than needing a depth bound — returns the whole 10,000-applicant component in **0.723s**; and the traversal as the ADR defines it, unbounded reachability **with the connecting path for every applicant, each hop labelled with the attribute that justifies it**, takes **1.023s** (frontier/visited walk keeping one predecessor and one attribute each; 205 routes sampled and validated against the labelled edge relation, 0 invalid). An earlier figure timed `count(*)` only and returned bare applicant IDs, neither of which supported the claim. The pessimistic baseline is not a one-time adjacency cost either: `AS MATERIALIZED` stops the relation being rebuilt per hop, not rescanned, and the plan records 5 scans of 553,928 rows. The depth-bounded form cannot answer the unbounded question by removing its bound: it keys on (id, depth) and would never terminate, which is a separate review finding. Two earlier sets of figures were benchmark defects, not PostgreSQL's cost — first a global adjacency rebuild per query ("~3s to depth 3 / 72.3s at depth 4"), then simple-path enumeration through a cyclic graph reported as reachability ("16.9–38.7s at depth 4, no return at depth 5"). **There is no depth cliff**; the refusal stands because nothing in production needs the query at all, with a written trigger to revisit (Week 9 beneficial ownership, a weighted-path problem rather than a latency one) |
| An independent source for the TILA expected values | ⬜ **Open.** `test_apr.py` has one vector, and its `_decimal_apr()` reference re-implements the same closed-form as `apr.py` — it can catch a precision regression but not a wrong formula. Owed: expected values from a source that is not this code, and more than one vector |
| The roadmap and debt register the ADRs keep citing | ✅ **Landed.** This file, plus [`DEBT.md`](DEBT.md) — the `D`/`RF` register, which had never existed in any form. All 16 citations in the tracked tree now resolve |

## Verification baseline

Counts below were measured at **`ca1dbf9`** (the PR #6 merge, 2026-08-05).
⚠️ **PRs #8 and #9 merged after that commit, so every figure here is
understated — re-measurement pending.** Reproduce with `python -m pytest -q` per
service, `python -m pytest db/tests -q`, and `npm run test:e2e` in `frontend/`.

- **441** backend tests across all eight services
- **76** db migration tests against real PostgreSQL
- **5** Playwright end-to-end specs driving the browser

Per-week test counts appear below as they were at the time that week shipped;
they are historical, not current, and are labelled as such.

---

## How the app works (fast reference)

8 backend services + Postgres + Redis + Next.js frontend, all behind one gateway (port 8000, the only host-reachable API). Frontend on port 3000.

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
| 1 | Payments | `payment-service.log` writes full PAN, CVV, SSN in plaintext on every charge | 🟡 **Partial — three halves, one closed.** ✅ `payment-service.charge()` redacts via a ported copy of `loan-assistant/redactor.py` before logging. ✅ `servicing-service/app/payments.py` logs `loan_id`/`amount`/`method` only, and receives a processor token rather than a PAN (ADR 0008). ✅ Origination's intake logs `app_id`/`applicant_id`; the "request middleware logging full POST bodies" named here **never existed** — that claim came from a copy-pasted docstring, which is the D5c defect reproducing itself inside this roadmap. 🟨 Storage: the `pan`/`cvv` columns still exist, nullable and **unwritten** — the seed writers went in PR #11, so both seed files insert `last4`/`brand` only and a fresh database contains no card data (a seed-content test enforces that, failing on a PAN-shaped literal in any inserted column or seed target). PR #15 drops the columns, which closes D5b/D13. *This line previously said the seeds "still write real values into them, so every fresh database contains card data" — true when written, false since PR #11.* **And the log file itself stayed committed to the repo until 2026-08-05** — the code was fixed weeks before the artifact it produced was removed, which is the closure gap the client review led with. See `DEBT.md` | CVV storage/logging is an absolute PCI-DSS violation, no exceptions — a leaked log is a breach, not a bug |
| 2 | Origination / Decisioning | Bureau + core-banking + processor keys hardcoded in `config.py`, also committed in root `.env` | ✅ Effectively closed — `.env` untracked, hardcoded fallbacks removed from all 7 services. **Confirmed with the project owner: these were training placeholders (`EXAMPLE-LEAKED-KEY-rotate-me`), never real provider accounts** — so there's no live credential to rotate, and the old values still in git history aren't a real security exposure, just cosmetic (a reviewer seeing placeholder-labeled strings in `git log`). A history rewrite remains available on request but isn't fixing an actual vulnerability here | For a *real* deployment this would be a genuine breach risk (a leaked bureau key pulling real credit data under Meridian's name) — confirmed not the case for this training instance specifically |
| 3 | Finance | Money stored/computed as `float` everywhere (`0.1 + 0.2` problem) | ✅ Fixed, both layers — **but see the ⚠️ note under Owed to the client: exact arithmetic is not the same as the right formula, and `compute_apr` on `main` still uses the wrong one.** Details:<br>• **Computation** — `disclosure-service` + `servicing-service` compute in `Decimal` throughout (`apr.py`, `offer.py`, `schedule.py`, `balance.py`, `delinquency.py`); `payment-service.charge()` quantizes to exact cents before storing/forwarding<br>• **Storage** — all 14 money columns migrated `DOUBLE PRECISION` → `NUMERIC` (`db/migrations/0005_money_columns_to_numeric.sql`), applied live against a populated 307-row DB, no data loss. `asdecimal=False` on the ORM models keeps it storage-only, no Decimal ripple<br>• **Regression caught + fixed** — post-migration live test broke `run_decision()`: raw-psycopg2 reads of a `NUMERIC` column return `Decimal` (unaffected by `asdecimal`), and forwarding that via `httpx.post(json=...)` crashed (`Decimal is not JSON serializable`). Fixed with `float(...)` at the forward boundary; audited every other cross-service call site, none else affected<br>• All 182 backend tests pass *(at the time — see Verification baseline for current)* | Rounding error compounds across balance updates and APR calculations — this exact fault line also caused a real Reg Z disclosure violation. Schema fix alone surfaced a live bug only end-to-end testing against a real populated DB would catch |
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
| 3 | Payments | Full PAN/CVV stored in the `payments` table; an unrelated SSN field accepted on the payment endpoint at all | ✅ **Landed** (PR #8, merged 2026-08-05; ADR 0008 is on `main`) — `PaymentIn` no longer has `pan`/`cvv`/`ssn` fields at all; the payment form tokenizes the card client-side (`frontend/lib/tokenize.ts`, a mock standing in for a real processor SDK) before it ever reaches a Meridian server. `pan`/`cvv` columns stay nullable/dead-going-forward for historical rows (not dropped — retroactive tokenization is its own project, same shape as the Week 10 retention question) | CVV storage is an absolute PCI-DSS violation, no exceptions; SSN had no functional reason to be on a payment-capture endpoint at all — that was GLBA-covered data creeping into a PCI-scoped flow for nothing |
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

What remains: the `pan`/`cvv` COLUMNS still exist, and `db/init/002_seed.sql`
and `003_seed_bulk.sql` still write real values into them, so a freshly created
database contains card data regardless of what the application does. PR #11
(the `last4` back-fill) is merged; PR #15 drops the columns, and the seed
writers must go with them (see `DEBT.md` D5b/D13).

**Also on PR #8, beyond this week's original scope** — added during review, not
from the brief: a captured payment could be authorized on the card and never
credited to the loan balance, recoverable only if the client happened to retry
the same idempotency key. `applied_at IS NULL` was queried nowhere in the
repository. `payment-service/app/reconcile.py` + `db/migrations/0028` make that
row a durable, self-draining work item with claim-safe concurrency, capped
backoff, an operator report and two Prometheus gauges.

Head `53ca666`, base `main`, CI 22/22, mergeable. Two documents this file cites
— `specs/0001-online-payments-idempotency-tokenization.md` and
`adr/0008-tokenize-card-data-stop-storing-pan-cvv.md` — exist **only on that
branch**, not on `main`.

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
| 4 | Payments | `payments` has **no `processor_ref` column at all** — even a correctly-scoped reconciliation could only match rows approximately (loan_id + amount + nearby date), never definitively by charge reference | ⬜ Open | This is itself part of why nobody can produce an exact break-report today, not just a missing job |

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
case, and it is not on `main` yet.

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
- **Week 5** — built and CI-green on PR #8, **not merged**. A showcase must not
  present tokenization as delivered while it sits on an open branch.

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

**Browser end-to-end** (`frontend/e2e/`, 7 spec files): approved, denied,
existing-offer, both halves of the manual-review path, the review-step edit
affordance, and the submission edit lock (in-flight and post-failure). The
count is of `*.spec.ts` files in that directory — `fixtures.ts` is shared
helpers, not a spec. Each verifies
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

Last full accuracy pass: **2026-08-06**, against `main` at `90ebce2` plus open PRs #10–#17.
