"""The client package in this repository must still be the client's bytes.

The package arrived as an email attachment on 2026-08-24 with its own
`SHA256SUMS.txt`. Ingesting it into git is the moment those checksums stop being
self-evidently true: git rewrites line endings on checkout unless told not to,
and `core.autocrlf=true` is the Windows default. Measured, not assumed — without
the `-text` rule in `.gitattributes`, a Windows checkout of this directory fails
**all 34** checksums, and fails them silently, because nothing else in the
repository reads these files byte-for-byte.

That matters beyond tidiness. The package's whole standing is "this is what the
client sent". A file altered after ingestion — by a helpful reformat, an editor
stripping trailing whitespace, or git's own newline conversion — is no longer
client input. It is repository-authored material wearing the client's name,
which is the exact failure their own README warns about when it says a real
vendor document must replace this packet rather than being merged into it.

So this test is the mechanical version of that warning.
"""
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "db" / "tools"
PACKAGE = REPO / "fixtures" / "offline_fairness_training" / "client_package_2026-08-24"

sys.path.insert(0, str(TOOLS))

from client_governance_package import (  # noqa: E402
    PACKAGE_VERSION,
    PackageIntegrityError,
    checksum_report,
    require_intact,
)


def test_the_package_is_present():
    assert PACKAGE.is_dir(), (
        f"{PACKAGE.relative_to(REPO)} is missing. The client's package is the "
        f"authority every offline tool here reads; without it there is nothing "
        f"to evaluate and the Week 8 fairness evaluation is blocked again.")


def test_every_checksum_the_client_shipped_still_verifies():
    """The whole package, against the client's own manifest."""
    report = checksum_report()

    assert report["mismatched"] == [], (
        "these files no longer match the checksum the client shipped, so they "
        "are no longer client input:\n  " + "\n  ".join(report["mismatched"]))
    assert report["missing"] == [], (
        f"files listed in SHA256SUMS.txt are absent: {report['missing']}")
    assert report["unlisted"] == [], (
        "files exist in the package directory that the client never listed: "
        f"{report['unlisted']}. Repository-authored material does not belong "
        "inside the client package; put it beside the package instead.")
    assert report["verified"] == 34, (
        f"expected 34 verified files, got {report['verified']}")


def test_the_gitattributes_rule_that_makes_that_possible_is_present():
    """Guard the guard.

    Without this rule the test above passes on a developer's working tree and
    fails for anyone who clones on Windows — the worst shape a provenance check
    can have, because it is green exactly where it is not needed.
    """
    rules = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "client_package_2026-08-24/** -text" in rules, (
        ".gitattributes no longer disables end-of-line conversion for the "
        "client package. With core.autocrlf=true (the Windows default) every "
        "checksum in SHA256SUMS.txt fails on checkout.")


def test_git_stores_the_package_without_newline_conversion():
    """Ask git directly, rather than trusting that the rule reads correctly.

    `check-attr` reports the attribute git will actually apply, which is the
    thing that matters — a rule can be present and still be overridden by a
    later pattern.
    """
    sample = PACKAGE / "vendor" / "reason-code-taxonomy.json"
    out = subprocess.run(
        ["git", "check-attr", "text", "--", str(sample.relative_to(REPO).as_posix())],
        cwd=REPO, capture_output=True, text=True, check=True).stdout

    assert "text: unset" in out, (
        f"git will apply end-of-line conversion to the client package: {out.strip()!r}")


def test_a_changed_package_fails_closed():
    """`require_intact` must raise, not warn.

    Every tool here calls it before reading, so the behaviour on a modified
    package is the behaviour of the whole offline toolchain.
    """
    report = checksum_report()
    assert report["ok"]

    with pytest.raises(PackageIntegrityError):
        require_intact(REPO / "fixtures" / "offline_fairness_training")


def test_the_package_declares_the_version_the_tools_expect():
    """A silent version drift would let a tool read a later package while
    reporting the earlier version's name on its output."""
    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
    inventory = (PACKAGE / "PACKAGE-INVENTORY.txt").read_text(encoding="utf-8")

    assert PACKAGE_VERSION in readme, (
        f"the package README does not declare {PACKAGE_VERSION}")
    assert PACKAGE_VERSION in inventory, (
        f"PACKAGE-INVENTORY.txt does not declare {PACKAGE_VERSION}")


def test_the_repository_wrapper_is_separate_from_client_files():
    """Repo-authored text must never sit inside the client's directory.

    The provenance README lives one level up, beside the package rather than in
    it, so that `SHA256SUMS.txt` continues to cover exactly the client's files
    and a reader can tell authorship from location alone.
    """
    wrapper = PACKAGE.parent / "README.md"
    assert wrapper.is_file()

    listed = {line.split("  ", 1)[1].strip()
              for line in (PACKAGE / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
              if line.strip()}
    assert "README.md" in listed, "the client's own README should be checksummed"

    # The wrapper is outside the checksummed set precisely because this
    # repository wrote it.
    assert not (PACKAGE / "PROVENANCE.md").exists(), (
        "a repository-authored provenance file was placed inside the client "
        "package directory; it belongs beside the package, not within it")
