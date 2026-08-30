#!/usr/bin/env bash
#
# Prove that the configured AI provider is REACHABLE and answering, against a
# running stack.
#
# WHY A SCRIPT AND NOT A TEST -- the same reasoning `check_self_approval.sh`
# carries, and for a sharper reason here.
#
# The browser suite stubs the model at the network boundary, deliberately: CI has
# no Bedrock credentials, and a suite that needed them would fail for want of a
# credential rather than for a defect. That stubbing is correct and must stay.
#
# But it means a green E2E run proves the APPLICATION behaves under deterministic
# boundaries. It proves nothing about whether the provider can be reached at demo
# time. Those are different claims, and the difference is not theoretical: on
# 2026-08-29 a TLS-inspecting proxy rotated its root, every outbound HTTPS call
# from `loan-assistant` began failing certificate verification, both live AI
# features returned 502 -- and the full suite stayed green throughout, because
# the suite never called the provider. The breakage was found by a person
# clicking a button.
#
# This script is the check that would have caught it. It is NOT wired into CI and
# must not be: it costs paid quota and needs credentials.
# `db/tests/test_ai_live_smoke_is_not_in_ci.py` asserts that.
#
# WHAT IT SENDS. Two policy questions and one summary request against a SEEDED,
# SYNTHETIC application. No real person's data, no real money, and it mutates
# nothing -- every probe is a read.
#
# WHAT IT PRINTS, and what it will not. Component, reachable yes/no, whether the
# response validated, and latency. It NEVER prints an API key, a prompt, an
# answer, a summary, a policy excerpt, an applicant name or any financial value.
# A readiness check that leaked the thing it was checking would be its own
# incident, and provider errors are reported by CLASS rather than echoed, because
# a provider error body can quote the request that caused it.
#
# EXIT CODE IS THE CONTRACT, same shape as the other checks in this directory:
#
#   0  ready         -- provider reachable, every probe validated
#   1  NOT READY     -- provider unreachable, refused, or answering in a shape
#                       the product cannot use. This is the finding this script
#                       exists to produce
#   2  could not run -- stack down, cannot log in, no seeded application. This
#                       says nothing about the provider
#
# Exit 1 and exit 2 must not be collapsed: "the AI path is broken" and "I could
# not tell" call for different responses before a demo.
#
# Usage:  bash scripts/check_ai_live.sh
#         make ai-live-smoke
set -uo pipefail

GW="${GATEWAY_URL:-http://localhost:8000}"
PASS=0
FAIL=0
#: Set once the provider has demonstrably answered. See the refusal probe.
REACHABLE=0

ok()     { echo "  READY      $1"; PASS=$((PASS+1)); }
bad()    { echo "  NOT READY  $1"; FAIL=$((FAIL+1)); }
step()   { echo; echo "=== $1"; }
cannot() { echo; echo "CANNOT RUN: $1"; exit 2; }

# Do not trust `command -v`. On Windows, `python3` resolves to a Microsoft Store
# stub that exists, runs, and is not a Python -- so a name being on PATH proves
# nothing. Each candidate is handed real JSON and must return the right answer.
# `check_self_approval.sh` already learned this; the fix is copied from it rather
# than rediscovered, which is exactly what happened here first.
PYBIN=""
for _c in python3 python py; do
  command -v "$_c" >/dev/null 2>&1 || continue
  if [ "$(printf '{"k":42}' | "$_c" -c "import sys,json;print(json.load(sys.stdin)['k'])" 2>/dev/null)" = "42" ]; then
    PYBIN="$_c"; break
  fi
done
[ -n "$PYBIN" ] || cannot "no working Python found (tried python3, python, py) -- used only to read JSON"

jq_() { "$PYBIN" -c "$1" 2>/dev/null; }

