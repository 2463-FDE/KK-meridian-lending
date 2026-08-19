#!/usr/bin/env bash
#
# Prove that maker-checker refuses self-approval -- at four depths, against a
# running stack.
#
# WHY A SCRIPT AND NOT A TEST. `db/tests/test_0037_resolve_pending_movement.py`
# and `servicing-service/tests/test_maker_checker_api.py` already assert this in
# CI, and they are the regression guard. This is for the other question, the one
# CI cannot answer: *is the control live in the environment actually running
# right now* -- after a deploy, a config change, a database restore, or in front
# of somebody who wants to see it rather than read about it. A passing CI badge
# and a deployed system are different claims.
#
# WHY FOUR STEPS. Each removes a layer, so "the button was just disabled" is not
# an available explanation:
#
#   1. the API, called as the person who raised the proposal   -- no browser
#   2. `resolve_pending_movement()`, called directly           -- no service
#   3. a raw UPDATE straight at the table                      -- no function
#   4. a DIFFERENT person, who MUST succeed
#
# Step 4 is what makes the first three mean anything. A system that refused
# everyone would pass 1-3 exactly as a working one does, so a check that only
# ever confirms refusal cannot tell "the control works" from "nothing works".
#
# EXIT CODE IS THE CONTRACT, same shape as the reconciliation control:
#
#   0  verified       -- self-approval refused at every layer, second approver works
#   1  FAILED         -- a layer did not refuse, or a second approver could not
#                        resolve. This is a control finding, not a flaky test
#   2  could not run  -- stack down, cannot log in, threshold unreadable
#
# Exit 1 and exit 2 mean different things and must not be collapsed: "the
# control is broken" and "I could not tell" call for different responses.
#
# WHAT IT LEAVES BEHIND. One proposal, rejected by admin. `pending_movements`
# refuses deletes by design -- a proposal is the evidence of what staff asked
# for -- so the row stays, resolved. NO MONEY MOVES at any point: a rejection
# writes no ledger entry, which step 6 prints so you can see it rather than
# take it on faith.
#
# Usage:  bash scripts/check_self_approval.sh
# Env:    GATEWAY_URL (default http://localhost:8000)
#         LOAN_ID     (default: chosen automatically -- any serviced loan)

set -uo pipefail

GW="${GATEWAY_URL:-http://localhost:8000}"
PASS=0
FAIL=0

