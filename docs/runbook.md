# Meridian Lending — Operations Runbook

> In-house ops notes. Sparse — Halcyon left no runbook, so this is what we've pieced
> together. Add to it when you learn something the hard way.

## Local / dev bring-up

```bash
cp .env.example .env     # NOTE: a populated .env is already committed, so this is optional
make up                  # docker compose up -d --build (postgres, redis, services, frontend)
make logs                # tail all services
make ps                  # container status
make down                # stop everything
```

- Portal: http://localhost:3000
- Gateway + OpenAPI docs: http://localhost:8000/docs
- Postgres: localhost:5432 (`meridian` / see `.env`)
- The DB auto-seeds from `db/init/*.sql` on first `up` (fresh volume only).

To re-apply the curated seed without recreating the volume:
```bash
make seed
```

To wipe and re-seed from scratch:
```bash
docker compose down -v && make up
```

## Demo logins

All seeded with password `password`:

| Username | Role | Use |
|----------|------|-----|
| `admin` | admin | full portal |
| `underwriter` | underwriter | decisioning views |
| `csr` | csr | servicing dashboard |
| `maria` | borrower | borrower view (applicant #1) |

## Health checks

The gateway is the only backend port published to the host:

```bash
curl localhost:8000/health     # gateway
```

Every other backend service is on the compose network only, so reach its health
endpoint through the container rather than the host:

```bash
docker compose exec origination-service  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8001/health').read())"
docker compose exec servicing-service    python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8002/health').read())"
docker compose exec kyc-service          python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8003/health').read())"
docker compose exec decision-service     python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8004/health').read())"
docker compose exec disclosure-service   python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8005/health').read())"
docker compose exec payment-service      python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8006/health').read())"
```

Or read them all at once from `docker compose ps` — each service's compose
healthcheck already probes exactly these URLs.

*This block previously listed `curl localhost:8001`–`8006` directly.* Those had
not worked since PR #6 un-published 8001, 8002 and 8004–8006; 8003 followed when
the `kyc-service` bypass was closed. An operator following the old version got
`Connection refused` on six of seven lines and had no way to tell that from a
service being genuinely down — which is the failure a runbook exists to prevent.

Ports 8003–8006 are the four services extracted from the old origination monolith
(ADR 0004). `.env` carries their base URLs as `KYC_URL` / `DECISION_URL` /
`DISCLOSURE_URL` / `PAYMENT_URL` — origination reads these in `app/clients.py`.

## Common tasks

Endpoints are reached through the gateway. After the decomposition, decisioning,
disclosure, KYC, and payments are backed by their own services — origination still
orchestrates the LOS flow and calls them over HTTP.

- **Run a credit decision:** `POST /los/applications/{id}/decision` (origination orchestrates
  → `decision-service`), or hit `decision-service` directly via `/decision/*`.
- **Run a KYC/CIP check:** `/kyc/*` → `kyc-service` (origination also calls it inline during intake).
- **Generate an offer/disclosure:** `POST /los/offer {app_id, principal, annual_rate_pct, term_months}`
  (origination → `disclosure-service`), or `/disclosure/*` directly.
- **Board an approved app to servicing:** `POST /los/applications/{id}/accept`.
- **Take a payment:** `/payments/*` → `payment-service` (captures the charge, then calls
  servicing `POST /accounts/{loan_id}/apply-payment` to post it). The legacy `POST /lss/payments`
  path is dead-but-present.
- **Look at the portfolio:** `GET /lss/loans?limit=25&offset=0&status=current` (requires auth).
- **Reconciliation eyeball:** `GET /lss/reconciliation/peek` (ledger vs settlement totals).

## Known operational pain (unresolved)

- **Payment retries — FIXED, keep watching.** The processor occasionally times out and
  clients retry. This used to insert a second `payments` row and apply the charge twice.
  `payment-service` now requires an `idempotency_key` (`db/migrations/0007`) and a partial
  unique index enforces it, with apply-once protection on the servicing side (`payment_applications`). If a
  "double charge" ticket still arrives, it is a new bug, not this one: check whether the
  caller sent a *different* key for the same retry, which defeats the control by design.
- **Decision/disclosure/KYC stalls block applicants.** Origination calls these over
  synchronous HTTP with no timeout or retry. If `decision-service`'s credit pull hangs, the
  applicant-facing origination request hangs with it. Watch `decision-service` latency when
  intake requests pile up. (No circuit breaker / fallback.)
- **Month-end close.** `reconciliation.peek` totals do not tie out and nothing runs on a
  schedule. Finance reconciles by hand in a spreadsheet.
- **No log line writes a card-number-shaped value — and that is narrower than "no SSN".**
  This entry previously said `payment-service` logs full PAN/CVV/SSN at INFO and that
  origination logs full PII at intake. Both are false against the current code and were
  verified line by line. The guarantee is stated as card-number-shaped data rather than
  "no SSN" because one gap is unclosable by validation: servicing's legacy charge logs
  `loan_id` before the insert that would reject a nonexistent loan, and a nine-digit
  integer is both a plausible loan id and an SSN — `412559981` is accepted by any bound
  that does not also refuse real ids (see `servicing-service/app/logging_config.py`).
  Treat an SSN-shaped identifier in that log as possible; everything below is what was
  actually verified:
  `payment-service`'s `PaymentIn` sets `extra="forbid"` and accepts only a processor
  token plus `last4`/`brand`, so a field *named* `pan`/`cvv`/`ssn` is rejected with a
  422 (ADR 0008) — that is a check on field names, not on content, so it is the
  redactor and not the schema that covers card data pushed through an allowed field
  (a PAN in `processor_token`). `charge()` logs `redact_dict` output, which masks
  sensitive keys and runs the PAN/SSN/CVV patterns over every other string value, with
  the cardholder name omitted entirely;
  `servicing-service`'s legacy charge logs `loan_id`/`amount`/`method` only; and
  `origination-service`'s intake logs `app_id`/`applicant_id` (PR #6 review, Gap C).
  **What logs still carry:** identifiers and financial decision data — applicant,
  application and loan ids, model scores, decisions, reason codes, balances, amounts,
  and `last4`/`brand`. Those are the correlation fields operations needs; they are not
  claimed to be free of privacy consequence, and `last4` and processor tokens remain
  security-relevant. **Scope:** verified at application level only. Reverse-proxy,
  container-runtime and deployment-platform logging were not available in this
  repository and were not tested, so confirm those before shipping logs to a
  third-party aggregator. See `docs/DEBT.md` D5a.
- **Provider keys have no rotation procedure.** `.env` is untracked and the hardcoded
  fallbacks are gone from all seven services, so this bullet no longer describes secrets
  sitting in the repository — it used to say `.env` is committed, which stopped being true
  and stayed on the page. The bureau, core-banking and processor keys are still supplied
  by environment alone with no rotation runbook and no expiry, which is the part that
  remains open. See `docs/ROADMAP.md` for why the historical values were placeholders
  rather than live credentials.

## Rotating `INTERNAL_SERVICE_TOKEN`

The shared secret every service checks on internal calls. Read this before rotating,
because the current design **cannot** rotate without a brief outage window and pretending
otherwise is how a rotation turns into an incident.

**Why there is a window.** Each service compares the caller's `X-Internal-Token` against
its own single configured value with `secrets.compare_digest`. There is no
accept-old-and-new period. So between the first restarted service and the last, a caller
holding one value talks to a service expecting the other and gets a 401. Money-moving
routes fail closed during that gap, which is the correct direction and still an outage.

**Procedure.**

1. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. At least 32
   characters and no placeholder words, or startup validation refuses it outside a
   development `ENVIRONMENT`.
2. Put it in the deployment's secret store and in your local gitignored `.env`. Never in
   this repository — a value committed here is not a secret, which is exactly the failure
   the token defends against.
3. Restart **all** services together rather than rolling. Rolling makes the window longer,
   not shorter, because every pair that disagrees fails for the whole roll.
4. Quiesce first if you can pick the moment: no in-flight card authorization means no
   payment can be captured against a servicing call that 401s mid-flight.
5. Verify after: `POST /kyc/check` with the OLD token returns 401, with the new one 200;
   an intake through `/los/applications` completes; a servicing money route 401s without a
   token. All three, because each covers a different service's copy of the value.

**If a zero-downtime rotation is ever required**, the change is to accept a list of valid
tokens rather than one — old and new both valid for the length of the rollout, then the
old one dropped. That is a code change to every service's `config.py` and its comparison,
not a runbook step, and it is not implemented today. Stated so nobody plans a
zero-downtime rotation against a system that cannot do one.

**If the token is believed compromised**, rotate immediately and accept the window: the
alternative is leaving a value that authorises money movement in an attacker's hands for
the length of a code change.

## Tests

```bash
make test    # runs pytest in both backend services (non-blocking)
```

`test_apr.py` (disclosure-service) and `test_money.py` (servicing-service) used to
FAIL by design, encoding float-rounding defects (D12/D6). Both are fixed now — a
real Decimal migration, not a weakened test (see `docs/ROADMAP.md`, Week 1). CI
no longer runs any service `continue-on-error`; every service's tests are blocking.
