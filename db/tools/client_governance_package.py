"""Load the client's synthetic governance package, and refuse to load a changed one.

Every other offline tool here reads the package through this module, so the
checksum verification happens once and cannot be skipped by whoever writes the
next tool. That matters more than it sounds: the package's entire standing is
"these are the bytes the client sent". A file edited after ingestion is no longer
client input, it is repository-authored material wearing the client's name, and
that is the specific failure the client warned about in `README.md`.

**Nothing in here is a runtime path.** It lives under `db/tools/` because
`services/**` is scanned by `db/tests/test_no_runtime_protected_class_proxy.py`
and must never so much as name the fixture directory. This module names it
constantly, which is exactly why it is not a service.

**No network, no database, no model.** The package authorises none of those, and
`db/tests/test_offline_evaluator_is_contained.py` asserts this module imports
nothing that could do them.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

#: The one location the client's synthetic labels may occupy. The directory name
#: carries the package date because a later real vendor packet replaces this one
#: entirely rather than merging into it -- see the package's own
#: `policies/vendor-document-precedence-and-versioning.md`.
PACKAGE_DIR = (REPO / "fixtures" / "offline_fairness_training"
               / "client_package_2026-08-24")

#: Declared by the client in `README.md` and `PACKAGE-INVENTORY.txt`.
PACKAGE_VERSION = "CCUS-SYN-2026.08.24"

#: Stamped on every artefact any tool here produces. The client's fairness-data
#: policy rule 5 forbids a production or real-world fairness claim from this
#: package; a banner is not a substitute for that rule, but an output that
#: travels without one is how a training number becomes a quoted number.
TRAINING_BANNER = "SYNTHETIC / TRAINING ONLY"

#: The audit-only columns. Named here so the containment tests can assert that
#: these strings appear in the offline tooling and nowhere in a runtime service.
PROTECTED_CLASS_COLUMNS = (
    "synthetic_sex",
    "synthetic_race_ethnicity",
    "synthetic_age_band",
)


class PackageIntegrityError(RuntimeError):
    """A package file does not match the checksum the client shipped.

    Fail closed. There is no repair path here on purpose: the client's
    precedence policy says an unresolved conflict escalates and is not resolved
    by paraphrasing or nearest-match, and silently continuing against altered
    bytes is a worse version of the same mistake.
    """


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checksum_report(package_dir: pathlib.Path | None = None) -> dict:
    """Verify every file listed in the client's `SHA256SUMS.txt`.

    Returns a report rather than raising, so a test can assert on the whole
    picture -- mismatches AND files present in the tree that the client never
    listed, which a plain `sha256sum -c` would not notice.
    """
    root = pathlib.Path(package_dir or PACKAGE_DIR)
    sums = root / "SHA256SUMS.txt"
    if not sums.is_file():
        raise PackageIntegrityError(f"{sums} is missing; the package cannot be verified")

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
        "package_dir": str(root),
        "listed": len(listed),
        "verified": len(verified),
        "mismatched": mismatched,
        "missing": missing,
        "unlisted": unlisted,
        "ok": not mismatched and not missing and not unlisted,
    }


def require_intact(package_dir: pathlib.Path | None = None) -> dict:
    report = checksum_report(package_dir)
    if not report["ok"]:
        raise PackageIntegrityError(
            "the client governance package does not match the checksums it "
            "shipped with, so it is no longer client input:\n"
            f"  mismatched: {report['mismatched']}\n"
            f"  missing:    {report['missing']}\n"
            f"  unlisted:   {report['unlisted']}"
        )
    return report


def _read(relative: str, package_dir: pathlib.Path | None = None) -> str:
    root = pathlib.Path(package_dir or PACKAGE_DIR)
    return (root / relative).read_text(encoding="utf-8")


def load_taxonomy(package_dir: pathlib.Path | None = None) -> dict:
    """{reason_code: entry} from the client's synthetic vendor taxonomy."""
    rows = json.loads(_read("vendor/reason-code-taxonomy.json", package_dir))
    return {r["reason_code"]: r for r in rows}


def load_wording(package_dir: pathlib.Path | None = None) -> dict:
    """{approved_wording_id: entry} from the client's approved wording table."""
    rows = json.loads(_read("vendor/approved-consumer-wording.json", package_dir))
    return {r["approved_wording_id"]: r for r in rows}


def load_fairness_fixture(package_dir: pathlib.Path | None = None) -> list[dict]:
    """The 32 audit-only rows.

    Callers get whole rows because the aggregation needs the label columns. What
    they must not do is emit one -- `fairness.py` aggregates and never returns a
    row, and `db/tests/test_offline_fairness_eval.py` asserts no fixture row id
    reaches the output.
    """
    text = _read("fixtures/synthetic-offline-fairness-evaluation.csv", package_dir)
    return list(csv.DictReader(io.StringIO(text)))


def load_acceptance_evaluations(package_dir: pathlib.Path | None = None) -> list[dict]:
    """The client's 28 acceptance cases, in file order."""
    text = _read("evaluations/governance-acceptance-evaluations.jsonl", package_dir)
    return [json.loads(line) for line in text.splitlines() if line.strip()]
