"""A count in a document is a factual claim, and this one was wrong by nine.

`docs/ROADMAP.md` said the browser suite was **12** spec files, "re-counted
2026-08-11". By 2026-08-24 it was 21. Nothing went wrong to make that happen --
specs were added, which is the intended direction -- and the sentence quietly
stopped being true.

The paragraph containing it complains, in its own aside, about exactly this class
of stale figure. That is what makes it worth a test rather than another
correction: a number that has to be maintained by remembering will be wrong
again, and the roadmap cites this file so a reader knows the figure is recounted
rather than recalled.

**"Re-counted <date>" is provenance, not a fence.** It says who last looked; it
does not say the figure has expired. So it does not exempt the claim -- which is
the judgement call this file makes explicit rather than leaving to whoever reads
the paragraph next.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
ROADMAP = REPO / "docs" / "ROADMAP.md"
E2E = REPO / "frontend" / "e2e"


def _claimed_count() -> int:
    text = ROADMAP.read_text(encoding="utf-8")
    match = re.search(
        r"\*\*Browser end-to-end\*\*\s*\(`frontend/e2e/`,\s*\*\*(\d+)\*\*\s*spec files",
        text)
    assert match, (
        "the roadmap's browser-suite sentence no longer states a spec-file count "
        "in the shape this test reads. If the count was removed deliberately, "
        "delete this test with it -- a guard that cannot find its subject passes "
        "for the wrong reason")
    return int(match.group(1))


def _actual_count() -> int:
    return len(list(E2E.glob("*.spec.ts")))


def test_the_roadmap_states_the_number_of_spec_files_there_are():
    claimed, actual = _claimed_count(), _actual_count()

    assert claimed == actual, (
        "docs/ROADMAP.md claims %d browser spec files; there are %d. This is not "
        "a defect in the suite -- specs were added, which is the point -- it is a "
        "count that has to be maintained by remembering, and was not. Update the "
        "figure:\n%s"
        % (claimed, actual,
           "\n".join(sorted(p.name for p in E2E.glob("*.spec.ts")))))


def test_the_count_is_of_spec_files_not_of_tests():
    """Guard the guard. `*.spec.ts` files, not `test(...)` blocks -- two very
    different numbers, and a future reader correcting the sentence should know
    which one it claims."""
    assert _actual_count() < sum(
        path.read_text(encoding="utf-8", errors="replace").count("test(")
        for path in E2E.glob("*.spec.ts")), (
        "there are no more test blocks than spec files, so the two figures are "
        "indistinguishable and this test cannot tell which the roadmap means")
