"""Loan Assistant service configuration."""
import os

ORIGINATION_URL = os.getenv("ORIGINATION_URL", "http://origination-service:8001")

# Bug fix: origination-service's /financials route requires this on top of
# X-User-Role (review fix closing a role-spoofing gap -- see
# origination-service/app/routers/applications.py::_is_staff). This service
# never sent it, so /applications/{id}/summary 403'd for every staff role,
# every time -- the AI Summary feature was broken end to end. Shared with
# every other service's own INTERNAL_SERVICE_TOKEN (docker-compose.yml).
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Which backend calls Claude. "anthropic" (default) = direct Anthropic API,
# reads ANTHROPIC_API_KEY. "bedrock" = AWS Bedrock, reads standard AWS env vars
# (AWS_BEARER_TOKEN_BEDROCK or the normal AWS credential chain, AWS_REGION) --
# see llm_client.py::make_client(). Config-driven so one vendor is never
# hardcoded, same principle as the CreditBureauClient abstraction planned for
# decision-service (RF-21).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")

# Required when LLM_PROVIDER=bedrock -- Bedrock model ids differ from direct-API
# model ids and depend on what your AWS account/region has been granted access
# to, so there is no safe default to fall back to here.
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "")

# --- one grounded external signal for the officer summary (app/macro.py) -----
# Week 1-4 review: the summary drew everything from inside the application.
# MACRO_ENABLED=0 turns the outbound call off entirely -- what the test suite
# uses, and what any environment without egress should set. The series is the
# BLS national unemployment rate; v1 needs no API key but allows only 25
# requests/day per IP, so the TTL is long on purpose (the series is monthly).
MACRO_ENABLED = os.getenv("MACRO_ENABLED", "1") not in ("0", "false", "False", "")
MACRO_SERIES_ID = os.getenv("MACRO_SERIES_ID", "LNS14000000")
MACRO_CACHE_TTL_SECONDS = int(os.getenv("MACRO_CACHE_TTL_SECONDS", str(6 * 60 * 60)))
MACRO_TIMEOUT_SECONDS = float(os.getenv("MACRO_TIMEOUT_SECONDS", "3.0"))
# How long a FAILED fetch suppresses further attempts (negative cache).
#
# Without this, an outage costs every single summary request its own full
# timeout: the failure was never recorded, so each request rediscovered it.
# Short by design -- this is a suppression window, not a cache of data. Long
# enough that a burst of summaries makes one attempt between them, short enough
# that the signal returns promptly once BLS recovers.
MACRO_FAILURE_TTL_SECONDS = float(os.getenv("MACRO_FAILURE_TTL_SECONDS", "60"))
# How long past MACRO_CACHE_TTL_SECONDS a previously-fetched figure may still be
# served while a refresh is impossible (in flight, or suppressed).
#
# Serving it is honest because MacroSignal carries its own `period` -- the
# officer reads "June 2026" whether the fetch happened a minute or a day ago, so
# nothing presents old data as current. Bounded anyway: past this window the
# citation is dropped rather than shown indefinitely.
MACRO_STALE_SERVE_SECONDS = float(os.getenv("MACRO_STALE_SERVE_SECONDS", str(24 * 60 * 60)))
