"""A synthetic loan must not trip the warning built to report a real data defect.

**WHAT WENT WRONG, which is why this is a test and not a comment.** The E2E
fixture that creates a serviced loan wrote the contract
`principal 12000.00, note rate 7.99%, term 36, regular 375.94 x 35, final
375.90`. Those amounts do not amortize 12,000: expanded by servicing's own
`amortization_from_contract`, the closing balance lands at **1.72**. So
`GET /loans/{id}/schedule` answered, correctly:

    This loan's recorded terms do not add up. The payment amounts recorded for
    this loan do not fully amortize its principal; 1.72 remains unaccounted for.
    This is a data defect and needs investigation before the final payment is
    taken.

The warning was right. The DATA was wrong, and it was wrong because four
plausible-looking numbers were written instead of asked for. A local database
held sixty-two of these loans, and a servicing screen opened on one during the
audit and showed that warning about a loan the test suite had manufactured.
While a run is in flight those fixture loans are `current`, so they are
reachable from the portfolio list.

**Why the check lives here.** The amounts are written in TypeScript
(`frontend/e2e/fixtures.ts`) and in a service test; the generator that can judge
them is Python. Nothing held the two together, so nothing did. This reads the
literals out of those files and expands them with the real generator.

**Two claims, deliberately separate.** Fixture contracts amortize, and the
canonical seeded contracts in `db/init` amortize. The second passes today and is
the one a client actually sees: it is asserted so a future seed cannot introduce
what the fixture had.

Not a schema constraint, and that is a judgement rather than an omission:
amortization is an iterative expansion over the term, so a CHECK would have to
re-implement the generator in SQL and the two copies would drift -- the exact
defect the real-schema harness exists to prevent elsewhere.
"""
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVICING = REPO / "services" / "servicing-service"

sys.path.insert(0, str(SERVICING))
schedule = pytest.importorskip(
    "app.schedule",
    reason="servicing-service's schedule generator is what judges a contract")

#: Where a hand-written loan contract can appear. DISCOVERED, not listed.
#:
#: A literal list of three filenames was the first version of this, and it
#: failed its own guard-the-guard immediately: two of the three fixtures live on
#: sibling branches, so on this branch the list found one contract and demanded
#: three. That is the hand-maintained-list defect this repository has produced
#: five times -- a list that reads complete while missing an entry -- and the
#: repo's own rule is to derive the set from the source instead. Any test or
#: fixture that writes a `loans` contract group is found by the pattern below,
#: including ones added after this file.
FIXTURE_GLOBS = (
    ("frontend/e2e", "*.ts"),
    ("services", "**/tests/*.py"),
    ("db/tests", "*.py"),
)


def _candidate_files():
    seen = []
    for directory, pattern in FIXTURE_GLOBS:
        root = REPO / directory
        if not root.is_dir():
            continue
        for path in sorted(root.glob(pattern)):
            if path.name == pathlib.Path(__file__).name:
                continue
            seen.append(path)
    return seen

_CONTRACT = re.compile(
    r"VALUES\s*\([^)]*?"
    r"(?P<principal>\d+\.\d{2})\s*,\s*"
    r"(?P<rate>\d+\.\d+)\s*,\s*"
    r"(?P<term>\d+)\s*,\s*"
    r"(?P<regular>\d+\.\d{2})\s*,\s*"
    r"(?P<count>\d+)\s*,\s*"
    r"(?P<final>\d+\.\d{2})\s*,\s*'",
    re.S)


def _contracts_in(path):
    if not path.is_file():
        return []
    found = []
    for m in _CONTRACT.finditer(path.read_text(encoding="utf-8")):
        found.append((
            path.name,
            float(m.group("principal")), float(m.group("rate")),
            int(m.group("term")), float(m.group("regular")),
            int(m.group("count")), float(m.group("final")),
        ))
    return found


def _all_fixture_contracts():
    out = []
    for path in _candidate_files():
        out.extend(_contracts_in(path))
    return out


def _residue(principal, rate, term, regular, final):
    """The closing balance the API reads, from the function the API calls."""
    rows = schedule.amortization_from_contract(principal, rate, term, regular, final)
    return rows[-1]["balance"] if rows else principal


def test_the_fixture_files_still_declare_contracts_here():
    """Guard the guard.

    If a fixture is renamed or its INSERT reshaped, the cases below would run
    over an empty set and report success for having checked nothing -- the
    vacuous pass this repository has produced before.
    """
    contracts = _all_fixture_contracts()
    assert contracts, (
        "no hand-written loan contract found under %s. Either every fixture now "
        "derives its contract (in which case delete this file with them) or the "
        "INSERT shape changed and the pattern no longer matches -- it must fail "
        "rather than pass by finding nothing"
        % ", ".join("%s/%s" % g for g in FIXTURE_GLOBS))


