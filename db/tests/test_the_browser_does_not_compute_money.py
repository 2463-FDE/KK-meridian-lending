"""No TILA figure is calculated in the browser.

Every amount in the federal disclosure box -- the APR, the finance charge, the
amount financed, the total of payments, and now the requested principal and the
origination fee that make the amount financed foot -- is computed by the service
that owns it and sent to the page. The page formats and displays.

**Why this is a test rather than a convention.** The origination fee has already
existed as three different numbers in three different files (`docs/DEBT.md` D6:
`fees.py` said 0.030, `apr.py` said 0.025), and the note rate as six (PR #80).
Each copy was added by someone who needed the figure where they were standing.
A copy in the browser is the worst of them: it is the surface a borrower reads,
it is the furthest from any authority, and a disagreement shows up as a
disclosure box whose own numbers do not add up.

**The specific temptation this was written for.** The amount-financed breakdown
is three numbers where `requested - fee = financed`. Given two of them, the
third is one line of TypeScript. That line would be correct for most principals
and wrong by a cent for the ones the rounding rule exists for -- `amount_financed`
stores the ROUNDED DIFFERENCE, so on a $1,002.50 principal the stored fee is
$30.07 while `1002.50 * 0.03` rounds to $30.08.

What is deliberately NOT flagged: formatting (`toFixed`, `usd()`), comparison,
array indexing, and layout arithmetic. Those are presentation. What is flagged is
a monetary VALUE derived from other monetary values.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

#: The screens that render the TILA box and the offer terms.
DISCLOSURE_SURFACES = (
    "frontend/app/apply/page.tsx",
    "frontend/app/underwriting/[appId]/page.tsx",
)

#: Field names whose values are money owned by a service. A line combining two of
#: them arithmetically is deriving a disclosure figure in the browser.
MONEY_FIELDS = (
    "amount_financed", "requested_principal", "origination_fee",
    "finance_charge", "total_of_payments", "monthly_payment", "final_payment",
    "apr", "note_rate_pct",
)

#: `-` alone is far too common in TSX (JSX, negative indices, CSS-in-JS), so the
#: pattern requires a money field on one side of an arithmetic operator and
#: something numeric on the other.
_FIELD = r"(?:disclosure|offer|d|breakdown)\s*[.?]\s*(?:%s)" % "|".join(MONEY_FIELDS)
_ARITHMETIC = re.compile(
    r"(?:%s)\s*[-+*/]\s*[\w.(]|[\w.)]\s*[-+*/]\s*(?:%s)" % (_FIELD, _FIELD))

#: Lines that are comments, and the entity-escape for a minus SIGN, which is a
#: label rather than an operation: `&minus;{usd(fee)}` prints "-$270.00" and
#: computes nothing.
_COMMENT = re.compile(r"^\s*(?://|/\*|\*)")


def _label(path: pathlib.Path) -> str:
    """A repo-relative path when it is in the repo, the name otherwise.

    The two self-checks below plant files under `tmp_path`, which
    `relative_to(REPO)` refuses -- so the first version of this helper raised
    ValueError inside the test that proves the pattern works.
    """
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return path.name


def _offending_lines(path: pathlib.Path) -> list[str]:
    out = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _COMMENT.match(line):
            continue
        if _ARITHMETIC.search(line):
            out.append("%s:%d %s" % (_label(path), lineno, line.strip()[:120]))
    return out


@pytest.mark.parametrize("surface", DISCLOSURE_SURFACES)
def test_no_disclosure_figure_is_derived_in_the_browser(surface):
    path = REPO / surface
    assert path.exists(), "%s no longer exists; update DISCLOSURE_SURFACES" % surface

    offenders = _offending_lines(path)

    assert not offenders, (
        "a TILA figure is computed in the browser:\n" + "\n".join(offenders)
        + "\n\nEvery amount in the disclosure box is calculated by the service "
          "that owns it. The origination fee has already been three different "
          "numbers in three files (D6) and the note rate six (PR #80); a copy in "
          "the browser is the one a borrower reads, and a disagreement renders as "
          "a disclosure box whose own figures do not add up")


def test_the_pattern_catches_the_line_it_was_written_for(tmp_path):
    """Guard the guard.

    A regex over TSX is easy to write so loosely that it matches nothing. This
    plants the exact line the test exists to refuse -- deriving the fee from the
    other two figures -- and confirms it is caught, so a future edit that
    weakens the pattern fails here instead of silently passing everything.
    """
    planted = tmp_path / "page.tsx"
    planted.write_text(
        "const fee = disclosure.requested_principal - disclosure.amount_financed;\n",
        encoding="utf-8")

    assert _offending_lines(planted), (
        "the pattern does not catch a fee derived from two disclosure figures, "
        "which is the single line this whole test exists to refuse")


def test_formatting_and_display_are_not_flagged(tmp_path):
    """The other half of guarding the guard: a test that flags `usd(...)` would
    be turned off within a week, and then it protects nothing."""
    allowed = tmp_path / "page.tsx"
    allowed.write_text(
        "\n".join([
            "<dd>{usd(disclosure.amount_financed)}</dd>",
            "<dd>&minus;{usd(breakdown.origination_fee)}</dd>",
            "{pct(disclosure.apr)}",
            "if (disclosure.origination_fee != null) { show(); }",
            "const rows = disclosure.schedule.slice(0, 12);",
            "<span>{disclosure.term_months} months</span>",
        ]) + "\n",
        encoding="utf-8")

    assert _offending_lines(allowed) == [], (
        "the pattern flags formatting or comparison, which is presentation and "
        "must stay allowed")
