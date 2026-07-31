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
