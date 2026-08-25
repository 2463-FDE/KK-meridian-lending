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

# Which backend calls Claude. "bedrock" (default) = AWS Bedrock, "anthropic" =
# direct Anthropic API reading ANTHROPIC_API_KEY. Bedrock reads standard AWS env vars
# (AWS_BEARER_TOKEN_BEDROCK or the normal AWS credential chain, AWS_REGION) --
# see llm_client.py::make_client(). Config-driven so one vendor is never
# hardcoded, same principle as the CreditBureauClient abstraction planned for
# decision-service (RF-21).
# Default: bedrock. Changed from "anthropic" when the summary became an agent.
#
# The agent runtime refuses any provider but Bedrock, so leaving the default on
# direct Anthropic meant the DOCUMENTED configuration produced: agent enabled ->
# provider anthropic -> agent refuses -> no summary. A developer following the
# supported setup would have selected the architecture the client rejected and
# then found the feature broken, which is the worst of both. Reviewed on PR #63.
#
# policy_chat still runs on whichever provider is configured; it is not the
# agent path and is unaffected by this default beyond which vendor it calls.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "bedrock")

# Required when LLM_PROVIDER=bedrock -- Bedrock model ids differ from direct-API
# model ids and depend on what your AWS account/region has been granted access
# to, so there is no safe default to fall back to here.
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "")

# The region the Bedrock client uses. Read here rather than left to the SDK's
# own inference: `anthropic` 1.x and `boto3` disagree about what happens when no
# region can be found, and an unset region surfaced as a CI failure on an
# unrelated PR (see services/loan-assistant/requirements.txt). Empty means "let
# the SDK infer", which is the previous behaviour, but it is now a stated choice
# rather than an accident.
AWS_REGION = os.getenv("AWS_REGION", "")

#: Read so the trace emitter can tell "tracing on but unconfigured" from
#: "tracing on and shipping". The VALUE is never logged, traced or returned --
#: credentials are on the client's prohibited-retention list, and only its
#: presence is ever consulted.
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY", "")

#: The project runs are filed under. Read because a distributed parent arrives
#: with a project name in its `baggage` header, and accepting that would let the
#: sender decide where Meridian's runs land; this is the value that overrides it.
#: Empty means "whatever the SDK defaults to", which is the SDK's own business.
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "")

# --- agentic underwriting summary --------------------------------------------
#
# The summary path runs as a LangChain v1 agent with ONE bounded policy tool
# (app/agent.py, app/policy_tool.py). The flag exists so the agent path can be
# turned off in an environment that has no Bedrock access at all, NOT so it can
# silently degrade: with it off the summary route refuses rather than falling
# back to a direct prompt-to-text call, because a silent fallback is exactly the
# architecture the client rejected and nothing downstream would reveal it.
AGENT_ENABLED = os.getenv("AGENT_ENABLED", "true").lower() not in ("0", "false", "no")

#: Ceiling on the agent's own output. Separate from MAX_INPUT_TOKENS, which
#: guards what we send.
AGENT_MAX_OUTPUT_TOKENS = int(os.getenv("AGENT_MAX_OUTPUT_TOKENS", "1024"))

#: Per-request timeout on the Bedrock call, in seconds.
#:
#: 20.0 is not a new number -- it is `llm_client.TIMEOUT_SECONDS`, the value the
#: summary path has always advertised, restated here because the agent path does
#: not go through `call_api` and stopped inheriting it. Left unset, botocore
#: applied its own 60s connect/60s read per model turn instead, so the 504 the
#: route documents had become unreachable. Reviewed on PR #63.
AGENT_REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("AGENT_REQUEST_TIMEOUT_SECONDS", "20.0"))

#: Hard ceiling on agent loop steps per summary (LangGraph `recursion_limit`).
#: Three steps is the minimum useful path -- decide, call the tool, answer. The
#: observed real run used seven. Twelve leaves room for a couple of extra
#: retrieval attempts and stops well short of anything that could run up a bill.
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "12"))

#: There is deliberately NO switch here for "accept a summary when policy
#: retrieval found nothing".
#:
#: An earlier revision of this PR had one (`AGENT_REQUIRE_POLICY_HIT`, default
#: on). Reviewed and removed, because the only thing it could do was produce an
#: ungrounded summary that looked exactly like a grounded one: nothing in the
#: LoanSummary, the API response or the UI distinguishes REAL from FALLBACK
#: today, so a demo run with retrieval missing would have been unreadable as
#: such by the person watching it. A toggle whose off-state cannot be seen in
#: the output is not a demo affordance, it is a silent downgrade of the exact
#: guarantee this PR exists to make.
#:
#: So the refusal is unconditional (`llm_client._summary_text_via_agent`). If a
#: classified fallback is genuinely wanted later, the classification has to ship
#: WITH it -- visible in the response and asserted in a test -- not before it.

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
