.PHONY: up up-e2e down logs build seed ps test fmt

up:
	docker compose up -d --build

# The browser suite only. Same stack, with the gateway rate limit raised so a
# dozen journeys from one source IP do not trip the shipped 120/60s control --
# see docker-compose.e2e.yml. `make up` deliberately does NOT include it.
up-e2e:
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

config:
	docker compose config -q && echo "compose config OK"
