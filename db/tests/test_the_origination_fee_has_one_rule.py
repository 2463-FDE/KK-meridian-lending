"""The origination fee a borrower is shown is derived one way, in both services.

Two services compute the amount-financed breakdown, because two services answer
"what was disclosed on this offer":

  * `disclosure-service` builds and re-reads the offer itself;
  * `origination-service` reads the offers row directly for
    `GET /applications/{id}`, which is what the underwriting console and a
    returning borrower's page use.

Two images, no shared library (ADR 0002), so the rule is written twice. That is
the same arrangement the maker-checker limits and the note rate have, and it gets
the same treatment: a test whose only job is to fail the moment the copies stop
agreeing.

**What must not drift, specifically.** The fee is the DIFFERENCE between the
stored principal and the stored amount financed -- never the fee percentage
applied a second time. `amount_financed` is stored as
`ROUND_HALF_UP(principal - principal * fee_pct)`, so the difference is what was
rounded: on a $1,002.50 principal the stored figures imply a fee of $30.07 while
`round(1002.50 * 0.03)` is $30.08. One of those makes the borrower's disclosure
box foot and the other does not.

The fee has already been three different numbers in three files (`docs/DEBT.md`
D6) and the note rate six (PR #80). Neither drifted because someone decided to
change it; each copy was added by someone who needed the figure where they were
standing.
"""
import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

#: Each service's breakdown function, by the file it lives in.
IMPLEMENTATIONS = {
    "disclosure-service": (
        REPO / "services" / "disclosure-service" / "app" / "routers" / "offers.py",
        "_amount_financed_breakdown",
    ),
    "origination-service": (
        REPO / "services" / "origination-service" / "app" / "routers" / "applications.py",
        "_amount_financed_breakdown",
    ),
}

#: Names that mean a fee RATE rather than a stored amount. Reading one at display
#: time is what makes a fee-policy change restate an existing offer -- the drift
#: `offers.fee_pct_used` was added to prevent.
FEE_RATE_NAMES = ("ORIGINATION_FEE_PCT", "fee_pct", "fee_pct_used",
                  "LEGACY_PRE_SNAPSHOT_FEE_PCT")


def _function(path: pathlib.Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        "%s has no function %s -- the breakdown moved or was removed, and this "
        "guard cannot see the copies it exists to compare"
        % (path.relative_to(REPO).as_posix(), name))


def _body_without_docstring(fn: ast.FunctionDef) -> str:
    """The code, with the docstring dropped.

    Every one of these functions explains in prose why it does not use the fee
    percentage, and a guard that scanned the whole source would fail on that
    explanation -- matching the comment rather than the behaviour.
    """
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


@pytest.mark.parametrize("service", sorted(IMPLEMENTATIONS))
def test_the_fee_is_never_derived_from_a_rate(service):
    path, name = IMPLEMENTATIONS[service]
    code = _body_without_docstring(_function(path, name))

    for rate_name in FEE_RATE_NAMES:
        assert rate_name not in code, (
            "%s derives the displayed fee from %s. The fee a borrower paid is "
            "the difference between two stored amounts; reading a rate at "
            "display time silently restates an existing offer the next time fee "
            "policy changes" % (service, rate_name))


@pytest.mark.parametrize("service", sorted(IMPLEMENTATIONS))
def test_the_fee_is_a_subtraction(service):
    """Positively, not only by absence. A function that returned a constant
    would pass the check above and be just as wrong."""
    path, name = IMPLEMENTATIONS[service]
    fn = _function(path, name)

    subtractions = [node for node in ast.walk(fn)
                    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)]

    assert subtractions, (
        "%s's breakdown contains no subtraction. The fee is the stored principal "
        "less the stored amount financed; anything else is a second opinion "
        "about what the borrower paid" % service)

    multiplications = [node for node in ast.walk(fn)
                       if isinstance(node, ast.BinOp)
                       and isinstance(node.op, (ast.Mult, ast.Div))]
    assert not multiplications, (
        "%s's breakdown multiplies or divides. Applying a percentage is exactly "
        "the derivation that disagrees with the stored figures by a cent"
        % service)


