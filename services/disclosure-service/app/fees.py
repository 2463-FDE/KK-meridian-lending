"""Fee constants.

Single source of truth for the origination fee — apr.py and offer.py both
import ORIGINATION_FEE_PCT from here instead of redeclaring their own copy.
That copy-paste was exactly how apr.py's copy drifted to 0.025 against the
published 3.0% (D6). Published source: policies/fee_schedule.md.
"""
from decimal import Decimal

ORIGINATION_FEE_PCT = Decimal("0.030")   # policy: 3.0%

# Identifies the rounding policy that produced a stored payment schedule, so a
# future policy change is distinguishable per row rather than retroactive.
# 'B1' = cent-rounded level regular payments, final period bills the remaining
# principal plus that period's interest. Bump this if that changes; never
# reinterpret existing rows under a new policy.
SCHEDULE_VERSION = "B1"

# The contractual note rate every offer is written at. It was a bare `7.99`
# inline in routers/offers.py -- the same shape as the fee constant before D6
# drifted three copies apart.
#
# **The note rate is no longer here.** It was `NOTE_RATE_PCT = Decimal("7.99")`,
# and it was what actually got calculated and persisted -- so when pricing moved
# to a configured value (`config.DEMO_NOTE_RATE_PCT`, PR #80), this constant
# would have quietly kept deciding the borrower's rate. Review caught it: set the
# configured rate to 8.50 and the offer would still have been built at 7.99.
# Removed rather than aliased, so a sixth copy cannot reappear here.
#
# The note rate is NOT the APR. The APR is solved against the amount financed
# and is higher whenever a prepaid fee exists; the note rate is what the payment
# schedule actually runs on. Conflating them is what PR #10's review caught.
LATE_FEE_FLAT = Decimal("35.0")
NSF_FEE = Decimal("25.0")

# Frozen, NOT policy. Offers created before fee_pct_used existed carry no
# snapshot; db/migrations/0011 back-fills those rows with exactly this value.
# A read path reconstructing such a row must use this constant and never
# ORIGINATION_FEE_PCT above -- reading the live rate is what makes a legacy
# offer's recovered principal drift the next time the fee policy changes, which
# is the whole reason fee_pct_used exists. Changing ORIGINATION_FEE_PCT must
# not change this number.
LEGACY_PRE_SNAPSHOT_FEE_PCT = Decimal("0.030")


def origination_fee(amount) -> float:
    p = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return float(p * ORIGINATION_FEE_PCT)


def late_fee(past_due) -> float:
    # "flat $35 OR 5% of past due, whichever is less" -- but this returns the
    # flat fee only. Separate, pre-existing logic bug, not part of the
    # Decimal/fee-drift fix here.
    return float(LATE_FEE_FLAT)