@pytest.mark.parametrize("contract", _all_fixture_contracts(),
                         ids=lambda c: "%s:%.0f" % (c[0], c[1]))
def test_every_fixture_contract_amortizes(contract):
    """A fixture loan must not be a loan the product would flag as defective."""
    name, principal, rate, term, regular, count, final = contract
    residue = _residue(principal, rate, term, regular, final)
    assert abs(residue) < 0.01, (
        "%s creates a loan whose stored terms do not amortize: %.2f at %s%% "
        "over %d months paid %.2f x %d then %.2f leaves %.2f unaccounted for, "
        "so the schedule route reports it as a data defect. Take the amounts "
        "from schedule.amortization(%s, %s, %d) instead of choosing plausible "
        "ones"
        % (name, principal, rate, term, regular, count, final, residue,
           principal, rate, term))


@pytest.mark.parametrize("contract", _all_fixture_contracts(),
                         ids=lambda c: "%s:%.0f" % (c[0], c[1]))
def test_every_fixture_contract_states_the_right_payment_count(contract):
    """`regular_payment_count` must be the number of regular payments there are.

    A contract can amortize while claiming the wrong count: the residue check
    above never reads it, because `amortization_from_contract` derives the count
    from the term. The stored column is what other code bills from.
    """
    name, principal, rate, term, regular, count, final = contract
    assert count == term - 1, (
        "%s stores regular_payment_count=%d for a %d-month term; Model B is "
        "(term - 1) regular payments plus one final payment"
        % (name, count, term))


def test_the_seeded_contracts_in_db_init_amortize():
    """The canonical demo data, which is what a client actually sees.

    Passes today. It is asserted because the fixture defect above shows how a
    plausible-looking four-number group gets written, and a seeded loan that did
    it would put the same warning on a demo screen with nobody's test running.
    """
    seed = REPO / "db" / "init" / "002_seed.sql"
    text = seed.read_text(encoding="utf-8")

    header = re.search(
        r"INSERT INTO offers\s*\(([^)]*)\)\s*VALUES\s*(?P<rows>.*?);",
        text, re.S)
    if header is None:
        pytest.skip("no offers seed statement in 002_seed.sql")

    # Read by COLUMN NAME rather than by position: the seed's column order is
    # not this test's to assume, and a reordering would otherwise be read as a
    # wrong contract instead of as a moved column.
    #
    # `principal` is the amortized base, NOT `amount_financed`, and getting that
    # wrong is easy: `amount_financed` is net of the origination fee, so
    # expanding the stored payments against it leaves a large NEGATIVE residue
    # (-742.58 on loan 4471) that looks like a defect and is a misread. The
    # loans rows boarded from these offers carry `principal`, and they amortize.
    columns = [c.strip() for c in header.group(1).split(",")]
    needed = ("app_id", "note_rate_pct", "monthly_payment",
              "regular_payment_count", "final_payment", "term_months",
              "principal")
    missing = [c for c in needed if c not in columns]
    assert not missing, (
        "db/init/002_seed.sql's offers INSERT no longer names %s -- this test "
        "must fail rather than guess at positions" % ", ".join(missing))
    at = {name: columns.index(name) for name in needed}

    checked = 0
    for row in re.finditer(r"\(([^()]*)\)", header.group("rows")):
        parts = [p.strip() for p in row.group(1).split(",")]
        if len(parts) != len(columns):
            continue
        loan_id = int(parts[at["app_id"]])
        rate = float(parts[at["note_rate_pct"]])
        regular = float(parts[at["monthly_payment"]])
        count = int(parts[at["regular_payment_count"]])
        final = float(parts[at["final_payment"]])
        term = int(parts[at["term_months"]])
        principal = float(parts[at["principal"]])

        residue = _residue(principal, rate, term, regular, final)
        assert abs(residue) < 0.01, (
            "seeded loan %d in db/init/002_seed.sql does not amortize: %.2f "
            "unaccounted for. A demo opening this loan is shown the "
            "data-defect warning" % (loan_id, residue))
        assert count == term - 1, (
            "seeded loan %d stores regular_payment_count=%d for a %d-month term"
            % (loan_id, count, term))
        checked += 1

    assert checked >= 3, (
        "only %d seeded contracts parsed out of 002_seed.sql -- if the seed's "
        "shape changed this test must fail rather than check nothing" % checked)
