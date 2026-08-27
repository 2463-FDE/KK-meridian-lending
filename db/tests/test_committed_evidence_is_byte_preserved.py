"""Committed telemetry evidence must still be the bytes that were captured.

`docs/evidence/**` holds trace payloads that a reader is invited to re-run a
privacy search against, and whose byte counts are quoted in
`docs/presentations/2026-08-25-agentic-client-handoff.md` §3a. Both of those
uses assume the file on disk is what the exporter posted.

That assumption broke once already, silently. The first version of §3a committed
one payload with no `-text` rule: 17,443 bytes on disk became 17,232 in the blob
because `core.autocrlf=true` (the Windows default) rewrote the line endings. The
document's figures were wrong for anyone who cloned, and the fourteen-class
privacy search would have run over different bytes than the ones captured.
Review caught it; nothing in the repository would have.

**Scope, per the engineering evidence rule.** Only `*.bin` is byte-pinned.
Markdown, JSON, CSV and plain-text evidence keep normal Git text behaviour --
a blanket `docs/evidence/** -text` would pin files nobody has written yet on a
guess about what they need.
"""
import hashlib
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "docs" / "evidence"


def _payloads():
    return sorted(EVIDENCE.rglob("*.bin")) if EVIDENCE.is_dir() else []


def _check_attr(path: str) -> str:
    out = subprocess.run(["git", "check-attr", "text", "--", path],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout
    return out.rsplit(": ", 1)[-1].strip()


def test_the_rule_pins_bin_files_at_any_depth():
    """Future-safe, and checked as such.

    `git check-attr` answers for paths that do not exist yet, so the rule can be
    verified against the directory the next evidence capture will use rather
    than only against today's.
    """
    for future in ("docs/evidence/2026-09-04-handoff/trace.bin",
                   "docs/evidence/future/deeply/nested/run/payload.bin"):
        assert _check_attr(future) == "unset", (
            f"{future} would be subject to end-of-line conversion; a payload "
            f"committed there would not be the bytes that were captured")


def test_the_rule_leaves_text_evidence_alone():
    """Deliberately narrow. Pinning every future file would be a guess."""
    for text_file in ("docs/evidence/2026-09-04-handoff/notes.md",
                      "docs/evidence/x/y/data.json"):
        assert _check_attr(text_file) == "unspecified", (
            f"{text_file} is byte-pinned; only *.bin carries that requirement")


def test_the_rule_does_not_reach_outside_evidence():
    assert _check_attr("docs/other/thing.bin") == "unspecified"


@pytest.mark.skipif(not _payloads(), reason="no committed .bin evidence yet")
def test_every_committed_payload_matches_its_blob():
    """The check that would have caught the original failure.

    Compares each file's size on disk against the size git stored. They differ
    exactly when a conversion happened, which is the whole failure mode.
    """
    mismatched = []
    for path in _payloads():
        rel = path.relative_to(REPO).as_posix()
        entry = subprocess.run(["git", "ls-files", "-s", "--", rel],
                               cwd=REPO, capture_output=True, text=True, check=True).stdout
        if not entry.strip():
            continue  # not staged or committed yet
        blob = entry.split()[1]
        stored = int(subprocess.run(["git", "cat-file", "-s", blob], cwd=REPO,
                                    capture_output=True, text=True, check=True).stdout)
        on_disk = path.stat().st_size
        if stored != on_disk:
            mismatched.append(f"{rel}: disk={on_disk} blob={stored}")

    assert mismatched == [], (
        "committed evidence does not match what git stored, so it is no longer "
        "the captured bytes:\n  " + "\n  ".join(mismatched))


@pytest.mark.skipif(not _payloads(), reason="no committed .bin evidence yet")
def test_committed_payloads_carry_no_sensitive_content():
    """The engineering evidence rule, enforced rather than promised.

    Allowlisted categorical and provenance fields only. No prompts, responses,
    retrieved text, applicant data, raw financial values, credentials, tokens,
    or card data. This is an engineering hygiene rule for a synthetic training
    engagement -- it is not a regulatory or legal retention policy, and no
    retention period is asserted anywhere in this repository.
    """
    import re

    banned = [
        ("prompt/response content",
         r'"(inputs|outputs|messages|prompt|response|completion|content|text)"\s*:'),
        ("SSN shape", r"\b\d{3}-\d{2}-\d{4}\b"),
        ("PAN shape", r"\b(?:\d[ -]?){13,19}\b"),
        ("credential", r"(ABSK|lsv2_|Bearer\s+[A-Za-z0-9._-]{20,})"),
        ("credential env name", r"(AWS_BEARER|SecretAccessKey|aws_secret)"),
        ("financial field", r'"(income|dti|debt_to_income|annual_income|balance)"\s*:'),
        ("raw provider error", r"(Traceback|botocore|ClientError|ValidationException)"),
    ]

    offenders = []
    for path in _payloads():
        body = path.read_bytes().decode("utf-8", errors="replace")
        for label, pattern in banned:
            match = re.search(pattern, body, re.I)
            if match:
                # The matched text is NOT echoed: if this fires, the point is
                # that the file should not be in the repository, and repeating
                # its contents in CI output would spread the problem.
                offenders.append(f"{path.relative_to(REPO).as_posix()}: {label}")

    assert offenders == [], (
        "committed evidence carries content the evidence rule excludes:\n  "
        + "\n  ".join(offenders))
