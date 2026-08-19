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
# EVERY SELF-RESOLUTION PROBE ASKS FOR `rejected`, NEVER `approved`. This is the
# difference between a diagnostic and a hazard, and it was review finding
# SA-001 against the first version of this script. The guard is on WHO resolves
# -- `resolved_by <> requested_by` -- not on which resolution is asked for, so a
# self-REJECTION is refused by exactly the same rule and tests exactly the same
# thing. But an APPROVAL that slipped through would write a ledger entry and
# move money, and the only environment where one could slip through is the
# broken one this script exists to find. A verifier that damages the system
# precisely when it detects damage is worse than no verifier. Asking for
# `rejected` makes the check harmless BY CONSTRUCTION, rather than by trusting
# the control it is measuring.
#
# EACH PROBE GETS ITS OWN PROPOSAL. If one layer breached and resolved a shared
# row, every later layer would fail with "already resolved" -- a second,
# invented finding masking the real one, and possibly a PASS for the wrong
# reason.
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
# WHAT IT LEAVES BEHIND. Four proposals, all resolved (rejected), none approved.
# `pending_movements` refuses deletes by design -- a proposal is the evidence of
# what staff asked for -- so the rows stay. NO MONEY MOVES at any point, and
# that holds even if every control in the system is broken, because no probe
# ever asks for an approval. Step 6 prints `ledger_entry_id` so you can see that
# rather than take it on faith.
#
# Usage:  bash scripts/check_self_approval.sh
# Env:    GATEWAY_URL (default http://localhost:8000)
#         LOAN_ID     (default: chosen automatically -- any serviced loan)

set -uo pipefail

GW="${GATEWAY_URL:-http://localhost:8000}"
PASS=0
FAIL=0

ok()     { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()    { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
step()   { echo; echo "=== $1"; }
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

raise_proposal() {   # $1 = what this proposal is for; echoes the movement id
  local raw mid
  raw=$(curl -s -X POST "$GW/lss/accounts/$LOAN/adjust-balance" \
    -H "Authorization: Bearer $UW" -H 'Content-Type: application/json' \
    -d "{\"component\":\"fees\",\"amount\":10.0,\"reason\":\"self-approval control check -- $1\"}")
  mid=$(echo "$raw" | python -c "import sys,json;print(json.load(sys.stdin).get('movement_id',''))" 2>/dev/null)
  [ -n "$mid" ] || cannot "could not raise a proposal ($1): $raw"
  echo "$mid"
}

step "STEP 1  underwriter (user $UWID) raises four proposals on loan $LOAN"
M_API=$(raise_proposal "api layer")
M_FN=$(raise_proposal "function layer")
M_SQL=$(raise_proposal "table layer")
M_OK=$(raise_proposal "second-approver success")
PROBES="$M_API $M_FN $M_SQL"
ok "raised #$M_API #$M_FN #$M_SQL #$M_OK   (all pending -- raising moves no money)"

step "STEP 2  the SAME underwriter tries to resolve #$M_API via the API (no browser)"
# `rejected`, not `approved` -- see the header. Same guard, and no ledger entry
# even if the guard has been removed.
BODY=$(curl -s -o /tmp/_sa2 -w '%{http_code}' -X POST "$GW/lss/movements/$M_API/resolve" \
  -H "Authorization: Bearer $UW" -H 'Content-Type: application/json' \
  -d '{"resolution":"rejected"}')
echo "        HTTP $BODY  $(cat /tmp/_sa2)"
[ "$BODY" = "403" ] && ok "the API refused the requester" \
                    || bad "expected HTTP 403 from the API, got $BODY"

step "STEP 3  bypass the API: resolve_pending_movement() on #$M_FN as the requester"
OUT=$(psql_ -c "SELECT resolve_pending_movement($M_FN, $UWID, 'underwriter', 'rejected', $THRESHOLD, ARRAY['current']);")
echo "$OUT" | grep -iE "^ERROR" | head -1 | sed 's/^/        /'
echo "$OUT" | grep -qi "may not resolve it" \
  && ok "the function refused the requester" \
  || bad "the function did NOT refuse the requester -- read its output above"

step "STEP 4  bypass the function: raw UPDATE straight at #$M_SQL"
OUT=$(psql_ -c "UPDATE pending_movements SET resolution='rejected', resolved_by=$UWID, resolved_role='underwriter', resolved_at=now(), resolved_threshold=$THRESHOLD WHERE id=$M_SQL;")
echo "$OUT" | grep -iE "^ERROR" | head -1 | sed 's/^/        /'
echo "$OUT" | grep -qi "no_self_approval" \
  && ok "the CHECK constraint refused it -- enforced by the schema, not the application" \
  || bad "constraint no_self_approval did NOT fire -- read the output above"

step "STEP 5  a DIFFERENT person (admin, user $ADID) resolves #$M_OK -- this MUST succeed"
BODY=$(curl -s -o /tmp/_sa5 -w '%{http_code}' -X POST "$GW/lss/movements/$M_OK/resolve" \
  -H "Authorization: Bearer $AD" -H 'Content-Type: application/json' \
  -d '{"resolution":"rejected"}')
echo "        HTTP $BODY  $(cat /tmp/_sa5)"
[ "$BODY" = "200" ] && ok "a second person could resolve it -- the control refuses the right thing, not everything" \
                    || bad "a second approver could NOT resolve (HTTP $BODY) -- the control may be refusing everyone"

step "STEP 6  close the probe proposals, then show the audit trail"
# The three probes should still be pending, because every layer refused them.
# Resolve them as admin so they do not sit in the queue for ever. If one is
# already resolved, a layer breached and step 2, 3 or 4 has already said so.
for m in $PROBES; do
  psql_ -c "SELECT resolve_pending_movement($m, $ADID, 'admin', 'rejected', $THRESHOLD, ARRAY['current']);" >/dev/null 2>&1
done
psql_ -c "SELECT id, requested_by, requested_role, resolved_by, resolved_role, resolution, ledger_entry_id
            FROM pending_movements WHERE id IN ($M_API, $M_FN, $M_SQL, $M_OK) ORDER BY id;" | head -9
echo "        Every ledger_entry_id is empty: nothing was approved, so no money moved."

echo
echo "======================================================="
echo "  $PASS passed, $FAIL failed   (movements #$M_API #$M_FN #$M_SQL #$M_OK)"
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