@pytest.mark.parametrize("service", sorted(IMPLEMENTATIONS))
def test_both_refuse_rather_than_invent_when_the_principal_is_absent(service):
    """A legacy row must produce no breakdown in either service. Asserted on the
    code because the two functions take different inputs -- one a mapping, one an
    ORM row -- so there is no single call this test could make."""
    path, name = IMPLEMENTATIONS[service]
    code = _body_without_docstring(_function(path, name))

    assert "None, None" in code or "(None, None)" in code, (
        "%s does not return an empty breakdown for a row with no stored "
        "principal. The only recoverable value is the inverted one, which is a "
        "cent away from what the borrower asked for" % service)


@pytest.mark.parametrize("service", sorted(IMPLEMENTATIONS))
def test_both_use_decimal_rather_than_float_arithmetic(service):
    """The subtraction is money. `9637.0 - 9347.89` in binary floating point is
    289.11000000000058, and the borrower's box would show a fee that does not
    match the difference of the two figures printed beside it."""
    path, name = IMPLEMENTATIONS[service]
    code = _body_without_docstring(_function(path, name))

    assert "Decimal" in code or "_dec" in code, (
        "%s subtracts money in floating point. The figures are NUMERIC(14,2) and "
        "the result is printed to the cent" % service)


def test_the_two_services_agree_on_the_same_inputs():
    """The comparison the whole file is for, run rather than inspected.

    Both functions are loaded from source with a stub `Decimal`, `getattr` and
    logger, so neither service package has to be importable in this job -- the
    same reason `test_the_note_rate_has_one_source.py` executes a slice of each
    config module instead of importing it.
    """
    from decimal import Decimal

    loaded = {}
    for service, (path, name) in IMPLEMENTATIONS.items():
        fn = _function(path, name)

        class _Log:
            @staticmethod
            def error(*a, **kw):
                pass

        namespace = {"Decimal": Decimal, "log": _Log, "getattr": getattr}
        # disclosure-service's breakdown calls `_dec`, its own "stored money as
        # an exact Decimal" helper. It is loaded FROM THE SAME FILE rather than
        # reimplemented here: a hand-written stand-in would make this comparison
        # run against a function that is not the one that ships, which is the
        # defect this whole file is about, committed inside the test for it.
        for helper in ("_dec",):
            try:
                helper_fn = _function(path, helper)
            except AssertionError:
                continue
            exec(compile(ast.Module(body=[helper_fn], type_ignores=[]),
                         str(path), "exec"), namespace)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"),
             namespace)
        loaded[service] = namespace[name]

    class _Row:
        def __init__(self, principal, amount_financed):
            self.principal = principal
            self.amount_financed = amount_financed
            self.id = 1
            self.app_id = 1

    # disclosure-service takes the two amounts; origination takes a row. Same
    # question, different call shapes -- which is exactly why an executed
    # comparison is worth more here than reading both.
    cases = [("1002.50", "972.43"), ("9000.00", "8730.00"),
             ("49999.99", "48499.99"), ("5000.00", "5000.00")]

    for principal, financed in cases:
        from_disclosure = loaded["disclosure-service"](
            Decimal(principal), Decimal(financed))
        from_origination = loaded["origination-service"](
            _Row(Decimal(principal), Decimal(financed)))

        assert from_disclosure == from_origination, (
            "the two services report different breakdowns for principal=%s "
            "amount_financed=%s: %r vs %r"
            % (principal, financed, from_disclosure, from_origination))

    # And both refuse the legacy shape identically.
    assert loaded["disclosure-service"](None, Decimal("972.43")) == (None, None)
    assert loaded["origination-service"](_Row(None, Decimal("972.43"))) == (None, None)
