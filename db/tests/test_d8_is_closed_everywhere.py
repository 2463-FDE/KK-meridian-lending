"""No live document may describe D8's controls as missing while they exist.

D8 was "fee waiver / balance adjust is available to any authenticated user -- no
role check, no second approver, no ledger entry". All four controls landed
(PRs #33/#34/#35), `docs/DEBT.md` records it as **Fixed**, and ADR 0011 records
it as fully implemented. What did not keep up was everything else: the servicing
service's own module docstring still said it "reads no principal ... and enforces
no second approver (D8)", `ARCHITECTURE.md` called D8 partly closed with
"servicing validates no human principal" and "no second approver exists", two
`balance.py` docstrings described the retired direct writers as the live path,
and three frontend comments cited D8 while claiming the API accepts any
authenticated caller *and* that D8 was fixed -- two statements that cannot both
be current.

The specific danger is not untidiness. A reader who trusts those sentences
concludes that one person can still move a borrower's balance alone, which is the
opposite of what the code does, and it is the kind of claim that gets repeated
into a client conversation.

**History is allowed, and is the point of several of these files.** A stale
sentence passes when its own scope marks it as past -- "this docstring used to
say", "*Historical*", "until PRs #34/#35", "before maker-checker landed". What
fails is an unfenced present-tense claim that contradicts code in this
repository.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

#: Where the controls actually live. Checked first: if these move or vanish, the
#: assertions below are measuring against nothing.
PRINCIPAL = REPO / "services" / "servicing-service" / "app" / "principal.py"
MAKER_CHECKER = REPO / "services" / "servicing-service" / "app" / "maker_checker.py"
RESOLVE_MIGRATION = REPO / "db" / "migrations" / "0037_resolve_pending_movement.sql"
SERVICING_MAIN = REPO / "services" / "servicing-service" / "app" / "main.py"
BALANCE = REPO / "services" / "servicing-service" / "app" / "balance.py"
DEBT = REPO / "docs" / "DEBT.md"
ARCHITECTURE = REPO / "ARCHITECTURE.md"

#: Documents and source files that describe the servicing money path to a reader.
#:
#: Derived from one list rather than assembled per check, because review of PR #77
#: found the two ways this set goes wrong. `principal.py` was referenced by the
#: guard and left out of the scan -- and it is the module that *implements* the
#: verified human, so a reader landing there took away the exact false conclusion
#: the guard exists to prevent. `login/page.tsx` was corrected by the same PR and
#: had no protection at all, so restoring its old wording would have passed
#: clean. Anything corrected for D8 belongs here; if a file describes the money
#: path and is missing, that is the defect.
LIVE_SURFACES = [
    ARCHITECTURE,
    SERVICING_MAIN,
    BALANCE,
    PRINCIPAL,
    MAKER_CHECKER,
    REPO / "docs" / "ROADMAP.md",
    REPO / "specs" / "0002-maker-checker-self-approval.md",
    REPO / "frontend" / "lib" / "api.ts",
    REPO / "frontend" / "components" / "AppBar.tsx",
    REPO / "frontend" / "components" / "RequireRole.tsx",
    REPO / "frontend" / "app" / "login" / "page.tsx",
]

#: A scope that says out loud it is describing the past.
_HISTORICAL = re.compile(
    r"(?i)(historical|superseded|previously|used to (?:say|read|claim|be)|"
    r"\buntil PRs? #\d+|\buntil #\d+|\bbefore (?:maker-checker|PRs? #\d+)|"
    r"this (?:docstring|comment|paragraph|bullet|row|passage|cell|sentence|"
    r"section|clause) (?:read|said|claimed|used to|first described)|"
    r"stopped being true|no longer|outlived|earlier draft|"
    r"what (?:this|the) section said before|described the live path|"
    r"the difference is the work|as of \d{4}-\d{2}-\d{2}|kept because|"
    # Each week's "What client handed over" block is a handover inventory: it
    # records the state the engagement started from, and is past by
    # construction. Same allowance as the Week 8 status guard.
    r"what client handed over|"
    # Naming the defect an artefact closes is not asserting it. "Closes: D8 --
    # fee waiver / balance adjust is available to any authenticated user, with
    # no second approver" is the debt's own title, quoted by the spec that
    # closed it.
    r"\*\*closes:\*\*|\bcloses\b|\bbears on\b|the gap, tracked)")

#: Present-tense claims the code contradicts.
FALSE_CLAIMS = [
    (re.compile(r"no second approver", re.I),
     "resolve_pending_movement refuses a resolver equal to the requester"),
    (re.compile(r"validates no human principal|reads no principal|"
                r"identif(?:ies|y) no human", re.I),
     "principal.require_staff_principal verifies a gateway-signed assertion"),
    # Both shapes: "no maker-checker", and "maker-checker ... is not
    # implemented", which is how `principal.py` phrased it and which the first
    # version of this list missed (found by mutation testing).
    (re.compile(r"no maker.?checker|"
                r"maker.?checker[^.]{0,60}\bnot implemented|"
                r"\bnot implemented[^.]{0,60}maker.?checker", re.I),
     "maker_checker.propose/resolve is the live adjust-balance / waive-fee path"),
    # `D8 itself is still open` slipped past a pattern anchored on "D8 is",
    # found by mutation testing -- hence the small gap for an intervening word.
    (re.compile(r"D8\b[^.]{0,24}\bis (?:still )?(?:open|partly closed|"
                r"partially closed)|partly closed|partially closed", re.I),
     "docs/DEBT.md records D8 as Fixed"),
    (re.compile(r"one (?:person|account) (?:still )?moves? (?:a|the) balance alone|"
                r"on one person'?s say-so", re.I),
     "a proposal moves nothing; a different principal must resolve it"),
    (re.compile(r"accepts? ANY authenticated caller", re.I),
     "money routes are role-gated, principal-verified and second-approver gated"),
    (re.compile(r"server-side authz is intentionally absent", re.I),
     "gateway role rules, ownership checks and servicing's principal all exist"),
]


#: One money route genuinely does act on a single principal: `late-fee`, which
#: assesses a contractual fee rather than adjusting a balance or waiving one, and
#: which D8 was never about (D8 is "fee waiver / balance adjust"). A sentence
#: about that route may say a single person acts alone, because they do -- see
#: `principal.py::require_money_mover` and spec 0002 §8. Recognised explicitly so
#: the guard does not push an accurate docstring into a false one.
_LATE_FEE = re.compile(r"late-fee|late fee", re.I)


def _read(path: pathlib.Path) -> str:
    assert path.is_file(), f"missing: {path}"
    return path.read_text(encoding="utf-8")


#: A scope that names a gap AND its closure in the same breath is not a false
#: claim -- it is how the roadmap's gap list and this repository's corrections are
#: written ("G-MAKER-CHECKER -- no second approver | adjust-balance raises
#: proposals ... a different verified principal resolves").
_CLOSURE = re.compile(
    r"(?i)(resolve_pending_movement|maker_checker|maker-checker (?:gates|now|"
    r"landed|enforced)|no_self_approval|require_staff_principal|"
    r"different (?:verified )?(?:principal|person|member)|raises? proposals?|"
    r"raise proposals|D8 is closed|principal-verified|second approver (?:has )?"
    r"(?:since )?landed|second person|"
    # A finding column that names the old state beside a status column saying
    # "Fixed" is the roadmap's normal shape, and the row as a whole is true.
    r"✅|\bFixed\b|re-verifies|rejects non-staff|signed principal)")


def _scopes(text: str, markdown: bool = True):
    """Claim scopes: a table row or list item alone, other prose by paragraph.

    Same rule as the Week 7/8/9 status guards. A marker three rows away is not a
    marker on this row, and these documents put a 2026 finding in one cell and
    its correction in the next.

    `markdown=False` for source files: a JSDoc block is a run of lines each
    starting with `*`, and treating those as list items chops one comment into a
    dozen scopes -- which is how this guard first reported a fenced sentence in
    `RequireRole.tsx` as an unfenced claim.
    """
    bullets = "[-*+]" if markdown else "[-+]"
    is_row = re.compile(rf"^\s*(?:{bullets}\s|\|)")
    for block in text.replace("\r\n", "\n").split("\n\n"):
        lines = block.splitlines()
        if not any(is_row.match(line) for line in lines):
            yield block
            continue

        # A list item owns its continuation lines. Lumping every continuation
        # into one "everything else" scope splices unrelated bullets together,
        # and a sentence assembled from two different bullets is not a sentence
        # anyone wrote.
        current: list = []
        preamble: list = []
        for line in lines:
            if is_row.match(line):
                if current:
                    yield "\n".join(current)
                current = [line]
            elif current:
                current.append(line)
            else:
                preamble.append(line)
        if current:
            yield "\n".join(current)
        if any(p.strip() for p in preamble):
            yield "\n".join(preamble)


def _flat(scope: str) -> str:
    return re.sub(r"\s+", " ", scope)


def _sentences(scope: str):
    """The scope's sentences, flattened.

    Prose is checked sentence by sentence rather than paragraph by paragraph.
    Mutation testing forced this: a bullet reading "**No second approver.**
    adjust-balance and waive-fee move money" passed while the rest of the same
    bullet still described the proposal path, because a paragraph-wide check
    accepted the neighbouring truth as cover for the planted falsehood.
    """
    flat = _flat(scope)
    # Comment and docstring furniture, stripped so a sentence is a sentence.
    flat = re.sub(r"(^|\s)[*#/]+\s", " ", flat)
    # An ellipsis is not a sentence end. Splitting on it cut a quoted, clearly
    # fenced claim -- `said this service "reads no principal ... and enforces no
    # second approver"` -- into a fragment with the fence on the other side of
    # the break, and the guard then failed on wording it should accept.
    for part in re.split(r"(?<=[;:])\s+|(?<!\.)(?<=\.)\s+", flat):
        if part.strip():
            yield part.strip()


def _claim_is_excused_in_a_row(row: str, pattern: re.Pattern) -> bool:
    """True when the row also states the control that closed the claim.

    The roadmap's gap tables read finding -> status -> why it mattered: "No
    maker-checker on any money-affecting action | Fixed -- adjust-balance raises
    proposals ... | A single person moving money with no second approver is a
    real internal-controls gap". Two of those three cells name the gap, and the
    row as a whole is true. Demanding a historical marker inside each of them
    would be demanding that gap lists stop naming gaps, and that rationale
    columns stop explaining why anyone cared.
    """
    return _CLOSURE.search(row) is not None and pattern.search(row) is not None


# --------------------------------------------------------------------------
# The controls exist. Everything below is asserted against these.
# --------------------------------------------------------------------------

def test_the_d8_controls_are_present_in_the_code():
    principal = _read(PRINCIPAL)
    maker_checker = _read(MAKER_CHECKER)
    main = _read(SERVICING_MAIN)

    assert "def require_staff_principal" in principal, (
        "the verified-human control is gone; D8's status everywhere needs "
        "rewriting and this guard is the wrong shape")
    assert "def resolve(" in maker_checker and "def propose(" in maker_checker, (
        "the maker-checker propose/resolve pair is gone")
    assert RESOLVE_MIGRATION.is_file(), "the resolve migration is gone"
    resolve_sql = _read(RESOLVE_MIGRATION)
    assert "resolve_pending_movement" in resolve_sql, (
        "the resolve function is no longer in its own migration")
    assert re.search(r"no_self_approval|requested_by\s*=\s*|self-approv",
                     resolve_sql, re.I), (
        "the self-approval refusal is no longer visible in the resolve path")

    # And the live routes use them, which is what makes the docstrings' old
    # claims false rather than merely unfashionable.
    assert "principal.require_staff_principal" in main
    assert "maker_checker.propose" in main
    assert "maker_checker.resolve" in main


def test_the_register_and_the_adr_agree_that_d8_is_closed():
    debt = _read(DEBT)
    row = debt[debt.index("| **D8**"):]
    row = row[:row.index("\n|")] if "\n|" in row else row

    assert re.match(r"^\s*\*\*Fixed", _flat(row.split("|")[3]).strip()), (
        "docs/DEBT.md D8 no longer opens as Fixed -- if the control regressed, "
        "every surface this guard checks needs the opposite treatment")

    adr = _read(REPO / "adr" / "0011-maker-checker-for-servicing-adjustments.md")
    assert re.search(r"D8 is closed", adr), (
        "ADR 0011 no longer states that D8 is closed, while DEBT.md says Fixed")


# --------------------------------------------------------------------------
# No live surface may contradict them.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", LIVE_SURFACES, ids=lambda p: p.name)
def test_no_live_surface_says_the_d8_controls_are_missing(path):
    text = _read(path)

    markdown = path.suffix == ".md"

    for pattern, why in FALSE_CLAIMS:
        for scope in _scopes(text, markdown=markdown):
            is_row = scope.lstrip().startswith("|")
            if is_row:
                if not pattern.search(_flat(scope)):
                    continue
                if _claim_is_excused_in_a_row(scope, pattern):
                    continue
                if _HISTORICAL.search(_flat(scope)):
                    continue
                pytest.fail(
                    f"{path.name} states {pattern.pattern!r} in a table row "
                    f"with no closure column after it and no historical "
                    f"marker.\nContradicted by: {why}.\n{_flat(scope)[:320]}")
                continue

            for sentence in _sentences(scope):
                if not pattern.search(sentence):
                    continue
                if _HISTORICAL.search(sentence) or _LATE_FEE.search(sentence):
                    continue
                pytest.fail(
                    f"{path.name} states {pattern.pattern!r} as current, with "
                    f"nothing in that sentence marking it as history.\n"
                    f"Contradicted by: {why}.\n{sentence[:320]}")


def test_the_servicing_docstring_states_the_controls_positively():
    """The negative checks are not enough on their own.

    A file that legitimately quotes its own superseded wording carries a
    historical marker, and that marker then shelters anything else in the same
    scope -- the mutation lesson from the Week 8 guard. So the docstring must
    also say, positively, that the human and the second approver exist.
    """
    doc = _flat(_read(SERVICING_MAIN)[:4000])

    assert re.search(r"principal\.require_staff_principal|verified (?:staff )?principal",
                     doc, re.I), (
        "servicing's module docstring does not say it verifies the human")
    assert re.search(r"second (?:person|approver)|different verified principal",
                     doc, re.I), (
        "servicing's module docstring does not say a second person is required")
    assert re.search(r"D8 is closed", doc), (
        "servicing's module docstring does not state D8's current status")


def test_maker_checker_names_the_guard_its_own_routes_use():
    """The module that implements the second approver must point at the right
    guard.

    Its docstring named `principal.require_money_principal` -- the csr/admin
    money-mover bit, which after the cutover is used by `late-fee` alone. Every
    proposal route calls `require_staff_principal`, and role authority comes from
    this module's own matrix, because "may move money" cannot express
    csr-proposes-but-never-approves. A reader tracing the control from here was
    sent to the wrong function, which is the same defect class as the rest of
    this file, one layer in.
    """
    doc = _read(MAKER_CHECKER)
    docstring = doc[:doc.index('"""', 3) + 3]
    flat = _flat(docstring)
    main = _read(SERVICING_MAIN)

    assert "require_staff_principal" in flat, (
        "maker_checker's docstring does not name the guard its routes call")
    body = doc[len(docstring):]
    for role_set in ("PROPOSER_ROLES", "APPROVER_ROLES_AT_OR_BELOW_THRESHOLD"):
        # In the DOCSTRING, not merely somewhere in the module: checking the
        # whole file passed trivially because the constants are defined there,
        # which mutation testing caught.
        assert role_set in flat, (
            f"maker_checker's docstring does not name {role_set}, so its role "
            f"rule reads as a single bit")
        assert role_set in body, (
            f"{role_set} is named in the docstring but no longer defined in the "
            f"module")

    # If the money-mover guard is mentioned, it must be as the late-fee guard or
    # as history -- never as this module's identity check.
    for sentence in _sentences(docstring):
        if "require_money_principal" not in sentence:
            continue
        assert _HISTORICAL.search(sentence) or _LATE_FEE.search(sentence), (
            f"maker_checker's docstring points at require_money_principal as a "
            f"current rule for its own routes:\n{sentence[:240]}")

    # And the claim is checked against the routes rather than trusted.
    assert main.count("principal.require_staff_principal") >= 4, (
        "the proposal routes no longer call require_staff_principal, so both "
        "the docstring and this guard need rewriting")


def test_the_retired_direct_writers_are_described_as_retired():
    """`models.py` already says `adjust_balance` and `waive_fee` are reachable
    from no route. Their own docstrings said the opposite, which is the version a
    reader lands on first."""
    balance = _read(BALANCE)

    for name in ("def adjust_balance(", "def waive_fee("):
        start = balance.index(name)
        docstring = balance[start:start + 1800]
        assert re.search(r"no route reaches (?:this|it)|not the live path|"
                         r"reachable from no route|retired direct writer",
                         docstring, re.I), (
            f"{name.strip('def (')}'s docstring does not say no route reaches "
            f"it, so it reads as the live money path")
