"""ADR 0010 and 0011 must describe ONE executable sequence.

The review's central finding was not a typo. Three separate statements in 0010
were each individually reasonable and could not all be true at once:

  * `balances` is written by the projection and nothing else;
  * `adjust_balance` and `waive_fee` keep writing it directly until ADR 0011;
  * the write-guard is not in the minimum slice.

An implementer following the first would attach the guard while two writers were
still bypassing it, and every staff adjustment and waiver would start raising in
production. The label drift compounded it -- `PR-5` named both the write-guard
step and the payment waterfall, and gates pointed at the wrong PR.

Prose cannot be trusted to stay consistent across a thousand-line document
through successive edits, so the sequence is asserted here instead. These tests
read the ADRs as text on purpose: the defect is a documentation defect, and the
document is the artifact an implementer follows.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
A10 = REPO / "adr" / "0010-append-only-ledger-for-servicing-balances.md"
A11 = REPO / "adr" / "0011-maker-checker-for-servicing-adjustments.md"

pytestmark = pytest.mark.skipif(
    not A10.is_file(), reason="ADR 0010 not on this branch"
)

# The one authoritative order. Maker-checker (PR-4) precedes the staff-path
# conversion and the guard (PR-5), because converting adjust/waive first would
# write unapproved staff money movements into an append-only table.
SEQUENCE = {
    1: "the failing lost-update test",
    2: "ledger schema, triggers, back-fill",
    3: "machine write paths convert",
    4: "maker-checker (ADR 0011)",
    5: "staff paths convert, then the write-guard",
    6: "payment waterfall",
}


def _text(path):
    return path.read_text(encoding="utf-8")


def test_no_adr_cites_a_bare_debt_md():
    """The file is `docs/DEBT.md`. A citation that does not resolve is a broken
    claim, and this repository has a test saying so."""
    for path in (A10, A11):
        bare = re.findall(r"(?<!docs/)\bDEBT\.md\b", _text(path))
        assert not bare, f"{path.name} cites a bare DEBT.md {len(bare)} time(s)"


def test_the_invariant_table_is_numbered_by_invariant():
    """The `#` column held PR-1..PR-5, which made the invariants unciteable and
    hid that invariant 3 lands later than the rest."""
    text = _text(A10)
    start = text.index("| # | Invariant | Lands in")
    table = text[start:text.index("\n\n", start)]
    rows = [r for r in table.splitlines() if r.startswith("|") and "---" not in r][1:]

    numbers = [r.split("|")[1].strip().strip("*") for r in rows]
    assert numbers == [str(n) for n in range(1, 8)], (
        f"the invariant table's # column reads {numbers}; it must be 1..7 with "
        "PR placement kept in `Lands in`"
    )


def test_the_only_writer_invariant_lands_after_the_staff_paths_convert():
    """Invariant 3 is the one that cannot be claimed early.

    It is true only once all five writers are converted AND the guard is on --
    which is PR-5, not PR-3.
    """
    text = _text(A10)
    row = next(
        line for line in text.splitlines()
        if line.startswith("| 3 |") and "only by the projection" in line
    )
    lands_in = row.split("|")[3]
    assert "PR-5" in lands_in, (
        f"invariant 3 claims to land in {lands_in.strip()!r}. It cannot be true "
        "until adjust_balance and waive_fee are converted, which is PR-5."
    )


def test_maker_checker_precedes_the_staff_path_conversion():
    """The dependency the whole sequence turns on."""
    text = _text(A10)
    names_pr4 = re.search(r"PR-4[^\n]*maker-checker", text, re.I) or \
        re.search(r"maker-checker[^\n]*PR-4", text, re.I)
    assert names_pr4, "PR-4 is not identified as maker-checker"
    assert re.search(r"PR-5[^\n]*(guard|adjust_balance|waive_fee)", text, re.I), (
        "PR-5 is not identified as the staff-path conversion and guard step"
    )


def test_no_pr_label_names_two_different_things():
    """`PR-5` named both the write-guard step and the payment waterfall.

    An implementer reading one table and building from the other builds the
    wrong thing, which is exactly what the review reported.
    """
    text = _text(A10)
    waterfall_labels = set(
        re.findall(r"\|\s*\*\*(PR-\d)\*\*\s*\|[^|]*payment waterfall", text, re.I)
    )
    guard_labels = set(
        re.findall(r"\*\*(PR-\d)\*\*[^.\n]{0,80}write-guard", text, re.I)
    )
    overlap = waterfall_labels & guard_labels
    assert not overlap, (
        f"{sorted(overlap)} labels both the payment waterfall and the "
        "write-guard step"
    )


def test_the_gates_point_at_the_first_ledger_write():
    """G1 and G2 make the mixed deploy safe, so they must gate the step that
    first writes the ledger. That is PR-3; the document said PR-4."""
    text = _text(A10)
    g1 = next(line for line in text.splitlines() if line.startswith("| **G1**"))
    g2 = next(line for line in text.splitlines() if line.startswith("| **G2**"))
    for label, line in (("G1", g1), ("G2", g2)):
        assert "PR-3" in line, (
            f"{label} gates {line.split('|')[2].strip()!r}, but the first ledger "
            "write is PR-3"
        )


def test_the_minimum_slice_does_not_claim_all_five_writers():
    """PR-1..PR-3 is the stated obligation, and it cannot include the staff
    paths -- those need ADR 0011 first."""
    text = _text(A10)
    start = text.index("## The minimum slice that is ADR-compliant")
    section = text[start:start + 3000]
    assert "excluded" in section and "adjust_balance" in section, (
        "the minimum slice does not say that adjust_balance and waive_fee are "
        "excluded from it"
    )
    assert "**All five writers**" not in section


def test_both_adrs_agree_on_the_dependency_direction():
    """0010 needs 0011 before its last step; 0011 must not claim the reverse."""
    a10, a11 = _text(A10), _text(A11)
    assert re.search(r"ADR 0011", a10), "0010 never names its dependency"
    assert re.search(r"0010", a11), "0011 never references the ledger ADR"
    # 0011 must not present itself as blocked by the guard it precedes.
    assert not re.search(r"0011[^.\n]{0,60}requires[^.\n]{0,40}write-guard", a11, re.I), (
        "ADR 0011 claims to depend on the write-guard, reversing the order 0010 "
        "specifies"
    )


def test_the_sequence_is_not_vacuous():
    """Every step 1-6 is actually named somewhere in the document."""
    text = _text(A10)
    for n in SEQUENCE:
        assert f"PR-{n}" in text, f"PR-{n} ({SEQUENCE[n]}) is not in the ADR"


def test_no_pr_label_names_the_waterfall_outside_a_table_row():
    """`PR 5` with a space slipped past the table-row check.

    The non-goals table said the waterfall is PR-6 while the paragraph under it
    said "the algorithm is PR 5". A reader can still end up assigning PR-5 to
    both the write-guard step and the waterfall -- the exact drift these tests
    exist to stop, surviving in prose because the first version of this check
    only looked at `| **PR-n** |` table cells.
    """
    text = _text(A10)
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            continue                     # table rows are covered above
        if re.search(r"waterfall|D14", line, re.I) and re.search(r"\bPR[-\s]5\b", line):
            raise AssertionError(
                f"the payment waterfall is called PR-5 in prose: {line.strip()!r}"
            )


def test_the_pr3_gate_does_not_require_the_pr5_invariant():
    """PR-3's gate cannot demand zero direct writers.

    `adjust_balance` and `waive_fee` are still direct writers until PR-5, by
    this ADR's own sequence. An implementer following a zero-direct-writer gate
    at PR-3 would either block PR-3 for ever or convert the staff paths before
    an approval path exists -- reintroducing the unapproved append-only movement
    the whole ordering is designed to prevent.
    """
    text = _text(A10)
    row = next(
        line for line in text.splitlines()
        if line.startswith("| **PR-3** |") and "grep 'UPDATE balances'" in line
    )
    assert "staff paths" in row or "adjust_balance" in row, (
        "the PR-3 gate demands that grep returns ONLY the projection, which is "
        "the PR-5 invariant. It must allow the two staff writers that this ADR "
        "says are still direct until PR-5."
    )
    assert "PR-5" in row, "the PR-3 gate does not say where the strict check belongs"


def test_the_migration_plan_lists_every_step_from_1_to_6():
    """A step named in the prose but absent from the plan cannot be executed.

    The tables jumped PR-4 -> PR-6, so PR-5 -- the step where the money-table
    write boundary is actually enforced -- had no row, no gate and no
    deliverable, while the surrounding text kept referring to it.
    """
    text = _text(A10)
    for table_header in ("| Step | ", "| PR | "):
        pass
    rows = [l for l in text.splitlines() if l.startswith("| **PR-")]
    assert rows, "no PR rows found at all"
    labels = [re.match(r"\| \*\*(PR-\d)\*\*", r).group(1) for r in rows]
    for n in range(1, 7):
        assert f"PR-{n}" in labels, (
            f"PR-{n} is described in the text but has no row in any plan table"
        )


def test_no_adr_says_0011_ships_approved_required_or_approved_at():
    """0010 said ADR 0011 adds `pending_movement_id`, `approved_required` and
    `approved_at`; 0011 says the last two exist in neither.

    Approval state lives on the proposal precisely so no denormalised copy on
    `ledger_entries` can drift. An implementer following the contradiction would
    build the duplicated state both ADRs reject.
    """
    for path in (A10, A11):
        text = _text(path)
        for line in text.splitlines():
            if re.search(r"\b0011\b", line) and re.search(r"adds all three", line):
                raise AssertionError(f"{path.name}: {line.strip()!r}")


def test_a_resolved_proposal_can_still_gain_its_ledger_entry_link():
    """ADR 0011's own approval order requires one post-resolution UPDATE.

    The transition trigger refused every UPDATE once `resolution` was set, so
    step 3 -- writing `ledger_entry_id` back -- would raise and no staff
    adjustment could ever complete. The trigger must allow that one write.
    """
    text = _text(A11)
    fn_start = text.index("CREATE FUNCTION pending_movements_single_transition")
    fn = text[fn_start:text.index("$$ LANGUAGE plpgsql;", fn_start)]
    assert "ledger_entry_id" in fn, (
        "the transition trigger never mentions ledger_entry_id, so it refuses "
        "the linkage its own approval sequence requires"
    )
    assert "OLD.ledger_entry_id IS NOT NULL" in fn, (
        "the trigger does not prevent the link being overwritten once set"
    )
