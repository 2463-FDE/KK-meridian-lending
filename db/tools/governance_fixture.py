"""Load the synthetic governance fixture, and refuse to load a changed one.

Every offline tool here reads the fixture through this module, so the checksum
verification happens once and cannot be skipped by whoever writes the next tool.
A file that changed without its checksum being regenerated is a fixture nobody
reviewed, and every result computed from it would be unreviewed too.

**Nothing in here is a runtime path.** It lives under `db/tools/` because
`services/**` is scanned by `db/tests/test_no_runtime_protected_class_proxy.py`
and must never so much as name the fixture directory. This module names it
constantly, which is exactly why it is not a service.

**No network, no database, no model.** `db/tests/test_offline_fairness_eval.py`
asserts this module imports nothing that could reach one.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

#: The one location synthetic protected-class labels may occupy. A real vendor
#: packet would replace this fixture entirely rather than merge into it -- see
#: `policies/vendor-document-precedence-and-versioning.md`.
FIXTURE_DIR = REPO / "fixtures" / "governance"

#: Declared in the fixture's `README.md` and carried by every taxonomy entry.
FIXTURE_VERSION = "SYN-GOV-2.0"

#: Stamped on every artefact any tool here produces. The fairness-data policy
#: (rule 5) forbids a production or real-world fairness claim from this fixture;
#: a banner is not a substitute for that rule, but an output that travels
#: without one is how a training number becomes a quoted number.
TRAINING_BANNER = "SYNTHETIC / TRAINING ONLY"

#: The audit-only columns. Named here so the containment tests can assert that
#: these strings appear in the offline tooling and nowhere in a runtime service.
PROTECTED_CLASS_COLUMNS = (
    "synthetic_sex",
    "synthetic_race_ethnicity",
    "synthetic_age_band",
)


class FixtureIntegrityError(RuntimeError):
    """A fixture file does not match its recorded checksum.

    Fail closed. There is no repair path here on purpose: the precedence policy
    says an unresolved conflict escalates and is not resolved by paraphrasing or
    nearest-match, and silently continuing against altered bytes is a worse
    version of the same mistake.
    """


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checksum_report(fixture_dir: pathlib.Path | None = None) -> dict:
    """Verify every file listed in the fixture's `SHA256SUMS.txt`.

    Returns a report rather than raising, so a test can assert on the whole
    picture -- mismatches AND files present in the tree that the manifest does
    not list, which a plain `sha256sum -c` would not notice.
    """
    root = pathlib.Path(fixture_dir or FIXTURE_DIR)
    sums = root / "SHA256SUMS.txt"
    if not sums.is_file():
        raise FixtureIntegrityError(f"{sums} is missing; the fixture cannot be verified")

    listed: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")
        listed[name.strip().lstrip("*")] = digest.strip()

    verified, mismatched, missing = [], [], []
    for name, expected in sorted(listed.items()):
        path = root / name
        if not path.is_file():
            missing.append(name)
            continue
        actual = _sha256(path)
        (verified if actual == expected else mismatched).append(
            name if actual == expected else f"{name}: expected {expected}, got {actual}")

    on_disk = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS.txt"
    }
    unlisted = sorted(on_disk - set(listed))

    return {
        "fixture_dir": str(root),
        "listed": len(listed),
        "verified": len(verified),
        "mismatched": mismatched,
        "missing": missing,
        "unlisted": unlisted,
        "ok": not mismatched and not missing and not unlisted,
    }


def require_intact(fixture_dir: pathlib.Path | None = None) -> dict:
    report = checksum_report(fixture_dir)
    if not report["ok"]:
        raise FixtureIntegrityError(
            "the governance fixture does not match SHA256SUMS.txt; regenerate the "
            "manifest only after reviewing the change:\n"
            f"  mismatched: {report['mismatched']}\n"
            f"  missing:    {report['missing']}\n"
            f"  unlisted:   {report['unlisted']}"
        )
    return report


def _read(relative: str, fixture_dir: pathlib.Path | None = None) -> str:
    root = pathlib.Path(fixture_dir or FIXTURE_DIR)
    return (root / relative).read_text(encoding="utf-8")


def load_taxonomy(fixture_dir: pathlib.Path | None = None) -> dict:
    """{reason_code: entry} from the synthetic vendor taxonomy."""
    rows = json.loads(_read("vendor/reason-code-taxonomy.json", fixture_dir))
    return {r["reason_code"]: r for r in rows}


def load_wording(fixture_dir: pathlib.Path | None = None) -> dict:
    """{approved_wording_id: entry} from the approved wording table."""
    rows = json.loads(_read("vendor/approved-consumer-wording.json", fixture_dir))
    return {r["approved_wording_id"]: r for r in rows}


def load_fairness_fixture(fixture_dir: pathlib.Path | None = None) -> list[dict]:
    """The 32 audit-only rows.

    Callers get whole rows because the aggregation needs the label columns. What
    they must not do is emit one -- `offline_fairness_eval.py` aggregates and
    never returns a row, and `db/tests/test_offline_fairness_eval.py` asserts no fixture row id
    reaches the output.
    """
    text = _read("fixtures/synthetic-offline-fairness-evaluation.csv", fixture_dir)
    return list(csv.DictReader(io.StringIO(text)))


def load_acceptance_evaluations(fixture_dir: pathlib.Path | None = None) -> list[dict]:
    """The 28 acceptance cases, in file order."""
    text = _read("evaluations/governance-acceptance-evaluations.jsonl", fixture_dir)
    return [json.loads(line) for line in text.splitlines() if line.strip()]
