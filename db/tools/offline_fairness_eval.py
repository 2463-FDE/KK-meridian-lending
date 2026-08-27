"""Offline fairness evaluation over the client's isolated synthetic fixture.

This is the evaluation Week 8 recorded as blocked. It was blocked because there
was nothing to evaluate: the client prohibited real protected-class collection
and prohibited a proxy, so the only permitted labels were the ones in a package
that had not arrived. It arrived on 2026-08-24 and is ingested under
`fixtures/offline_fairness_training/client_package_2026-08-24/`.

**What this computes.** Counts and outcome rates, grouped by each synthetic
protected-class column, over 32 synthetic rows. Nothing else.

**What this deliberately does not compute.** A verdict. There is no four-fifths
result, no disparity threshold, no pass/fail. The client's fairness-data policy
says no production or real-world fairness claim may be made from this fixture,
and their acceptance case EVAL-16 rejects exactly the claim "the model is fair
based on the 32-row fixture". A threshold would manufacture the conclusion the
package forbids, and this repository has no approved one to apply. If the client
later supplies criteria, they go in the package and this tool reads them.

**Why 32 rows could not support a verdict anyway**, stated so the absence does
not read as an oversight: the largest group here is a handful of rows. A
four-fifths ratio computed on single-digit cells moves by a whole category when
one row changes. Reporting the counts and refusing the ratio is the honest shape.

**Offline only.** CLI or test. Never a route, never a job, never a model input.
It reads the package and the package alone -- no `applicants`, no `applications`,
no `decisions`, no `decision_events`, no database connection of any kind.

Usage:

    python db/tools/offline_fairness_eval.py
    python db/tools/offline_fairness_eval.py --json
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

# `db/tools/` is a directory of scripts, not an installed package, so a sibling
# import needs the directory on the path before the import runs -- not inside
# `__main__`, which is after it.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from client_governance_package import (  # noqa: E402  (path set immediately above)
    PACKAGE_VERSION,
    PROTECTED_CLASS_COLUMNS,
    TRAINING_BANNER,
    load_fairness_fixture,
    require_intact,
)

#: Outcomes the fixture actually carries. Read from the data rather than assumed,
#: but ordered here so a table is stable between runs.
_OUTCOME_ORDER = ("approve", "refer", "deny")


def aggregate(rows: list[dict]) -> dict:
    """Counts and rates per protected-class column. Aggregate only.

    No row identifier, no score, and no per-record value is carried into the
    result. That is a containment property, not a formatting choice, and
    `test_the_output_carries_no_individual_row` asserts it.
    """
    outcomes = [o for o in _OUTCOME_ORDER
                if any(r.get("synthetic_outcome") == o for r in rows)]
    extra = sorted({r.get("synthetic_outcome") for r in rows} - set(outcomes) - {None})
    outcomes = outcomes + extra

    result: dict = {
        "banner": TRAINING_BANNER,
        "package_version": PACKAGE_VERSION,
        "total_rows": len(rows),
        "outcomes": outcomes,
        "groups": {},
        "verdict": None,
        "verdict_withheld_because": (
            "The client's fairness-data policy permits no production or real-world "
            "fairness claim from this fixture, and the package defines no disparity "
            "threshold. No pass/fail is computed. See acceptance case EVAL-16."
        ),
    }

    for column in PROTECTED_CLASS_COLUMNS:
        buckets: dict[str, collections.Counter] = collections.defaultdict(
            collections.Counter)
        for row in rows:
            buckets[row.get(column, "")][row.get("synthetic_outcome", "")] += 1

        result["groups"][column] = {
            value: {
                "n": sum(counts.values()),
                "counts": {o: counts.get(o, 0) for o in outcomes},
                "rates": {
                    o: round(counts.get(o, 0) / sum(counts.values()), 4)
                    for o in outcomes
                },
            }
            for value, counts in sorted(buckets.items())
        }

    return result


def render(result: dict) -> str:
    out = [
        f"=== OFFLINE FAIRNESS EVALUATION — {result['banner']} ===",
        f"package {result['package_version']}  ·  {result['total_rows']} synthetic rows",
        "",
        "Client-provided synthetic training fixture. Not vendor-issued, not",
        "production evidence, and not a fairness result for any real population.",
        "",
    ]
    for column, groups in result["groups"].items():
        out.append(f"-- {column} --")
        header = f"{'group':<22}{'n':>4}  " + "".join(f"{o:>10}" for o in result["outcomes"])
        out.append(header)
        for value, stats in groups.items():
            cells = "".join(
                f"{stats['counts'][o]:>4} {stats['rates'][o]:>5.2f}" for o in result["outcomes"]
            )
            out.append(f"{value:<22}{stats['n']:>4}  {cells}")
        out.append("")
    out.append(f"VERDICT: none. {result['verdict_withheld_because']}")
    out.append(f"=== END — {result['banner']} ===")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit the aggregate as JSON")
    args = parser.parse_args(argv)

    # Verify before reading. An altered fixture is not client input, and an
    # evaluation over one would carry the client's name without their bytes.
    report = require_intact()
    rows = load_fairness_fixture()
    result = aggregate(rows)
    result["checksums_verified"] = report["verified"]

    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via test_offline_fairness_eval
    raise SystemExit(main())