ok()    { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()   { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
step()  { echo; echo "=== $1"; }
cannot() { echo; echo "CANNOT RUN: $1"; exit 2; }

psql_() { docker compose exec -T postgres psql -U meridian -d meridian "$@" 2>&1; }

login() {
  curl -s -X POST "$GW/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$1\",\"password\":\"password\"}" \
    | python -c "import sys,json;print(json.load(sys.stdin).get('token',''))" 2>/dev/null
}

# --- preconditions, each failing as "could not run" rather than "control broken"

command -v docker >/dev/null || cannot "docker is not on PATH"
command -v python >/dev/null || cannot "python is not on PATH (used only to read JSON)"

# Seeded demo logins (db/init/002_seed.sql). Local training data, never real.
UW="$(login underwriter)"
AD="$(login admin)"
[ -n "$UW" ] && [ -n "$AD" ] || cannot "could not log in -- is the stack up? (docker compose ps)"

UWID=$(curl -s "$GW/auth/me" -H "Authorization: Bearer $UW" | python -c "import sys,json;print(json.load(sys.stdin)['id'])" 2>/dev/null)
ADID=$(curl -s "$GW/auth/me" -H "Authorization: Bearer $AD" | python -c "import sys,json;print(json.load(sys.stdin)['id'])" 2>/dev/null)
[ -n "$UWID" ] && [ -n "$ADID" ] || cannot "could not resolve the acting user ids from /auth/me"

# The threshold is READ from the running service, never assumed. Writing a
# figure in here would put a second copy of a configured money value in the
# repository, free to drift from the deployed one -- the defect this codebase
# keeps correcting in its own documents.
THRESHOLD=$(docker compose exec -T servicing-service printenv MAKER_CHECKER_ADMIN_THRESHOLD 2>/dev/null | tr -d '\r\n')
[ -n "$THRESHOLD" ] || cannot "MAKER_CHECKER_ADMIN_THRESHOLD is unset in servicing-service"

# Any serviced loan with a balances row. Chosen from the database rather than
# hardcoded, so this survives a reseed.
LOAN="${LOAN_ID:-$(psql_ -tAc "SELECT l.id FROM loans l JOIN balances b ON b.loan_id = l.id WHERE l.status = 'current' ORDER BY l.id LIMIT 1" | tr -d '\r')}"
[ -n "$LOAN" ] || cannot "no serviced 'current' loan with a balances row to propose against"

echo "gateway=$GW  loan=$LOAN  threshold=$THRESHOLD  requester=$UWID  approver=$ADID"

# --- the check itself

step "STEP 1  underwriter (user $UWID) raises a proposal on loan $LOAN"
RAW=$(curl -s -X POST "$GW/lss/accounts/$LOAN/adjust-balance" \
  -H "Authorization: Bearer $UW" -H 'Content-Type: application/json' \
  -d '{"component":"fees","amount":10.0,"reason":"self-approval control check"}')
MID=$(echo "$RAW" | python -c "import sys,json;print(json.load(sys.stdin).get('movement_id',''))" 2>/dev/null)
MOVED=$(echo "$RAW" | python -c "import sys,json;print(json.load(sys.stdin).get('balance_moved','?'))" 2>/dev/null)
[ -n "$MID" ] || cannot "could not raise a proposal: $RAW"
ok "raised movement #$MID   (balance_moved=$MOVED -- raising moves no money)"

step "STEP 2  the SAME underwriter tries to approve it, via the API (no browser)"
BODY=$(curl -s -o /tmp/_sa2 -w '%{http_code}' -X POST "$GW/lss/movements/$MID/resolve" \
  -H "Authorization: Bearer $UW" -H 'Content-Type: application/json' \
  -d '{"resolution":"approved"}')
echo "        HTTP $BODY  $(cat /tmp/_sa2)"
[ "$BODY" = "403" ] && ok "the API refused the requester" \
                    || bad "expected HTTP 403 from the API, got $BODY"

step "STEP 3  bypass the API: call resolve_pending_movement() as the requester"
OUT=$(psql_ -c "SELECT resolve_pending_movement($MID, $UWID, 'underwriter', 'approved', $THRESHOLD, ARRAY['current']);")
echo "$OUT" | grep -iE "^ERROR" | head -1 | sed 's/^/        /'
echo "$OUT" | grep -qi "may not resolve it" \
  && ok "the function refused the requester" \
  || bad "the function did NOT refuse the requester -- read its output above"

step "STEP 4  bypass the function: raw UPDATE straight at the table"
OUT=$(psql_ -c "UPDATE pending_movements SET resolution='approved', resolved_by=$UWID, resolved_role='underwriter', resolved_at=now(), resolved_threshold=$THRESHOLD WHERE id=$MID;")
echo "$OUT" | grep -iE "^ERROR" | head -1 | sed 's/^/        /'
echo "$OUT" | grep -qi "no_self_approval" \
  && ok "the CHECK constraint refused it -- enforced by the schema, not the application" \
  || bad "constraint no_self_approval did NOT fire -- read the output above"

step "STEP 5  a DIFFERENT person (admin, user $ADID) resolves it -- this MUST succeed"
BODY=$(curl -s -o /tmp/_sa5 -w '%{http_code}' -X POST "$GW/lss/movements/$MID/resolve" \
  -H "Authorization: Bearer $AD" -H 'Content-Type: application/json' \
  -d '{"resolution":"rejected"}')
echo "        HTTP $BODY  $(cat /tmp/_sa5)"
[ "$BODY" = "200" ] && ok "a second person could resolve it -- the control refuses the right thing, not everything" \
                    || bad "a second approver could NOT resolve (HTTP $BODY) -- the control may be refusing everyone"

step "STEP 6  the audit trail -- who asked, who decided"
psql_ -c "SELECT id, requested_by, requested_role, resolved_by, resolved_role, resolution, ledger_entry_id
            FROM pending_movements WHERE id = $MID;" | head -5
echo "        ledger_entry_id empty: a rejection writes no entry, so no money moved."

echo
echo "======================================================="
echo "  $PASS passed, $FAIL failed   (movement #$MID)"
if [ "$FAIL" = "0" ]; then
  echo "  VERIFIED: self-approval is refused at every layer, and a"
  echo "  different approver still works."
else
  echo "  CONTROL FINDING: read the FAIL lines above. This is not a"
  echo "  flaky test -- a layer that should have refused did not."
fi
echo "======================================================="
[ "$FAIL" = "0" ] || exit 1
exit 0
