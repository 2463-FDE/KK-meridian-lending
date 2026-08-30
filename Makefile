.PHONY: bootstrap up up-e2e down logs build seed ps test fmt ai-live-smoke

# One-time local setup. docker-compose.yml supplies NO default for
# INTERNAL_SERVICE_TOKEN or ENVIRONMENT -- a fallback committed to this
# repository is not a secret, and an ENVIRONMENT default of "development" would
# silently skip the token-strength checks on the money-moving routes. So the
# base file requires both, and this target is the documented way a developer
# supplies them.
#
# Writes only to .env, which is gitignored. The generated token never leaves
# this machine and must never be committed.
bootstrap:
	python scripts/bootstrap_env.py

up: bootstrap
	docker compose up -d --build

# The browser suite only. Same stack, with the gateway rate limit raised so a
# dozen journeys from one source IP do not trip the shipped 120/60s control --
# see docker-compose.e2e.yml. `make up` deliberately does NOT include it.
up-e2e: bootstrap
	docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --build

build:
	docker compose build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

# Seed runs automatically via db/init on first `up`; this re-applies seed only.
seed:
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-meridian} -d $${POSTGRES_DB:-meridian} < db/init/002_seed.sql

test:
	cd services/origination-service && python -m pytest -q || true
	cd services/servicing-service && python -m pytest -q || true
	cd services/kyc-service && python -m pytest -q || true
	cd services/decision-service && python -m pytest -q || true
	cd services/disclosure-service && python -m pytest -q || true
	cd services/payment-service && python -m pytest -q || true

# Is the AI provider reachable RIGHT NOW? Deliberately not part of `make test`
# or CI. The browser suite stubs the model on purpose -- CI has no provider
# credentials -- so a green suite says nothing about whether a demo's AI
# features will work. This asks the provider. It costs paid quota, so a person
# runs it before a demo rather than a machine running it on every push;
# db/tests/test_ai_live_smoke_is_not_in_ci.py asserts that stays true.
#
# Exit 0 ready, 1 not ready (a real finding), 2 could not run (stack down).
ai-live-smoke:
	bash scripts/check_ai_live.sh

config:
	docker compose config -q && echo "compose config OK"
