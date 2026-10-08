"""The governance fixture must match its manifest, byte for byte.

Every offline governance tool reads `fixtures/governance/` through
`db/tools/governance_fixture.py`, which verifies `SHA256SUMS.txt` first and
fails closed. These tests keep that guarantee honest: the manifest covers every
file, a changed or extra file is refused, and git does not rewrite the bytes on
checkout.

That last point is easy to miss. `core.autocrlf=true` is the Windows default,
and without the `-text` rule in `.gitattributes` a Windows checkout would fail
every checksum -- and fail silently, because nothing else in the repository
reads these files byte-for-byte.
"""
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "db" / "tools"
FIXTURE = REPO / "fixtures" / "governance"

sys.path.insert(0, str(TOOLS))

from governance_fixture import (  # noqa: E402
    FIXTURE_DIR,
    FIXTURE_VERSION,
    FixtureIntegrityError,
    checksum_report,
    require_intact,
)

#: Every file in the fixture except the manifest itself.
EXPECTED_FILES = 25


def test_the_fixture_is_where_the_tools_read_it():
    assert FIXTURE.is_dir(), f"{FIXTURE.relative_to(REPO)} is missing"
    assert FIXTURE_DIR == FIXTURE


def test_every_checksum_verifies():
    report = checksum_report()

    assert report["mismatched"] == [], (
        "these files changed without SHA256SUMS.txt being regenerated:\n  "
        + "\n  ".join(report["mismatched"]))
    assert report["missing"] == [], (
        f"files listed in SHA256SUMS.txt are absent: {report['missing']}")
    assert report["unlisted"] == [], (
        f"files exist in the fixture that the manifest does not list: {report['unlisted']}")
    assert report["verified"] == EXPECTED_FILES, (
        f"expected {EXPECTED_FILES} verified files, got {report['verified']}")


def test_the_gitattributes_rule_that_makes_that_possible_is_present():
    """Guard the guard: without this rule the checksums pass on a developer's
    working tree and fail for anyone who clones on Windows."""
    rules = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "fixtures/governance/** -text" in rules


def test_git_stores_the_fixture_without_newline_conversion():
    """Ask git directly: a rule can be present and still be overridden by a
    later pattern."""
    sample = FIXTURE / "vendor" / "reason-code-taxonomy.json"
    out = subprocess.run(
        ["git", "check-attr", "text", "--", sample.relative_to(REPO).as_posix()],
        cwd=REPO, capture_output=True, text=True, check=True).stdout

    assert "text: unset" in out, (
        f"git will apply end-of-line conversion to the fixture: {out.strip()!r}")


def test_a_changed_fixture_fails_closed(tmp_path):
    """`require_intact` must raise, not warn. Every tool calls it before reading,
    so this is the behaviour of the whole offline toolchain."""
    import shutil

    assert checksum_report()["ok"]

    edited = tmp_path / "edited"
    shutil.copytree(FIXTURE, edited)
    taxonomy = edited / "vendor" / "reason-code-taxonomy.json"
    taxonomy.write_bytes(taxonomy.read_bytes() + b"\n")
    with pytest.raises(FixtureIntegrityError):
        require_intact(edited)

    extra = tmp_path / "extra"
    shutil.copytree(FIXTURE, extra)
    (extra / "vendor" / "unreviewed.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FixtureIntegrityError):
        require_intact(extra)


def test_the_fixture_declares_the_version_the_tools_expect():
    """A silent version drift would let a tool read a later fixture while
    reporting the earlier version on its output."""
    for name in ("README.md", "evaluations/README.md"):
        text = (FIXTURE / name).read_text(encoding="utf-8")
        assert FIXTURE_VERSION in text, f"{name} does not declare {FIXTURE_VERSION}"