# Response body and status WITHOUT a temp file.
#
# The first version wrote each response to /tmp and read it back in Python. On
# Git Bash that silently fails: curl writes to the MSYS-mapped path while a
# Windows Python resolves `/tmp/...` as something else entirely, so every parse
# read a file that was not there and reported the provider "degraded" while it
# was answering perfectly. A readiness check that cries wolf before a demo is
# worse than no check, so nothing here touches the filesystem: the body arrives
# on stdout and is piped straight into Python.
#
# `-w '\n%{http_code}'` appends the status as the last line; `body_of` and
# `status_of` split it back apart.
status_of() { printf '%s' "$1" | tail -n 1; }
body_of()   { printf '%s' "$1" | sed '$d'; }

# Milliseconds since epoch, portable enough for the two shells this runs in.
now_ms() { "$PYBIN" -c 'import time;print(int(time.time()*1000))'; }

curl -s -o /dev/null --max-time 10 "$GW/health" \
  || cannot "gateway not answering at $GW -- start the stack first (make up)"

login() {
  curl -s -X POST "$GW/auth/login" -H 'Content-Type: application/json' \
    --max-time 15 -d "{\"username\":\"$1\",\"password\":\"password\"}" \
    | jq_ 'import sys,json;print(json.load(sys.stdin).get("token",""))'
}

# Seeded demo login (db/init/002_seed.sql). Local training data, never real.
UW="$(login underwriter)"
[ -n "$UW" ] || cannot "could not log in as the seeded underwriter"

