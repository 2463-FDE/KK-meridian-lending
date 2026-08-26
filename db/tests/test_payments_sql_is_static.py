"""The premise `check_no_pan_readers.py` rests on, enforced instead of assumed.

The checker folds SQL out of the syntax tree to decide whether anything still
reads `payments.pan` / `payments.cvv`, and migration 0031 refuses to drop those
columns without an acknowledgement that it passed. Review of PR #15 kept finding
the same family of holes in that folding: a runtime-selected column
(`f"SELECT {column} FROM payments"`), a `%` template with an unknown operand, a
module constant rebound after a function is defined, a tuple of columns
overwritten by a same-named local. Each is a real way to defeat a static folder,
and chasing them one at a time was turning a purpose-built check into a general
Python evaluator -- which is a losing race, because `getattr`, a config file and
a database round-trip all defeat it in the end.

So this test attacks the premise rather than the folder. Every one of those
findings needs application code of a particular shape to exist: SQL naming the
`payments` table that is COMPOSED rather than written out. This test asserts that
no such code is in the tree. If that holds, the checker never has to fold
anything interesting to be right about this repository, and the remaining
findings describe robustness of the tool rather than a way to authorize an unsafe
drop.

The trade is deliberate and it is narrower than it sounds: composing SQL against
`payments` now fails a test with instructions, instead of silently landing in a
blind spot of the thing that guards a destructive migration. Anyone who needs
dynamic SQL there has to say so out loud. The residual limits of the folder
itself are recorded as debt in docs/DEBT.md (D20), not silently carried.

Application code only. The migration harness in db/tests interpolates a schema
name into its fixtures on purpose, and test code cannot authorize the drop --
0031 reads the checker's verdict on the services, and an operator confirms the
running images.
"""
import ast
import re
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# Calls that hand SQL to a database. `query` is this repository's own thin
# wrapper -- every service has one, and it is how application code actually
# sends raw SQL, so leaving it out would exempt nearly all of the real traffic.
DB_CALLS = {
    "execute", "executemany", "executescript", "query", "fetch", "fetchone",
    "fetchall", "fetchval", "fetchrow", "scalar", "exec_driver_sql", "text",
}

# The payments TABLE in a position where SQL names one. A bare "payments"
# substring also matches `total_of_payments`, which is a column on `offers`, and
# drowns the signal. The word boundary is written as a negative lookahead rather
# than \b on purpose: \b in this repository has twice been turned into a literal
# U+0008 in transit, and the failure is invisible -- the pattern still compiles
# and matches nothing, so the check reports clean. `test_no_control_character_
# escapes` guards the source; this guards the semantics.
TABLE_REFERENCE = r"(?:from|join|into|update|table)\s+(?:public\.)?payments(?!\w)"


@pytest.fixture(scope="module")
def table_pattern():
    import re

    pattern = re.compile(TABLE_REFERENCE, re.I)
    # A pattern that cannot match makes every assertion below vacuous, so it is
    # exercised before it is trusted.
    assert pattern.search("INSERT INTO payments (loan_id) VALUES (1)")
    assert pattern.search("select id from payments where id = 1")
    assert pattern.search("SELECT x FROM public.payments p JOIN loans l")
    assert not pattern.search("SELECT total_of_payments FROM offers")
    assert not pattern.search("SELECT regular_payment_count FROM offers")
    return pattern


