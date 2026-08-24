"""Disclosure service configuration."""
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:postgres@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# --- the note rate an offer is calculated and stored at ----------------------
#
# **The same figure origination publishes**, read from the same environment
# variable (`origination-service/app/config.py::DEMO_NOTE_RATE_PCT`). A training
# default, not a pricing policy: there is one rate, it applies to every offer,
# and nothing in this system underwrites a per-applicant rate.
#
# Why a second read rather than trusting the caller's `annual_rate`: this service
# deliberately stopped sourcing principal, term and rate from the request body,
# because a repeat POST for an approved application could otherwise overwrite the
# canonical offer with whatever numbers the caller sent. Reading configuration
# keeps that property and still gives one authority for the figure.
#
# Why a second read rather than importing origination's: these are separate
# services with separate images and no shared library (ADR 0002). The two values
# are compared by `db/tests/test_the_note_rate_has_one_source.py`, which fails if
# they can disagree -- the same shape as the maker-checker limits guard.
DEMO_NOTE_RATE_PCT = float(os.getenv("DEMO_NOTE_RATE_PCT", "7.99"))

# Required on every POST /offers (see routers/offers.py) -- defense in depth
# for the network boundary (disclosure-service has no host port mapping; see
# docker-compose.yml). Unset means no caller can ever match it, so a deploy
# that forgets to configure this fails closed rather than open.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