# ---------------------------------------------------------------- policy chat
#
# A question the corpus can answer. `test_policy_chat_examples_are_answerable.py`
# holds the same expectation at unit speed against the retrieval gate; this asks
# whether the MODEL behind it is reachable and returns evidence.
step "Policy Chat -- grounded answer"
T0=$(now_ms)
RESP=$(curl -s -w '\n%{http_code}' --max-time 90 \
  -X POST "$GW/assistant/policy-chat" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $UW" \
  -d '{"question":"What is the late fee?"}')
MS=$(( $(now_ms) - T0 ))
CODE=$(status_of "$RESP")

if [ "$CODE" != "200" ]; then
  # Deliberately the status only. A provider error body can quote the request
  # that produced it, and this script must not print prompts.
  bad "policy chat did not answer (HTTP $CODE) -- provider unreachable or refusing"
else
  VERDICT=$(body_of "$RESP" | jq_ 'import sys,json
d=json.load(sys.stdin)
print("ok" if (d.get("answerable") and d.get("source_chunk_id") and (d.get("answer") or "").strip()) else "shape")')
  if [ "$VERDICT" = "ok" ]; then
    ok "policy chat answered with evidence (${MS}ms)"
    REACHABLE=1
  else
    bad "policy chat answered but without grounding evidence -- the answer path is degraded"
  fi
fi

# ---------------------------------------------------------------- refusal path
#
# The other half of the contract, and cheap: one more small call. An assistant
# that answers everything is a worse failure than one that is unreachable,
# because it looks like it is working.
# ONLY MEANINGFUL IF THE PROVIDER IS UP, and that is not a detail.
#
# A refusal is `answerable: false`. So is a provider that cannot be reached --
# the route fails closed, which is correct behaviour and indistinguishable from
# a working evidence gate if you only look at this field. Run against a stack
# with a broken CA bundle, this probe reported READY while the other two
# reported the outage: green for the wrong reason, which is the failure mode
# this whole script exists to prevent.
#
# So it is skipped unless probe 1 proved the provider answers. A skip here is
# honest -- the question cannot be asked - where a pass would not be.
step "Policy Chat -- refusal on an out-of-corpus question"
if [ "$REACHABLE" -eq 0 ]; then
  echo "  SKIPPED    provider not reachable, so a refusal proves nothing"
else
RESP=$(curl -s -w '\n%{http_code}' --max-time 90 \
  -X POST "$GW/assistant/policy-chat" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $UW" \
  -d '{"question":"What is the current share price of an unrelated public company?"}')
CODE=$(status_of "$RESP")

if [ "$CODE" != "200" ]; then
  bad "refusal probe did not answer (HTTP $CODE)"
else
  REF=$(body_of "$RESP" | jq_ 'import sys,json
print("refused" if not json.load(sys.stdin).get("answerable") else "answered")')
  if [ "$REF" = "refused" ]; then
    ok "out-of-corpus question refused, as the evidence gate requires"
  else
    bad "an out-of-corpus question was ANSWERED -- the evidence gate is not holding"
  fi
fi
fi

# ---------------------------------------------------------------- ai summary
#
# The staff summary is the other live model path, and it is agent-backed rather
# than single-call, so it can fail where policy chat succeeds.
# CHOOSING THE APPLICATION IS PART OF THE CHECK, not a detail.
#
# The first version took `?limit=1&offset=0`, which is newest-first, and on a
# FRESH seed that is application 6014 -- whose `employment_years` is NULL.
# `summarize_application()` raises `LLMInsufficientDataError` for a missing
# income or employment figure, so the route answers 422 before the provider is
# contacted at all, and the smoke announced a provider outage while the provider
# was perfectly healthy. I did not see it because I only ever ran this against a
# database the browser suite had already added applications to. Same cry-wolf
# class as the /tmp defect above, found by review rather than by me.
#
# So a candidate is CHECKED for the fields the summary needs, using the
# staff-only financials route, rather than assumed. No model call is involved in
# picking one.
step "AI application summary"
APPID=""
CANDIDATES=$(curl -s --max-time 15 "$GW/los/applications?limit=25&offset=0&order=oldest" \
  -H "Authorization: Bearer $UW" \
  | jq_ 'import sys,json
items=json.load(sys.stdin).get("items") or []
print(" ".join(str(i["id"]) for i in items))')
for _id in $CANDIDATES; do
  SUITABLE=$(curl -s --max-time 15 "$GW/los/applications/$_id/financials" \
    -H "Authorization: Bearer $UW" \
    | jq_ 'import sys,json
d=json.load(sys.stdin)
print("yes" if d.get("income") is not None and d.get("employment_years") is not None else "no")')
  if [ "$SUITABLE" = "yes" ]; then APPID="$_id"; break; fi
done
[ -n "$APPID" ] || cannot "no seeded application carries both income and employment_years, so the summary cannot be exercised"

T0=$(now_ms)
RESP=$(curl -s -w '\n%{http_code}' --max-time 120 \
  -X POST "$GW/assistant/applications/$APPID/summary" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $UW" -d '{}')
MS=$(( $(now_ms) - T0 ))
CODE=$(status_of "$RESP")

if [ "$CODE" = "422" ]; then
  # Insufficient data on the application, which says nothing about the provider.
  # Reporting it as NOT READY would be the same conflation the refusal probe
  # made: a check must not answer a question it did not get to ask.
  echo "  SKIPPED    application $APPID lacks the inputs a summary needs -- not a provider verdict"
elif [ "$CODE" != "200" ]; then
  bad "summary did not generate (HTTP $CODE) -- provider unreachable or refusing"
else
  # Length only. The summary describes a synthetic applicant's finances and is
  # exactly the kind of content this script must not echo.
  LEN=$(body_of "$RESP" | jq_ 'import sys,json
print(len((json.load(sys.stdin).get("summary") or "").strip()))')
  if [ "${LEN:-0}" -gt 40 ]; then
    ok "summary generated (${LEN} chars, ${MS}ms)"
  else
    bad "summary returned but is empty or too short to be a summary"
  fi
fi

echo
echo "=============================================="
if [ "$FAIL" -eq 0 ]; then
  echo "AI LIVE SMOKE: READY  ($PASS checks)"
  echo "Provider reachable and answering in the expected shape."
  exit 0
fi
echo "AI LIVE SMOKE: NOT READY  ($FAIL of $((PASS+FAIL)) checks failed)"
echo
echo "This is a real finding. The deterministic suite cannot see it: the browser"
echo "tests stub the model, so they stay green while these paths are broken."
echo
echo "Most common cause on a laptop behind TLS inspection -- the interception"
echo "root rotated and the container's CA bundle is stale:"
echo "    python scripts/make_ca_bundle.py"
echo "    docker compose restart loan-assistant"
exit 1