def _application_files():
    files = []
    for service in sorted((REPO / "services").iterdir()):
        app = service / "app"
        if not app.is_dir():
            continue
        files.extend(sorted(p for p in app.rglob("*.py")
                            if "__pycache__" not in p.parts))
    assert files, "no application files found -- the walk is looking in the wrong place"
    return files


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _names_payments(node, pattern):
    """True if any string constant anywhere inside `node` names the table.

    Deliberately looks INSIDE the expression: the point is to catch
    f"SELECT {col} FROM payments", whose table reference sits in one of the
    literal pieces rather than in the value of the whole expression.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if pattern.search(sub.value):
                return True
    return False


def _is_string_expression(node):
    """String-composition shapes only. A dict or a Gauge() is not SQL."""
    if isinstance(node, (ast.JoinedStr, ast.BinOp)):
        return True
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Call):
        return _call_name(node) in {"format", "join"}
    return False


def _describe(node):
    if isinstance(node, ast.JoinedStr):
        holes = [v for v in node.values if not isinstance(v, ast.Constant)]
        return "f-string with %d interpolation(s)" % len(holes)
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mod):
            return "%-formatted template"
        if isinstance(node.op, ast.Add):
            return "string concatenation"
        return "binary operation"
    if isinstance(node, ast.Call):
        return "%s() call" % (_call_name(node) or "?")
    return type(node).__name__


def _composed_payments_sql(pattern):
    """Every composed payments-table SQL expression in application code."""
    found = []
    for path in _application_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:            # pragma: no cover - syntax is CI-checked
            pytest.fail("%s does not parse: %s" % (path, exc))
        for node in ast.walk(tree):
            # SQL handed straight to a database call.
            if isinstance(node, ast.Call) and _call_name(node) in DB_CALLS:
                for arg in node.args:
                    if (_names_payments(arg, pattern)
                            and not isinstance(arg, ast.Constant)):
                        found.append((path, node.lineno, _describe(arg)))
            # And SQL assigned to a name first, the `sql = ...; db.query(sql)`
            # shape -- caught at the assignment, because that is where the
            # composition happens and where the fix belongs.
            #
            # `ast.AnnAssign` as well as `ast.Assign`: `sql: str = f"..."` is the
            # same statement with a type annotation on it, and handling only the
            # unannotated form left the annotated one invisible to this test AND
            # to the checker -- `db.query(sql)` carries no table literal of its
            # own, so nothing downstream noticed either. Reviewed on PR #15.
            elif (isinstance(node, (ast.Assign, ast.AnnAssign))
                  and node.value is not None
                  and _is_string_expression(node.value)
                  and _names_payments(node.value, pattern)
                  and not isinstance(node.value, ast.Constant)):
                found.append((path, node.lineno, _describe(node.value)))
    return found


def test_no_application_code_composes_sql_against_the_payments_table(table_pattern):
    """The invariant that makes a clean checker run mean something here.

    Every remaining dynamic-SQL finding on PR #15 needs code of this shape to
    exist. None does, so the checker is not relying on folding it correctly.
    """
    composed = _composed_payments_sql(table_pattern)
    assert not composed, (
        "application code composes SQL against the `payments` table:\n"
        + "\n".join(
            "  %s:%d  (%s)" % (p.relative_to(REPO).as_posix(), line, what)
            for p, line, what in composed
        )
        + "\n\n`check_no_pan_readers.py` gates a destructive migration by folding\n"
          "SQL statically, and composed SQL is where that folding has blind spots.\n"
          "Either write the statement out as a literal, or -- if the composition is\n"
          "genuinely required -- extend the checker to resolve this exact pattern\n"
          "and say so in docs/DEBT.md (D20) before dropping any column."
    )


def test_the_walk_actually_reaches_the_payments_readers(table_pattern):
    """Guard against the assertion above passing because it found nothing at all.

    A walk pointed at the wrong directory, or a pattern broken in transit, also
    reports "no composed SQL". So this asserts the population is non-empty: real
    payments-table statements exist in application code and this walk sees them.
    Without it, the test above is indistinguishable from a no-op.
    """
    literals = 0
    for path in _application_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in DB_CALLS:
                for arg in node.args:
                    if (isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and table_pattern.search(arg.value)):
                        literals += 1
    assert literals >= 5, (
        "only %d static payments-table statements were found in application "
        "code; the walk or the pattern is broken, which would make the "
        "invariant test above vacuous" % literals
    )


def test_the_walk_treats_an_annotated_assignment_like_a_plain_one(table_pattern):
    """Reviewed on PR #15: `sql: str = f"..."` was invisible to this walk.

    It handled `ast.Assign` only, so an annotated assignment of composed payments
    SQL passed -- and the `db.query(sql)` that followed carries no table literal
    of its own, so nothing else caught it either. Annotating a line is not a
    semantic change and must not be a way through.

    Exercised on synthetic sources rather than by planting code in a service, so
    the assertion is about the walk itself and cannot be satisfied by the tree
    happening to be clean.
    """
    annotated = ast.parse(
        'def read(column):\n'
        '    sql: str = f"SELECT {column} FROM payments"\n'
        '    return db.query(sql)\n'
    )
    plain = ast.parse(
        'def read(column):\n'
        '    sql = f"SELECT {column} FROM payments"\n'
        '    return db.query(sql)\n'
    )

    def composed(tree):
        found = []
        for node in ast.walk(tree):
            if (isinstance(node, (ast.Assign, ast.AnnAssign))
                    and node.value is not None
                    and _is_string_expression(node.value)
                    and _names_payments(node.value, table_pattern)
                    and not isinstance(node.value, ast.Constant)):
                found.append(_describe(node.value))
        return found

    assert composed(annotated), "an annotated composed assignment was not seen"
    assert composed(annotated) == composed(plain), (
        "the annotated and plain forms are the same statement and must be "
        "reported identically"
    )

    # And a declaration with no value binds nothing, rather than crashing on
    # `node.value` being None.
    assert composed(ast.parse("def read():\n    sql: str\n")) == []


# ── D20: the second premise, previously true and unasserted ──────────────────
#
# `check_no_pan_readers.py` decides whether anything still reads
# `payments.pan` / `payments.cvv` by folding SQL out of the syntax tree, and
# `docs/DEBT.md` D20 is explicit that a static folder cannot resolve an
# arbitrarily dynamic program. The response to that has been to enforce the
# PREMISE instead: while no application code composes SQL against `payments`,
# the folder never has to resolve anything interesting to be right here.
#
# There is a second premise doing the same work, and until now nothing asserted
# it. `SELECT *` names no columns, so a wildcard read is a statement the folder
# cannot reason about at all -- it can prove the statement touches `payments`
# and nothing whatever about which columns come back. Today no application code
# does it. That fact is load-bearing and was resting on nobody having written one.
#
# Kept narrow on purpose. This is not a general ban on `SELECT *`, which would
# be a style rule; it is a ban on wildcard reads of the ONE table whose dropped
# columns the checker exists to police.

WILDCARD_PAYMENTS = r"select\s+\*\s+from\s+(?:public\.)?payments(?!\w)"


@pytest.fixture(scope="module")
def wildcard_pattern():
    import re

    pattern = re.compile(WILDCARD_PAYMENTS, re.I)
    # Exercised before it is trusted, for the same reason `table_pattern` is: a
    # pattern that cannot match makes the assertion below vacuous.
    assert pattern.search("SELECT * FROM payments WHERE id = 1")
    assert pattern.search("select  *  from public.payments p")
    assert not pattern.search("SELECT * FROM offers")
    assert not pattern.search("SELECT id, amount FROM payments")
    return pattern


def _string_literals_in_application_code():
    """Every string constant in service application code, with its location."""
    for path in sorted(REPO.glob("services/*/app/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield path, node.lineno, node.value


def test_no_application_code_reads_the_payments_table_with_a_wildcard(
        wildcard_pattern):
    """A wildcard read is the one shape the checker can say nothing about.

    It can prove the statement names `payments`; it cannot name a single column
    the query returns. So `SELECT *` would reintroduce exactly the uncertainty
    D20 records, in a form no amount of folding resolves -- and it would do it
    while the checker still reported clean.
    """
    offenders = [
        (path, line) for path, line, text in _string_literals_in_application_code()
        if wildcard_pattern.search(text)
    ]

    assert not offenders, (
        "application code reads the `payments` table with a wildcard:\n"
        + "\n".join("  %s:%d" % (p.relative_to(REPO).as_posix(), line)
                    for p, line in offenders)
        + "\n\n`SELECT *` names no columns, so `check_no_pan_readers.py` cannot\n"
          "tell whether a dropped column comes back in the result set. Name the\n"
          "columns the query actually needs. See docs/DEBT.md (D20)."
    )


def test_the_wildcard_walk_actually_reads_application_code(wildcard_pattern):
    """Guard the guard: the assertion above must not pass on an empty walk.

    A glob pointed at the wrong directory also reports "no wildcard reads". So
    this proves the walk sees real SQL naming `payments` -- the same population
    the composed-SQL test asserts on -- and that the pattern would fire if such
    a statement existed.
    """
    saw_payments_sql = False
    for _, _, text in _string_literals_in_application_code():
        if re.search(TABLE_REFERENCE, text, re.I):
            saw_payments_sql = True
            break

    assert saw_payments_sql, (
        "the walk found no SQL naming the payments table at all, so the "
        "wildcard assertion above is indistinguishable from a no-op")

    # And the pattern fires on the shape it exists to catch, proving the clean
    # result above is the code's doing rather than the regex's.
    assert wildcard_pattern.search("SELECT * FROM payments")
