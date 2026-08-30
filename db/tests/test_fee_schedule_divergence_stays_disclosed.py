"""The published late-fee rule must not be readable without its divergence note.

`policies/fee_schedule.md` publishes a late-fee rule the code does not
implement, and that is a decided, recorded position rather than an oversight:
the client settled the rule on 2026-08-29, and implementing it needs
installment-level facts this schema does not persist (`docs/DEBT.md` D23). The
file discloses the gap in the same table row as the rule, and again in a section
of its own.

**That disclosure is the entire control.** `POST /lss/accounts/{id}/late-fee`
still charges the older published rule, and this file is served to Policy Chat,
so if the caveat is ever edited away the system publishes one rule and charges
another with nothing left to say so. The endpoint was evaluated for being failed
closed instead and deliberately kept: it has no UI, no scheduler and no
automated caller, every fee lands on the immutable ledger and is reversible by
waiver, and refusing at the route would make two exception mappings added after
review unreachable -- replacing a disclosed divergence with dead code and tests
asserting behaviour nothing can reach.

So the disclosure is checked instead of assumed, and it is checked in the
direction that matters: **the published rule may not appear without the caveat,
and the caveat must state the figures the code actually uses.**

DERIVED, NOT HAND-LISTED. The figures come from `delinquency.py` itself. A guard
that repeated `$35` as its own constant would keep passing after the code
changed, which is the failure it exists to prevent -- and this repository has
already produced tests that were green for exactly that reason.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCHEDULE = REPO / "policies" / "fee_schedule.md"
DELINQUENCY = REPO / "services" / "servicing-service" / "app" / "delinquency.py"
DEBT = REPO / "docs" / "DEBT.md"

SECTION_HEADING = "### Current implementation differs — late payment fee"


def _schedule() -> str:
    return SCHEDULE.read_text(encoding="utf-8")


def _constant(name: str) -> str:
    """The Decimal literal `delinquency.py` assigns to `name`."""
    source = DELINQUENCY.read_text(encoding="utf-8")
    match = re.search(rf'^{name}\s*=\s*Decimal\("([0-9.]+)"\)', source, re.M)
    assert match, f"{name} is no longer a module-level Decimal in delinquency.py"
    return match.group(1)


def _rule_row() -> str:
    """The 'Late payment fee' row of the published fee table."""
    for line in _schedule().splitlines():
        if line.startswith("| Late payment fee"):
            return line
    pytest.fail("the fee table no longer has a 'Late payment fee' row")


def _divergence_section() -> str:
    text = _schedule()
    start = text.find(SECTION_HEADING)
    assert start != -1, (
        f"{SCHEDULE.name} no longer contains {SECTION_HEADING!r}. The published "
        "rule is not implemented; removing the section that says so leaves the "
        "file claiming a rule the code does not apply."
    )
    rest = text[start + len(SECTION_HEADING):]
    # To the next heading of the same or higher level, so the section's own
    # subheadings stay inside it.
    end = re.search(r"^#{1,3} ", rest, re.M)
    return rest[: end.start()] if end else rest


def test_the_published_rule_carries_a_pointer_to_the_divergence():
    """A reader must not be able to take the row as a description of the code.

    The row is what Policy Chat retrieves and what a client reads. It states the
    decided rule, so on its own it would describe behaviour that does not exist.
    """
    row = _rule_row()
    assert "does not yet implement" in row, (
        "the published late-fee row no longer says the code does not implement "
        f"it:\n{row}"
    )
    assert "Current implementation differs" in row, (
        "the published late-fee row no longer points at the section that "
        f"explains what the code does instead:\n{row}"
    )


def test_the_divergence_section_states_the_figures_the_code_uses():
    """Derived from `delinquency.py`, so changing the code fails this."""
    section = _divergence_section()

    flat = _constant("LATE_FEE_FLAT")            # e.g. "35.00"
    pct = _constant("LATE_FEE_PCT_OF_PAST_DUE")  # e.g. "0.05"

    # "$35" and "$35.00" are both fair renderings of the same figure; the whole
    # dollars are what must match, not the formatting.
    whole_dollars = flat.split(".")[0]
    assert re.search(rf"\${whole_dollars}\b", section), (
        f"the divergence section does not state the flat fee the code charges "
        f"(LATE_FEE_FLAT = {flat})"
    )

    percent = f"{float(pct) * 100:g}%"           # "0.05" -> "5%"
    assert percent in section, (
        f"the divergence section does not state the percentage the code applies "
        f"(LATE_FEE_PCT_OF_PAST_DUE = {pct} -> {percent})"
    )


def test_the_divergence_section_names_the_base_the_code_actually_uses():
    """The gap is the BASE, and naming it is what makes the section useful.

    Both rules are "the lesser of $35 and five per cent", so quoting the figures
    alone would read as agreement. The code takes its percentage from
    `balances.past_due` -- one projected total that mixes principal, interest
    and every fee already assessed -- where the decided rule takes it from a
    single installment's unpaid scheduled principal and interest.
    """
    section = _divergence_section()
    assert "past_due" in section, (
        "the divergence section no longer names `past_due` as the base the code "
        "prices off; without it the section reads as though the two rules agree"
    )


def test_no_document_claims_the_decided_rule_is_implemented():
    """The register and the schedule must agree that it is not built.

    `docs/DEBT.md` D23 is the entry that records why. A document describing this
    as done would contradict a route that is still charging the older rule.
    """
    d23 = [line for line in DEBT.read_text(encoding="utf-8").splitlines()
           if line.startswith("| **D23**")]
    assert d23, "docs/DEBT.md no longer carries a D23 row"
    row = d23[0]
    assert "DATA-MODEL EXPANSION" in row.upper(), (
        "D23 no longer records that the decided late-fee rule needs a data-model "
        "expansion. If it has genuinely been implemented, this guard and the "
        "divergence section should be removed together with the old rule -- not "
        "the register row alone."
    )
