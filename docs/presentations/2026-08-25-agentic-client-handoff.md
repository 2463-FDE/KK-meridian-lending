# Agentic underwriting — client handoff

**Recorded at main:** `aa9fc34212bc29c361513d088a2752cb6812ee35` (which was `main`
on 2026-08-25 — `main` has advanced since, and that is expected)
**Recorded:** 2026-08-25
**Author:** Kalab Kebede

Every number and status below was produced from that SHA, on images rebuilt from
it, in the run described in [§3](#3-the-run-this-document-records). It is not a
summary of earlier demonstrations, and it does not reuse figures from the two
existing `docs/presentations/*-three-slides.md` files — those are historical and
remain so.

To reproduce the figures, check out the pinned SHA ([§2, Mode A](#mode-a--pinned-replay)).
To verify a later `main`, use Mode B and re-record the figures rather than quoting
these.

---

## 1. Why this document exists in this shape

This repository has repeatedly produced convincing-looking evidence from a stale
Docker image: a change that appeared to work because the running container did not
contain it, and a change that appeared to survive a mutation test for the same
reason. So the rebuild is written out as a procedure with a verification step
rather than described as "rebuild first", and the demo has two **behavioural**
stale-image checks that fail loudly if the wrong image is running.

The same discipline applies to the status table. "Real" is a claim about what a
component actually did in the recorded run, not about what it is capable of.

---

## 2. Rebuild procedure

**This document has two modes, and mixing them is how the procedure below stopped
working the moment it was written.** `aa9fc34` was `main` on 2026-08-25. `main`
advances; this file does not. So:

* **Mode A — replay the recorded run.** Check out the pinned SHA. The figures in
  [§3](#3-the-run-this-document-records) are reproducible only here.
* **Mode B — verify a later `main`.** Check out `main`. The structural and
  behavioural checks below still apply, because they test features rather than a
  commit, but the run figures will differ and must be re-recorded rather than
  quoted from this file.

Do not skip the verification step; its whole purpose is to catch the failure this
section exists for.

### Mode A — pinned replay

```bash
git checkout aa9fc34212bc29c361513d088a2752cb6812ee35
git rev-parse HEAD          # aa9fc34212bc29c361513d088a2752cb6812ee35
```

### Mode B — current main

```bash
git checkout main
git pull --ff-only origin main
git rev-parse HEAD          # will NOT be aa9fc34 -- that is expected, not a failure
```

### Then, in either mode

```bash

# Visible build. Do not suppress this output -- a build that fails quietly is how
# a stale image survives.
docker compose build gateway loan-assistant decision-service \
                     origination-service servicing-service payment-service frontend

docker compose up -d
```

**Verification — the images actually contain this work.** These greps test for
files that exist because of #93, #94 and #95, so they hold on any `main` at or
after those merges, not only on the pinned SHA:

```bash
docker exec meridian-lending-gateway-1             test -f /app/app/agent_trace.py       # PR #93
docker exec meridian-lending-decision-service-1    test -f /app/app/tracing.py           # PR #94
docker exec meridian-lending-origination-service-1 test -f /app/app/tracing.py           # PR #94
docker exec meridian-lending-loan-assistant-1 \
  grep -c "policy_chat stage=policy_chat_request" /app/app/policy_chat.py                # PR #95, expect 4
```

All four verified present on the recorded run.

**Behavioural stale-frontend checks** (these fail if an old frontend image is
serving). Bring the stack up **with the E2E overlay** first — see the box below:

```bash
docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --build

cd frontend
DATABASE_URL=... npx playwright test e2e/staff-balance-label.spec.ts \
                                    e2e/appbar-layout.spec.ts --workers=1
```

The staff loan page's top card must read **"Current principal balance"**, and the
admin header must hold one row at 1366×768.

> **Two separate harness requirements, and both are needed.**
>
> **`docker-compose.e2e.yml`** raises the gateway's rate limit for the browser
> suite. The default stack ships the real limit — 120 requests per 60 seconds per
> client IP — and a dozen browser journeys from one IP trip it. When that happens
> `signInAsStaff` never leaves `/login`, and the failure surfaces as a URL
> assertion on whichever spec drew the short straw — indistinguishable from the
> stale-image failure this section exists to catch. The overlay is a separate file
> so the raise cannot leak into the demo stack.
>
> **`--workers=1`** is still required on top of it, for a different reason: the
> suite shares one database with no per-spec isolation (RF-24), and parallel
> workers produce `ECONNRESET`. The overlay does not fix that and is not a
> substitute for it.
>
> Recorded plainly because it cost this engagement real time: every rate-limit
> failure diagnosed during this work was avoidable, and the overlay that avoids it
> was already in the repository with the reason written in its own header comment.

---

## 3. The run this document records

One synthetic application, through the authenticated gateway, against real
Bedrock. Application **7289** (seeded synthetic data — not a real person).

| Observation | Value |
|---|---|
| main SHA | `aa9fc34212bc29c361513d088a2752cb6812ee35` |
| Route | `POST /assistant/applications/7289/summary` via gateway, staff session (`underwriter`) |
| Provider | AWS Bedrock, `langchain_aws` Converse API |
| Model | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| Region | `us-east-1` |
| **Model turns** | **2** |
| **Runtime tool calls** | **2**, both `search_underwriting_policy` |
| Policy evidence | `status=hit`, 3 chunks retrieved per call |
| Tool gate | `agent accepted stage=tool_gate tool_calls=2 policy_evidence=hit` |
| Framework tracing | suppressed (`agent tracing suppressed stage=privacy_interim`) |
| Result | HTTP 200, 7 response fields, `flags: []` |
| Macro signal | live `GET https://api.bls.gov/.../LNS14000000` → `HTTP 200`; value 4.1%, period July 2026. The provider caches and fails open — see the macro row in [§4](#4-real--fixture--fallback) |

Credentials are not recorded here, and none appear in any log line quoted above.

**Do not say "one Bedrock call."** The runtime made **two** model turns and **two**
tool executions in this run, and the count varies per application — an earlier run
on the same build made four turns and three tool calls. The claim to make is that
every turn is bounded by a step budget and a provider attempt limit, not that there
is exactly one.

**The gateway trace root was verified live**, not inferred from the code:

```
is_enabled(): True
headers minted: ['baggage', 'langsmith-trace']
metadata: {'stage': 'gateway_entry', 'service': 'gateway', 'role': 'underwriter',
           'route_class': 'agent_summary', 'tracing_mode': 'privacy_safe_categorical',
           'schema_version': '1'}
```

---

## 4. REAL / FIXTURE / FALLBACK

| Component | Status | What that means here |
|---|---|---|
| **Application data** | **FIXTURE** | Seeded synthetic applicants and loans (`db/init/002_seed.sql`). Synthetic data is not real customer data, and no claim about production behaviour follows from it. |
| **Policy corpus** | **REAL** | The actual policy documents committed in this repository, with content-hash versions. Real documents, not a stub corpus. |
| **Policy retrieval** | **REAL** | Genuine retrieval over that corpus — embedding plus IDF scoring, returning `status=hit` with 3 chunks and citable `chunk_id`s. Real retrieval over a local corpus is still real retrieval. |
| **LangChain agent** | **REAL** | LangChain v1 `create_agent` runtime. The model decides to call the tool, the runtime executes it, and a real `ToolMessage` is required — there is no app-side `tool_called = True`. |
| **Bedrock model** | **REAL** | `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us-east-1`, 2 Converse turns in the recorded run. |
| **LangSmith** | **REAL** | Project `2463-fde`. The gateway mints a `gateway_entry` root after authorisation; the agent path emits a categorical trace beneath it. Framework tracing is suppressed everywhere. |
| **Macro source** | **REAL, cached, fails open** | A live HTTPS request to `api.bls.gov` was observed (`GET /publicAPI/v1/timeseries/data/LNS14000000` -> `HTTP/1.1 200 OK`), and the value read back was **4.1%, period July 2026**, series `LNS14000000`. Two qualifications a bare "REAL" would hide: the provider **caches**, so an individual summary may be served from cache rather than a fresh fetch, and it **fails open** -- an unreachable provider yields no signal rather than an error, so an absent signal is not evidence of a failed call. The test suite deliberately blocks real BLS traffic (`services/loan-assistant/tests/conftest.py`), so no test result speaks to this row. |
| **Payment processor** | **FIXTURE** | Stub processor; `PROCESSOR_API_KEY` is unset and tokens are mock (`tok_mock_…`). A stub processor is not a processor. |
| **Credit bureau** | **FIXTURE** | `EXPERIAN_KEY` is unset, so a deterministic development stub score is used. Outside a development or test environment the service **refuses to decide** rather than scoring from a fake — see `services/decision-service/app/decision.py`. |
| **AI scorer** | **FIXTURE** | `AI_MODEL_API_KEY` unset → deterministic stub score. |

---

## 5. Live demo sequence (7–10 minutes)

1. **The app shell.** Sign in as admin. "These are the workflows this role is
   allowed to navigate." Do not discuss CSS.
2. **Underwriting.** Open the queue, pick a synthetic application.
3. **Generate the AI summary.** Say: *the request is authenticated at the gateway
   before the agent runs.*
4. **The trace.** In client language: Gateway → Agent → Policy lookup → Bedrock →
   Safety checks → Result. Stay out of LangSmith internals unless asked.
5. **Policy provenance.** Show the document, its version and the citation. Say:
   *if the agent retrieves no policy evidence, it refuses the summary.*
6. **One refusal.** Show either the injection block or the missing-evidence
   refusal.
7. **Maker–checker.** Staff A proposes — **no money moves**. Self-approval is
   refused. Staff B approves, and that writes one ledger movement.
8. **Servicing.** Original principal, **current principal balance**, outstanding
   fees, Account Activity.
9. **Payment.** Captured → receipt with Fees → Interest → Principal from the
   ledger. Pending → claims nothing. Failed → says declined, not pending.
10. **Reconciliation.** `processor_ref` → transaction-level comparison → break and
    review evidence.
11. **Open decisions.** Show the late-fee reassessment question as genuinely open.
    Do not present every box as green.

---

## 6. Client questions, with the distinctions kept

**Should duplicate-looking payments automatically break or reverse?**
No. The authorised behaviour is to flag for human review. The system does not
conclude a duplicate, does not reverse or refund, does not move money from a review
signal, and does not raise an automatic reconciliation break from one.

**Where should payment allocation be visible?**
Engineering now supports a ledger-backed Payment History and a captured-payment
receipt. We must **not** claim the client formally selected a final placement
preference — no such decision is on record.

**Is late-fee compounding intended?**
Still open. The amount formula is fixed: the lesser of $35 or 5% of arrears (5%
below $700, $35 at or above $700). What is unresolved is repeated reassessment and
whether previously assessed fees belong in the next base.

**Is the card-data path PCI certified?**
No. Repository-level handling is traced and tested with named boundaries. This is a
synthetic training demonstration, not PCI certification.

**Is the AI trace the same as the Week 7 payment trace?**
No, and they are evidenced independently.
*Payment trace:* payment-service → processor → servicing → ledger, on a shared
correlation identifier.
*Agent trace:* authenticated gateway → LangChain agent → policy retrieval →
Bedrock → deterministic validation → outcome.

---

## 7. Known limitations

| Limitation | Effect | Owner |
|---|---|---|
| **RF-24** — browser suite shares one database, no per-spec isolation | Parallel runs produce `ECONNRESET`. Run `--workers=1`; the E2E compose overlay does **not** address this. | Engineering |
| **Gateway rate limit in tests** — 120 req/60s per IP | Back-to-back or parallel suite runs return HTTP 429. `signInAsStaff` then never leaves `/login` and the failure presents as a URL assertion, or as "element not found" when the staff section never renders — moving between tests on each run. **Mitigation already exists:** bring the stack up with `docker-compose.e2e.yml` ([§2](#2-rebuild-procedure)). Every occurrence during this work was diagnosed from the gateway log rather than retried past, and every one of them was avoidable by using that overlay. | Engineering |
| **`appbar-layout.spec.ts` focus test is order-dependent** | It passes alone and failed once inside a batch. Chromium only applies `:focus-visible` when it judges the last interaction to be keyboard-driven, and the test focuses programmatically. It is my test and it is not yet reliable; the fix is to drive focus with the keyboard. | Kalab (open) |
| **Synthetic data throughout** | No conclusion about production behaviour follows from a seeded portfolio. | — |
| **Stub processor and stub bureau** | Payment capture and credit scoring are not exercised against real providers. | — |
| **RF-26** — tests hand-write partial `applications` schemas | Divergence risk between test schemas and migrations. | Engineering |
| **D20** — static PAN-reader SQL scanner has known limits | Scanner cannot see dynamically composed SQL. | Engineering |

---

## 8. Open client decisions — do not answer these for the client

| Item | Status |
|---|---|
| **D23** late-fee reassessment / compounding | **OPEN CLIENT DECISION** — cadence, repeat assessment, and whether earlier fees enter the next 5% base |
| **D24** fairness fixture | **CLIENT-BLOCKED** — no approved protected-class or proxy dataset exists; one must not be manufactured |
| **RF-25** manual DTI entry | **OPEN CLIENT DECISION** — whether staff may enter it, and what evidence authorises it |
| **D7** external alert destination | **OPS-BLOCKED / CLIENT-PROHIBITED before freeze** — in-app visibility exists; email, Slack, PagerDuty, SMS and webhooks were ruled out |
| **Week 9** KYC/AML/UBO/sanctions | **COMPLIANCE- and VENDOR-BLOCKED** |
| **Week 10** retention-aware redaction | Plan only, pending go-ahead |

---

## 9. Evidence references

Each path below exists at this SHA.

| Claim | Test |
|---|---|
| Trace starts at the authenticated gateway; caller cannot choose the context | `services/gateway/tests/test_the_agent_trace_starts_here.py` |
| Agent spans join the gateway root; no prohibited value on the wire | `services/loan-assistant/tests/test_the_trace_joins_the_gateway.py` |
| The agent trace carries categorical metadata only | `services/loan-assistant/tests/test_trace_is_privacy_safe.py` |
| Framework tracing emits zero bytes on the agent path | `services/loan-assistant/tests/test_agent_tracing_is_suppressed.py` |
| Policy Chat retains neither the question nor a raw trace | `services/loan-assistant/tests/test_policy_chat_retains_nothing.py` |
| Decision graph transmits nothing | `services/decision-service/tests/test_the_decision_graph_transmits_nothing.py` |
| Auto-offer graph transmits nothing | `services/origination-service/tests/test_the_auto_offer_graph_transmits_nothing.py` |
| Runtime tool evidence is required, not simulated | `services/loan-assistant/tests/test_agent_tool_gate.py` |
| Staff card names the balance it shows | `frontend/e2e/staff-balance-label.spec.ts` |
| Header holds one row at presentation widths | `frontend/e2e/appbar-layout.spec.ts` |
| Late fee follows the published schedule | `services/servicing-service/tests/test_late_fee_follows_the_published_schedule.py` |
| Reconciliation is a control, matched at transaction level | `services/servicing-service/tests/test_reconciliation_is_a_control.py`, `services/servicing-service/tests/test_reconciliation_matches_transactions.py` |
| Review signals move no money | `services/payment-service/tests/test_review_signals_do_not_touch_money.py` |
| No card data on either schema path | `db/tests/test_no_card_data_on_either_schema_path.py` |

---

## 10. Claims we must NOT make

- Not "PCI compliant" or "PCI certified".
- Not "one Bedrock call" — the run made two model turns, and the count varies.
- Not "all traces are PII-scrubbed" — three different controls are in play:
  suppression, categorical emission, and no trace at all.
- Not "tested against a real credit bureau" or "a real payment processor" — both
  are stubs here.
- Not "the client chose the payment-allocation placement" — no such decision exists.
- Not "late-fee compounding is settled" — it is open.
- Not "fairness has been evaluated" — no approved dataset exists (D24).
- Not "the E2E suite is green in parallel" — it requires `--workers=1`, and the
  browser step additionally requires the `docker-compose.e2e.yml` overlay.
- Not "the macro signal is fetched fresh for every summary" — the provider caches,
  and it fails open.
