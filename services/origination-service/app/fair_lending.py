"""ZIP-level disparate-impact check (Week 8 fair-lending fix).

ROADMAP.md's own Week 8 finding: "Can't be checked at all -- confirmed no ZIP
field exists anywhere in the schema." db/migrations/0008_add_applicant_zip.sql
adds the field; this module is the actual check it unblocks.

Groups decisions by ZIP3 (the first 3 digits of the applicant's ZIP) rather
than the full 5-digit ZIP -- a platform this size doesn't have enough
decisions per single ZIP for a per-ZIP rate to mean anything. Applies the
four-fifths rule: EEOC's own adverse-impact screen, not a bespoke statistic --
a group's approval rate below 80% of the highest-approval group's rate is
flagged for review. This does not itself prove discrimination; it's the same
first-pass screen real fair-lending compliance programs use to decide what's
worth a deeper look.
"""
from . import db


def _zip3(zip_code: str | None) -> str | None:
    if not zip_code or len(zip_code) < 3:
        return None
    return zip_code[:3]


def zip_disparate_impact_report(min_group_size: int = 5) -> dict:
    """Approval rate per ZIP3, four-fifths-rule flag against the highest rate.

    Groups below min_group_size are excluded from flagging (too small a
    sample to mean anything) but still reported for visibility.
    """
    rows = db.query(
        "SELECT ap.zip_code, d.outcome "
        "FROM decisions d "
        "JOIN applications a ON a.id = d.app_id "
        "JOIN applicants ap ON ap.id = a.applicant_id "
        "WHERE ap.zip_code IS NOT NULL"
    )

    groups: dict[str, dict[str, int]] = {}
    for r in rows:
        z3 = _zip3(r["zip_code"])
        if not z3:
            continue
        g = groups.setdefault(z3, {"total": 0, "approved": 0})
        g["total"] += 1
        if r["outcome"] == "approve":
            g["approved"] += 1

    zip_rates = {z3: g["approved"] / g["total"] for z3, g in groups.items() if g["total"] > 0}
    if not zip_rates:
        return {"groups": [], "max_rate": None, "four_fifths_threshold": None, "flagged": []}

    max_rate = max(zip_rates.values())
    threshold = max_rate * 0.8

    group_reports = []
    flagged = []
    for z3, rate in sorted(zip_rates.items()):
        total = groups[z3]["total"]
        eligible = total >= min_group_size
        is_flagged = eligible and rate < threshold
        group_reports.append({
            "zip3": z3,
            "total": total,
            "approved": groups[z3]["approved"],
            "approval_rate": round(rate, 4),
            "eligible_for_flagging": eligible,
            "flagged": is_flagged,
        })
        if is_flagged:
            flagged.append(z3)

    return {
        "groups": group_reports,
        "max_rate": round(max_rate, 4),
        "four_fifths_threshold": round(threshold, 4),
        "flagged": flagged,
    }
