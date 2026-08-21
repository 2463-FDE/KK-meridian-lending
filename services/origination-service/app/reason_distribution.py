"""How many distinct adverse-action reasons the model actually emits.

That was the Week 8 brief's first question, and until now it was answerable
only by reading `decision.py` and counting the constants. Counting constants
tells you what the code *can* emit; this tells you what it *did*, which is the
question a regulator asks and the only one that notices a vendor whose
behaviour drifted.

Spec 0003 §1.3 requires the count of distinct reasons, the frequency of each,
and the count of decisions carrying no reason at all -- over a stated window,
**grouped by `model_version`**. The grouping is not decoration: a distribution
that silently mixes two model versions describes neither of them, and the whole
point of the exercise is to be able to say something about a specific model.

Everything comes from `decision_events`, which has carried every field this
needs since Week 3. No schema change, no new write path, and no model call --
this is a read.

**What this deliberately does not do.** It sets no threshold. "Too few distinct
reasons" is a compliance judgement, and spec 0003 records that this repository
has no authority to make one. It draws no fairness conclusion. It is not
scheduled and raises no alert, because the Week 8 deliverable is a monitoring
*spec* and a scheduler is a separate decision with its own cost. It reports
counts and lets a person read them.
"""
from . import db

#: Denial outcomes. Only a denial carries an adverse-action reason, so an
#: approval with no reason is correct rather than an anomaly, and counting it
#: as one would bury the real signal under thousands of approvals.
_ADVERSE_OUTCOMES = ("deny",)


def adverse_reason_distribution(since=None, until=None) -> dict:
    """Reason counts per `model_version` over a window.

    `since`/`until` are inclusive dates or None. Both are echoed back in the
    result, including when they are None: a report that does not state its own
    window cannot be compared with another one, and "all time" is a window
    worth saying out loud rather than leaving to be assumed.
    """
    where = ["decision = ANY(%s)"]
    params: list = [list(_ADVERSE_OUTCOMES)]
    if since:
        where.append("occurred_at >= %s")
        params.append(since)
    if until:
        where.append("occurred_at < (%s::date + INTERVAL '1 day')")
        params.append(until)

    rows = db.query(
        "SELECT model_version, reason_codes FROM decision_events "
        "WHERE " + " AND ".join(where) + " ORDER BY id",
        tuple(params),
    )

    by_version: dict[str, dict] = {}
    for row in rows:
        version = row["model_version"] or "unknown"
        bucket = by_version.setdefault(
            version, {"model_version": version, "decisions": 0,
                      "reason_frequency": {}, "missing_reason": 0})
        bucket["decisions"] += 1

        codes = [c for c in (row["reason_codes"] or [])
                 if isinstance(c, str) and c.strip()]
        if not codes:
            # Spec 0003 §1.3: should be zero. A denial with no reason on record
            # is the Reg B defect itself, so it is counted rather than skipped.
            bucket["missing_reason"] += 1
            continue
        # The principal reason is the one a notice states, so the distribution
        # is over first codes. Counting every code would describe the model's
        # internal signalling, not what applicants were told.
        principal = codes[0]
        bucket["reason_frequency"][principal] = \
            bucket["reason_frequency"].get(principal, 0) + 1

    versions = []
    for bucket in by_version.values():
        bucket["distinct_reasons"] = len(bucket["reason_frequency"])
        bucket["reason_frequency"] = dict(
            sorted(bucket["reason_frequency"].items(),
                   key=lambda kv: (-kv[1], kv[0])))
        versions.append(bucket)
    versions.sort(key=lambda b: b["model_version"])

    return {
        "window": {"since": str(since) if since else None,
                   "until": str(until) if until else None},
        "outcomes_counted": list(_ADVERSE_OUTCOMES),
        "versions": versions,
        # Deliberately absent: any threshold, pass/fail verdict, or fairness
        # conclusion. See the module docstring and spec 0003.
    }
