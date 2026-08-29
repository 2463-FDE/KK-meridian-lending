# Agentic underwriting — client handoff

**Sections 1-3 recorded at main:** `aa9fc34212bc29c361513d088a2752cb6812ee35`
(which was `main` on 2026-08-25)
**Section 3a re-recorded at main:** `21a8a139eb6c04b70d5c5264db25ba545ea3edfb`
(2026-08-27, the commit demonstrated)

Two SHAs on purpose. The header used to carry one and say "`main` has advanced
since, and that is expected", which was true and stopped being sufficient once a
second run existed. Naming the range each section covers is better than picking
one and leaving the other section sitting under a header that does not describe
it.
**Recorded:** 2026-08-25
**Author:** Kalab Kebede

Every number and status in **Sections 1-3** was produced from the first SHA, on
images rebuilt from it, in the run described in
[§3](#3-the-run-this-document-records). **[§3a](#3a-mode-b-re-record-at-the-demonstrated-commit--2026-08-27)
carries its own SHA, its own run and its own committed artifacts**, and none of
its figures are copied from §3 — the two runs disagree, which is why both are
here. Neither reuses figures from the two existing
`docs/presentations/*-three-slides.md` files; those are historical and remain so.

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
> **Use `127.0.0.1` in `DATABASE_URL`, not `localhost`** — on the workstation this
> was recorded on, connections over the IPv6 loopback dropped intermittently and
> surfaced as an apparent database fault. Measurement, environment and the test
> that tells this apart from RF-24 are in [§7](#7-known-limitations); the rule here
> is just the host to use.
>
> Recorded plainly because it cost this engagement real time: every rate-limit
> failure diagnosed during this work was avoidable, and the overlay that avoids it
> was already in the repository with the reason written in its own header comment.

---

## 3. The run this document records

One synthetic application, through the authenticated gateway, against real
Bedrock. Application **7289** (seeded synthetic data — not a real person).

> **Superseded for the demonstrated commit — see [§3a](#3a-mode-b-re-record-at-the-demonstrated-commit--2026-08-27).**
> The figures below are a correct record of the run at `aa9fc34` and are not
> edited. They are not the figures for the commit being demonstrated: that run
> made **3** runtime tool calls where this one made 2.

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

## 3a. Mode B re-record at the demonstrated commit — 2026-08-27

Section 3 above records a run at `aa9fc34` and stays exactly as it was. This
section is a **second, independent run** at the commit that will actually be
demonstrated. Neither replaces the other, and no number is copied between them.

**Why this section exists.** A direction check on 2026-08-27 made the point that
the recorded proof was pinned to a commit the branch had moved past. Section 3
was never dishonest — it names its SHA and offers Mode A/Mode B — but it was
*under-strength* for the commit being shown. This closes that gap by re-running
rather than by re-labelling.

### SHA discipline

| | |
|---|---|
| **Runtime source SHA** | `21a8a139eb6c04b70d5c5264db25ba545ea3edfb` — the tree the images were built from and the run executed against |
| **Evidence-packaging commit** | this commit, which adds this section and the artifacts under [`docs/evidence/2026-08-27-demo-proof/`](../evidence/2026-08-27-demo-proof/) |

Those differ, and saying so is the point rather than a caveat.

**What is proven about image ↔ tree correspondence, precisely.** An earlier
draft of this section claimed the running image *was* the demonstrated tree on
the strength of one file's hash. Review was right that the conclusion outran the
evidence: a single module says nothing about the other fourteen, the
dependency manifest, or the Dockerfile. The proof is widened rather than the
sentence softened.

| Check | Host | Container | Match |
|---|---|---|---|
| `app/` tree hash, all 15 `.py` files, name + bytes | `7e072ba60820d8aa4fdca0a4` | `7e072ba60820d8aa4fdca0a4` | ✅ |
| `requirements.txt` sha256 | `89c32931e79d8a32f6149c9b` | `89c32931e79d8a32f6149c9b` | ✅ |
| `app/main.py` sha256 | `230de01efb3b070e` | `230de01efb3b070e` | ✅ |

Running image digests at capture time:

```
loan-assistant  sha256:bf88658f3bc4c90387683b8611ce23bb932c336fdb0fbfebf772590061175799
gateway         sha256:c3273950f447ee5a95e76cdb62c27b0d5b54e7a542201f801d4894cf997c03c6
```

**Every file that changed between the runtime source SHA and the demo head**, annotated:

```
$ git diff --name-only 21a8a13 e69d313
README.md                                             outside every build context
db/tests/test_public_docs_match_shipped_behaviour.py  outside every build context
docs/ROADMAP.md                                       outside every build context
```

Build contexts are `./services/*` and `./frontend` (`docker-compose.yml`). None
of the three changed files is inside one, so no image could differ. That is the
claim — *the assistant service's source tree and dependency manifest in the
running image are byte-identical to the runtime source SHA, and nothing between
that SHA and the demo head could change an image* — and it is the claim the
table above supports. It is not a claim that every container in the stack was
rebuilt from the demo head; the two services this section exercises were
checked, and the others were not.

### Build freshness, checked before recording

A `docker compose up` earlier that day **exited 0 while every image build had
failed** on a corrupted BuildKit snapshot. A build that fails quietly is how a
stale image gets demonstrated, so freshness was proven three ways rather than
inferred from an exit code:

1. visible rebuild of the normal demo stack, zero `failed to solve` lines;
2. `app/main.py` sha256 in the container matched the host byte-for-byte;
3. a behavioural check — the route signature carries the `X-Internal-Token`
   parameter added by #108, which did not exist in the previous images.

### The run

Synthetic seeded application **7354**. Not a real person.

| Observation | Value |
|---|---|
| Runtime source SHA | `21a8a13` |
| Captured | 2026-08-27T15:36:21Z |
| Route | `POST /assistant/applications/7354/summary`, via gateway, staff session (`underwriter`) |
| Provider | `bedrock` |
| Model family | `claude` |
| Region | `us-east-1` |
| **Model turns** | **2** |
| **Runtime tool calls** | **3**, `search_underwriting_policy` |
| Policy evidence | `hit` |
| Documents | `fee_schedule.md`, `underwriting_guidelines.md` |
| Document versions | `sha256:1972040e71e5`, `sha256:ead76c59e419` |
| Citations | 6 chunk ids, each carrying its document's version hash |
| Validators run | `dti_claim`, `macro_contradiction`, `risk_classification` |
| Outcome | `summary_returned`, HTTP 200, 13.21s |
| Step budget | 12 |
| Tracing mode | `privacy_safe_categorical` |

**This run made 3 tool calls. The run at `aa9fc34` made 2.** That difference is
the reason this section exists rather than a note adjusting the old one. The
durable claim is that model and tool execution are *bounded and runtime-observed*
— the exact count is a property of a run, not of the system, and quoting a fixed
number as though it were a guarantee is a claim the next run can falsify.

### Privacy-safe trace artifact, captured from this same run

Captured at a **local ingest sink**: `LANGSMITH_ENDPOINT` was pointed at a
listener on the Compose network, so the exact bytes the exporter posted could be
read, and nothing had to leave the machine or be fetched with a credential.

**This is a pre-egress capture, and the claim is bounded to that.** It shows the
payload the exporter produces and posts — which is what the allowlist governs and
therefore what the privacy claim is about. It does **not** show hosted delivery:
credentialed auth, retries, endpoint allowlisting, or what the hosted service
does with the same payload after ingest are outside this capture and outside this
exercise. A reader should not take it as end-to-end equivalence with the
production tracing path.

**20,992 bytes across three payloads, all three committed** — the figure is
`wc -c` of the files in
[`docs/evidence/2026-08-27-demo-proof/`](../evidence/2026-08-27-demo-proof/),
not a note taken at the time:

| File | Bytes | Carries |
|---|---|---|
| `trace-01-gateway-root.multipart.bin` | 1,812 | `gateway_entry` — the run opened at the authenticated entry point |
| `trace-02-gateway-close.multipart.bin` | 1,737 | `gateway_entry` close, with `http_status` |
| `trace-03-agent-spans.multipart.bin` | 17,443 | `request`, `policy_retrieval`, `model`, `agent_run`, `validation`, `outcome` |

Stage chain, observed end to end across that set:

`gateway_entry → request → policy_retrieval → model → agent_run → validation → outcome`

The first draft committed only the third file while claiming the chain and the
20,992 total. Review caught it, and the catch is the point of committing
artifacts at all: the first thing a reader checks disagreed with the document.
`gateway_entry` lives in the gateway's own payloads, so a claim that the trace
starts at authentication is only inspectable if those are committed too.

**Fields retained** — categorical and provenance only: `stage`, `service`,
`status`, `outcome`, `role`, `route_class`, `provider`, `model_family`, `region`,
`model_turns`, `tool_name`, `tool_calls`, `evidence_status`, `documents`,
`document_versions`, `citations`, `validators_run`, `refusal_class`,
`http_status`, `duration_ms`, `step_budget`, `provider_attempt_limit`,
`tracing_mode`, `schema_version`.

**Inspected for, and not observed in this captured artifact:** the subject's
name, SSN, DOB, address, email and loan amount (compared against the actual
database row); the application id; any `inputs` / `outputs` / `messages` /
`prompt` / `response` / `content` key; SSN- and PAN-shaped strings; bearer
tokens or API keys; credential environment names; `income` / `dti` keys; raw
provider or model errors.

**What that supports, and what it does not.** The correct claim is: *this
captured agent trace contains only the allowlisted categorical and provenance
fields, and framework content tracing is suppressed on these paths.* It is not
evidence that all traces everywhere are scrubbed. Trace fidelity here is
deliberately bounded by the allowlist, not by the framework's default
full-fidelity capture — the framework would emit far more, and was measured
doing so before it was turned off.

### Refusal 1 — agent disabled

Exercised in an **isolated container**; the demo stack was not modified.

| | |
|---|---|
| Condition | `AGENT_ENABLED=false` |
| Expected | refusal, and no direct-model fallback |
| Actual | **HTTP 502** |
| Body | `AGENT_ENABLED is off -- the underwriting summary runs through the agent runtime and will not fall back to a direct model call.` |

The refusal names the guarantee: the summary path does not degrade to a direct
call when the agent is unavailable. A second, different variant exists —
missing agent dependencies or a non-Bedrock provider raise `AgentUnavailable`
and map to **503** — and the two are not interchangeable.

### Refusal 2 — retrieval miss

| | |
|---|---|
| Condition | agent runs; policy corpus present but carrying no matching content |
| Expected | tool executes, no usable evidence, refusal |
| Actual | **HTTP 502** |
| Body | `the agent called search_underwriting_policy but retrieval returned 'miss'; refusing a summary with no policy behind it` |
| Service log | `policy tool: status=miss hits=0 k=3` |
| Refusal class | `PolicyEvidenceMissing` |

The log line is the load-bearing part: the tool **ran**. This is a refusal on
absent evidence, not a refusal to call the tool — a distinction a status code
alone would not carry.

**Why both refusals are 502 and not two different codes.** Both are upstream
failures from the gateway's point of view: the summary could not be produced, and
the reason is not the caller's. They are distinguished by `refusal_class` in the
trace and by the response body, not by the status —
`LLMResponseError` for the disabled agent, `PolicyEvidenceMissing` for the
retrieval miss. A third variant does differ: missing agent dependencies or a
non-Bedrock provider raise `AgentUnavailable`, which maps to **503**. The mapping
lives in `services/loan-assistant/app/main.py` and is asserted by
`services/loan-assistant/tests/test_agent_failures_reach_the_route.py`.

### Also observed in the same run

A borrower session on the staff summary route was refused **403** at the gateway,
before the request reached the agent.

### Test evidence, run where it actually runs

`services/loan-assistant` — **463 passed, 0 skipped**, executed inside the
service image with the repository mounted.

Stated because the obvious way to run it is wrong: on a host without
`langchain`, nine agent and tracing tests **skip silently** and the suite still
reports green. A green host run is not evidence for this service.

### Resolved dependency set

Captured from the demo images at the runtime source SHA. Recorded as evidence;
nothing was upgraded for the demo.

| Package | Version |
|---|---|
| `langchain` | 1.3.16 |
| `langchain-core` | 1.6.0 |
| `langchain-aws` | 1.7.3 |
| `boto3` / `botocore` | 1.43.81 |
| `fastapi` | 0.141.1 |
| `pydantic` | 2.13.4 |
| `httpx` | 0.28.1 |

58 packages in `loan-assistant`, 40 in `gateway`. The transitive closure is not
pinned by a lock file; `docs/DEBT.md` **SEC-11** tracks that, and a CI audit
count is not evidence of a reachable exploit.

### 3a.1 Appendix — artifacts and the commands that produced them

Review's point was that §3a stated numbers a reader cannot check, which is the
one property an evidence section has to have. The artifacts are committed and
the commands are here. Where a number below disagrees with a re-run, the re-run
is the better evidence.

Artifacts: [`docs/evidence/2026-08-27-demo-proof/`](../evidence/2026-08-27-demo-proof/)

| File | Bytes | What it is |
|---|---|---|
| `trace-01-gateway-root.multipart.bin` | 1,812 | gateway root span, `gateway_entry` |
| `trace-02-gateway-close.multipart.bin` | 1,737 | gateway root close, `http_status` |
| `trace-03-agent-spans.multipart.bin` | 17,443 | every assistant-side stage |
| `pip-freeze-loan-assistant.txt` | — | resolved packages in the running assistant image |
| `pip-freeze-gateway.txt` | — | resolved packages in the running gateway image |

```bash
wc -c docs/evidence/2026-08-27-demo-proof/*.bin   # 1812 + 1737 + 17443 = 20992
```

The payloads are committed **because** they are the artifact the review asked
for, and re-scanned immediately before entering git — no content-bearing key, no
SSN or PAN shape, no credential, no application id.

**`.gitattributes` marks them `-text`, and that is load-bearing.** The first
attempt committed one payload without it: 17,443 bytes on disk became 17,232 in
the blob, because `core.autocrlf=true` rewrote the line endings. Every byte
figure in this section would have been wrong for anyone who cloned, and the
sensitive-field search would have run over different bytes than the ones
captured. Same rule and same reason as the client governance package.

**Image ↔ tree correspondence** (§3a "SHA discipline"):

```bash
docker compose ps -q loan-assistant | xargs docker inspect --format '{{.Image}}'

# tree hash, run once on the host and once in the container
python -c "import hashlib,pathlib; \
  fs=sorted(p for p in pathlib.Path('services/loan-assistant/app').rglob('*.py') \
            if '__pycache__' not in p.parts); \
  h=hashlib.sha256(); [ (h.update(p.name.encode()), h.update(p.read_bytes())) for p in fs ]; \
  print(h.hexdigest()[:24], len(fs))"

# container side, verbatim -- the same walk over /app/app
docker compose exec -T loan-assistant python -c "import hashlib,pathlib;   fs=sorted(p for p in pathlib.Path('/app/app').rglob('*.py')             if '__pycache__' not in p.parts);   h=hashlib.sha256(); [ (h.update(p.name.encode()), h.update(p.read_bytes())) for p in fs ];   print(h.hexdigest()[:24], len(fs))"

# requirements.txt, both sides
sha256sum services/loan-assistant/requirements.txt
docker compose exec -T loan-assistant sha256sum /app/requirements.txt

git diff --name-only 21a8a13 e69d313
```

**The run** — from inside the Compose network, synthetic seeded application:

```bash
docker compose exec gateway python - <<'EOF'
import httpx
tok = httpx.post("http://gateway:8000/auth/login",
                 json={"username": "underwriter", "password": "password"}).json()["token"]
r = httpx.post("http://gateway:8000/assistant/applications/7354/summary",
               headers={"Authorization": f"Bearer {tok}"}, timeout=240)
print(r.status_code, sorted(r.json()))
EOF
```

**Trace capture** — a listener on the Compose network, with
`LANGSMITH_ENDPOINT` pointed at it for `loan-assistant` and `gateway` only, then
restored to the shipped default afterwards. Every categorical field quoted in
§3a is read directly from the committed payload; the stage chain is the set of
`stage` values it contains.

**The negative search** — run over the captured bytes. The subject's values were
read from the database row for application 7354 and searched for literally;
the remaining classes were searched as patterns:

```
name · ssn · dob · address · email · loan_amount        (literal, from the DB row)
\b7354\b                                                 (application id)
"(inputs|outputs|messages|prompt|response|completion|content|text)"\s*:
\b\d{3}-\d{2}-\d{4}\b                                    (SSN shape)
\b(?:\d[ -]?){13,19}\b                                   (PAN shape)
(ABSK|lsv2_|Bearer\s+[A-Za-z0-9._-]{20,})                (credentials)
(AWS_BEARER|SecretAccessKey|aws_secret)                  (credential env names)
"(income|dti|debt_to_income|annual_income)"\s*:
(Traceback|botocore|ClientError|ValidationException)     (raw provider errors)
```

Fourteen classes, none observed. Re-runnable against the committed file.

**Tests** — inside the service image with the repository mounted, which is the
part that matters:

```bash
docker run --rm --network meridian-lending_default \
  -v "$PWD:/repo:ro" -e ENVIRONMENT=test -e MACRO_ENABLED=0 \
  -e INTERNAL_SERVICE_TOKEN=test-internal-token \
  -w /repo/services/loan-assistant meridian-lending-loan-assistant:latest \
  sh -c "pip install -q pytest pytest-asyncio; python -m pytest tests -q -rs"
# 463 passed, 0 skipped
```

On a host without `langchain` the same suite reports green with nine agent and
tracing tests skipped. Run it on the host and the number is meaningless.

**Refusals** — each in a throwaway container so the demo stack was never
modified. Both need `POLICIES_DIR` set, which `docker-compose.yml` supplies and
`--env-file .env` does not:

```bash
# agent disabled
docker run --rm -d --name la-off --network meridian-lending_default \
  --env-file .env -e AGENT_ENABLED=false -e POLICIES_DIR=/app/policies \
  meridian-lending-loan-assistant:latest

# retrieval miss: allowlisted filenames, content that matches nothing
docker run --rm -d --name la-miss --network meridian-lending_default \
  --env-file .env -e POLICIES_DIR=/app/policies \
  -v "$PWD/.tmp/miss-policies:/app/policies:ro" \
  meridian-lending-loan-assistant:latest
```

**Dependency snapshot**:

```bash
docker compose exec -T loan-assistant python -m pip freeze
docker compose exec -T gateway python -m pip freeze
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
Engineering is done: a ledger-backed Payment History and an immediate
captured-payment receipt both exist and work. The **product decision is open** —
the client has not chosen history only, receipt only, both, or something else, and
we must not claim they did. Neither surface is removed while that is unanswered.

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
| **IPv6 loopback dropped connections — observed on one workstation, not asserted as a general rule** | `localhost` resolves to `::1` first there, and connects over it dropped intermittently: **1 failure in 12** sequential attempts versus **0 in 12** over `127.0.0.1`. Twelve samples cannot support a rate, so treat this as a direction, not a percentage. It surfaces as `psycopg2.OperationalError: ... server closed the connection unexpectedly`, which reads as a database fault. Same commit, changing only the host: `localhost` → 1008 passed / **4 failed** (reproduced twice); `127.0.0.1` → **1012 passed / 0 failed**. Ruled out: connection exhaustion (12 of 100 in use) and a Postgres restart (`restarts=0`). **Measured on:** Windows 11 10.0.26200, Docker Desktop engine 28.5.1, `postgres:16-alpine` published on `0.0.0.0:5432` **and** `[::]:5432`, psycopg2 2.9.12, Python 3.14.6; connect probe was `psycopg2.connect(host=…, port=5432, connect_timeout=3)` ×12 per host. A different Windows or Docker build may not reproduce it — check the address in the error text before applying the workaround. | Environment |
| **Telling the IPv6 fault apart from RF-24** — they hit the same file | Both make `test_offer_creation_concurrency.py` fail, for different reasons, which is the confusion most likely to repeat. **IPv6 fault:** happens when a Python/DB connection is *opened*, the error text names the address `(::1)`, and it disappears when `DATABASE_URL`'s host is `127.0.0.1`. **RF-24:** happens under *parallel browser workers* sharing one database, surfaces as `ECONNRESET`, and disappears with `--workers=1`. RF-24 is real and unfixed — it was simply not the cause of these particular failures, which I initially attributed to it and got wrong. | Engineering / Environment |
| **RF-24** — browser suite shares one database, no per-spec isolation | Parallel runs produce `ECONNRESET`. Run `--workers=1`; the E2E compose overlay does **not** address this. | Engineering |
| **Browser specs time out when another heavy suite runs concurrently** | Running the browser suite while `db/tests` was running produced 30s `page.goto`/`locator.fill` timeouts in **8 of 25** targeted specs; **all 25 passed** on an idle machine. Run suites sequentially. The rule is narrow on purpose: a timeout that reproduces **only** while another heavy suite is running is not a product finding — but it is only cleared once it has been **re-run idle and passed**. A timeout that also reproduces idle is a finding, and may be a performance regression. | Environment |
| **Gateway rate limit in tests** — 120 req/60s per IP | Back-to-back or parallel suite runs return HTTP 429. `signInAsStaff` then never leaves `/login` and the failure presents as a URL assertion, or as "element not found" when the staff section never renders — moving between tests on each run. **Mitigation already exists:** bring the stack up with `docker-compose.e2e.yml` ([§2](#2-rebuild-procedure)). Every occurrence during this work was diagnosed from the gateway log rather than retried past, and every one of them was avoidable by using that overlay. | Engineering |
| **`appbar-layout.spec.ts` focus test is order-dependent** | It passes alone and failed once inside a batch. Chromium only applies `:focus-visible` when it judges the last interaction to be keyboard-driven, and the test focuses programmatically. It is my test and it is not yet reliable; the fix is to drive focus with the keyboard. | Kalab (open) |
| **Synthetic data throughout** | No conclusion about production behaviour follows from a seeded portfolio. | — |
| **Stub processor and stub bureau** | Payment capture and credit scoring are not exercised against real providers. | — |
| **RF-26** — tests hand-write partial `applications` schemas | Divergence risk between test schemas and migrations. | Engineering |
| **D20** — static PAN-reader SQL scanner has known limits | Scanner cannot see dynamically composed SQL. | Engineering |

---

## 8. What is still open, and who owns it

Split into two tables on purpose. Collapsing them invites the reading this
section exists to prevent: that the client owes a decision they have already
given.

### 8a. Decisions the client HAS made — implemented, not open

| Item | The decision | Where it lives |
|---|---|---|
| Fairness data policy | No real protected-class collection; no approved proxy; ZIP/ZIP3 prohibited as one; synthetic labels only inside an isolated offline fixture; aggregate output only; training only | Recorded at D24; the runtime ZIP3 screen was retired the same day |
| Vendor governance boundary | The referenced package is **synthetic and training-only** — it is *not* vendor-issued, not production validation, and not authority for live vendor calls. Real approved materials must replace it before any non-training use | D24 |
| Duplicate-looking payments | Review signal to a human, never an automatic money action. Exact match on provider transaction id or idempotency key with **no** window; heuristic on loan + amount + source + channel inside a rolling 30 minutes | D22, implemented |
| Where findings go, this phase | The **in-app reconciliation queue**. No email, Slack, PagerDuty, SMS or webhook before the freeze, and no new credentials | D7, implemented |

### 8b. Genuinely open — do not answer these for the client

| Item | Status | Owner |
|---|---|---|
| **D23** late-fee reassessment / compounding | **ANSWERED 2026-08-29 -- RULE DECIDED, NOT YET IMPLEMENTED.** *This row read "OPEN CLIENT DECISION -- may a fee be assessed again, at what cadence, and do previously assessed fees enter the next 5% base" when this document was written, and that was true then.* The answer replaces the rule rather than setting a cadence: at most one fee per missed scheduled installment, after the existing grace period, never reassessed against the same installment, priced at `min($35, 5% x unpaid scheduled P&I for THAT installment)` with all fees excluded from the base. **The code still does the older published comparison priced off the past-due total**, because the decided rule needs installment-level facts this system does not persist -- nothing records which installment a payment satisfied or which installment a fee belongs to. `docs/DEBT.md` D23 carries the traced gate, the exact missing primitive, the smallest addition that would close it, and why no backfill of existing loans could be truthful. Not approximated from `past_due`, deliberately. | Lending Operations (rule) / engineering (data-model expansion) |
| **Payment-allocation placement** | **ENGINEERING DONE / PRODUCT DECISION OPEN** — both a ledger-backed Payment History and an immediate captured-payment receipt exist and work, each with tests cited in [§9](#9-evidence-references). The client has **not** chosen the final placement: history only, receipt only, both, or something else. Neither surface may be removed without direction | Client / product |
| **D24** fairness training package | **CLOSED 2026-08-27 — POLICY ANSWERED (8a) AND PACKAGE RECEIVED.** The client's synthetic training package arrived as an email attachment dated 2026-08-24 and is ingested byte-for-byte at `fixtures/offline_fairness_training/client_package_2026-08-24/`, all 34 checksums verifying. It authorises the isolated offline evaluation and nothing else: the evaluator reports aggregate counts and computes **no fairness verdict**. It is not vendor-issued and establishes no real-world fairness; real approved vendor materials must still replace it before any non-training use. *This row read `POLICY ANSWERED / ARTIFACT PENDING — not present in this repository` until 2026-08-27, which was correct when written.* | Closed — no client action outstanding |
| **RF-25** manual DTI entry | **OPEN CLIENT DECISION** — whether staff may apply DTI manually in a referred review, and what evidence authorises it | Lending Ops / Compliance |
| **D7** external alert delivery, after the freeze | **OPS-BLOCKED + CLIENT-PROHIBITED** — the current phase is decided and built (8a); a *firing* alert with nobody watching still has no human destination | Operations, then client |
| **Week 9** KYC/AML/UBO/sanctions | **COMPLIANCE- / VENDOR- / CLIENT- / OPS-BLOCKED** | Multi-party |
| **Week 10** retention-aware redaction | **PLAN ONLY** — needs a scope separating legally required evidence from identifying data | Pending authorisation |

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
| A captured payment shows its ledger-backed split; pending and failed claim nothing | `frontend/e2e/payment-state-and-receipt.spec.ts` |
| Allocation follows fees, then interest, then principal | `frontend/e2e/payment-allocation.spec.ts`, `frontend/e2e/payment-allocation-view.spec.ts` |
| Payment History reads back each ledger movement once | `frontend/e2e/account-activity.spec.ts`, `services/servicing-service/tests/test_account_activity.py` |
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
- Not "the client has not decided fairness policy" — they decided it on
  2026-08-24; only the artifact is outstanding.
- Not "the synthetic package is vendor-issued documentation" — it is training-only
  material, and real approved documents must replace it before non-training use.
- Not "production-ready", "fully secure", "PCI certified" or "SOC 2 compliant".
  **This is a synthetic training deployment.** Production hardening — password
  storage, session architecture, browser-token handling, perimeter TLS, Redis
  authentication, container runtime identity, the bounded forwarded-role trust
  boundary and dependency governance — is tracked separately under
  `SEC-01`..`SEC-17` in `docs/DEBT.md` and is **not**
  claimed as production-ready. Naming that plainly is what lets the rest of this
  document be believed.
- Not "the macro signal is fetched fresh for every summary" — the provider caches,
  and it fails open.
