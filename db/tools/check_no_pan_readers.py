"""Prove nothing still reads payments.pan / payments.cvv before dropping them.

    python db/tools/check_no_pan_readers.py            # exit 0 = source is clean
    python db/tools/check_no_pan_readers.py --verbose  # show every match

Why this exists. db/migrations/0031 drops `payments.pan` and `payments.cvv`.
Any service instance still selecting or mapping those columns starts failing the
moment the ALTER commits -- and during a rolling restart, instances running the
PREVIOUS image are exactly that. Migration 0031 refuses to run without an
acknowledgement that this check passed; this is the check.

What it can and cannot answer, stated plainly because the difference is the
whole point:

  CAN    -- whether the source tree it is pointed at still references these
            columns, in ORM mappings, in raw SQL, or in attribute reads.
  CANNOT -- which images are actually serving traffic right now. Nothing in this
            repository knows that. The operator confirms it, and
            docs/RUNBOOK-pan-cvv-contract.md says how.

A green run means "the code at this revision is clean", NOT "production is
clean". Both are required before the drop. Treating the first as the second is
the mistake this file exists to make hard rather than easy.

Matching is case-INSENSITIVE, because PostgreSQL folds an unquoted identifier:
`SELECT PAN` reads the very column being dropped. An early version matched only
lowercase on the argument that real references here are lowercase while the
prose is capitalised -- which was true of the prose and not of SQL, and it meant
an uppercase reader passed. Prose is excluded by WHERE it sits (see below)
rather than by how it is capitalised, which is the distinction that actually
holds. Reviewed on PR #15.

What it resolves, and where it stops. SQL reaching a database execution call is
folded from the syntax tree: literals, adjacent literals, concatenation,
f-strings, `.format()` and `%` templates WITH their arguments, and names bound
earlier in the same scope. Bindings are read in source order, so an assignment
below an `execute()` cannot decide what that call meant. SQL that is being
BUILT but cannot be finished -- an unknown `.format()` argument, a name assigned
from an unresolvable string expression -- fails closed as "unresolved dynamic
SQL passed to execute()".

It does NOT follow calls. `stmt = select(...)` or `sql = build_query()` is
opaque here, and is left alone rather than reported: telling a SQLAlchemy
construct apart from a function returning a SQL string means analysing the whole
application, which this tool deliberately does not do. Nor does it fail closed
on a pass-through wrapper (`def query(sql, params): cur.execute(sql)`), which
every service has -- the query text lives at the CALL SITES and is scanned
there. Failing closed on those would leave the checker permanently red, unable
to authorise the drop it exists to gate.

Excluded from the search, each for a reason:
  * db/migrations/**  -- a migration necessarily names the columns it acts on
  * db/init/**        -- schema and seeds, handled separately
  * tests/**          -- a test asserting the legacy fallback still works is not
                         a live reader
  * comments          -- prose about the columns is not a read
  * REAL docstrings   -- module, class and function docstrings, identified from
                         the syntax tree. NOT triple-quoted strings in general:
                         `conn.query(\"\"\"SELECT pan ...\"\"\")` is a live reader that
                         happens to be quoted the same way, and skipping it
                         returned a false all-clear (PR #15).
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVICES = REPO_ROOT / "services"

PATTERNS = [
    (re.compile(r"^\s*(pan|cvv)\s*:\s*Mapped\["), "ORM column mapping"),
    (re.compile(r"\.(pan|cvv)\b"), "attribute read"),
    (re.compile(r"""getattr\(\s*[^,]+,\s*["'](pan|cvv)["']"""), "dynamic attribute read"),
    # A raw query consumed as a mapping. `row["pan"]` is as live a read as
    # `payment.pan`, and it carries no SQL keyword of its own -- the SELECT that
    # produced the row is elsewhere, often in another function. Reviewed on
    # PR #15.
    (re.compile(r"""\[\s*["'](pan|cvv)["']\s*\]"""), "mapping key read"),
    (re.compile(r"""\.get\(\s*["'](pan|cvv)["']"""), "mapping get read"),
]

# A bare `pan` / `cvv` token counts as a column reference only on a line that is
# also doing SQL. Without that qualifier the word appears in ordinary prose.
_SQL_CONTEXT = re.compile(r"\b(select|insert|update|delete|set|where|values)\b", re.IGNORECASE)
# Case-insensitive: PostgreSQL folds an unquoted identifier to lower case, so
# `SELECT PAN` reads the very column being dropped. This was case-sensitive and
# missed it. Reviewed on PR #15.
_BARE_COLUMN = re.compile(r"\b(pan|cvv)\b", re.IGNORECASE)

# SQL is scanned as whole STRING LITERALS, taken from the syntax tree, not as
# lines within a sliding window. A window is a guess about how far a projection
# can run: the first version required the keyword and the column on one line and
# missed every multiline query; widening it to six lines still missed a
# projection with seven fields before `pan`. A literal has an actual beginning
# and end, so there is nothing left to guess. Reviewed on PR #15.

# Methods that hand a statement to the database. Used only to report a
# variable-carried query at the point it is EXECUTED -- the assignment may be
# far away, or in another function entirely.
# Fail-closed applies to RAW SQL execution only. `.scalar()`/`.fetchall()` on a
# SQLAlchemy construct receive an ORM object, not a string, and reporting those
# as "unresolved SQL" is noise rather than a finding.
_FAIL_CLOSED_CALLS = {"execute", "executemany", "executescript"}

_EXECUTE_CALLS = {
    "execute", "executemany", "executescript", "query", "fetch", "fetchone",
    "fetchall", "fetchval", "fetchrow", "scalar", "exec_driver_sql", "text",
}

SKIP_DIRS = {"__pycache__", "tests", "node_modules", ".git"}


def _is_comment(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("#") or stripped.startswith("--") or stripped.startswith("*")


def _docstring_lines(source: str) -> set[int]:
    """Line numbers belonging to REAL docstrings -- module, class, function.

    The previous version tracked triple-quote fences and skipped everything
    between them. That treats a perfectly ordinary multiline query --

        conn.query(\"\"\"SELECT id, pan FROM payments\"\"\")

    -- as documentation and skips every line of it, so the checker returned exit
    0 over a live reader and could authorise dropping a column deployed code
    still selects. Reviewed on PR #15.

    A docstring is a position in the syntax tree, not a quoting style, so it is
    identified as one: the first statement of a module, class or function when
    that statement is a bare string. Every other string literal -- including a
    triple-quoted SQL constant or one passed to a database call -- is code, and
    is scanned.

    Returns an empty set for a file that does not parse, in which case nothing
    is skipped: over-reporting on an unparseable file is the safe direction for
    a check that gates a destructive migration.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            end = getattr(first, "end_lineno", first.lineno)
            lines.update(range(first.lineno, end + 1))
    return lines


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of the Constant nodes that are real module/class/function docstrings."""
    doc = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                doc.add(id(body[0].value))
    return doc


# A value substituted at runtime. The text around it is what names the columns,
# so the hole is filled with a placeholder rather than dropped -- otherwise
# `f"SELECT {cols} FROM payments"` would silently lose the part that matters.
_RUNTIME_HOLE = " ? "


def _fold(node: ast.AST, names: dict[str, str], depth: int = 0) -> str | None:
    """The SQL text of `node`, when it can be known WITHOUT running anything.

    Deliberately a small set of shapes, not an evaluator:

      * a string constant, including adjacent literals (Python concatenates
        those into one Constant before this ever sees them);
      * `"a" + "b"` and its multiline form;
      * an f-string -- the literal parts, with each substitution as a hole;
      * `"...".format(...)` and `"..." % (...)` -- the TEMPLATE is the SQL, and
        the template is what names the columns;
      * a plain name bound earlier to any of the above.

    Anything else returns None and is simply not analysed. A checker that
    guesses at runtime values would be a Python interpreter with a false
    confidence attached; this one reports what it can prove and the runbook
    carries the rest. Reviewed on PR #15.
    """
    if depth > 8:            # a self-referential assignment chain
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            # A substitution whose value IS statically known gets substituted:
            # `COL = "pan"` then `f"SELECT {COL} FROM payments"` is a live read
            # of pan, and leaving a hole there hid it -- the standalone "pan"
            # carries no SQL context of its own, so nothing else would catch
            # it. Only names this file already resolved; anything else stays a
            # hole. Reviewed on PR #15.
            inner = _fold(value.value, names, depth + 1) if isinstance(value, ast.FormattedValue) else None
            parts.append(inner if inner is not None else _RUNTIME_HOLE)
        return "".join(parts)
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            left = _fold(node.left, names, depth + 1)
            right = _fold(node.right, names, depth + 1)
            if left is not None and right is not None:
                return left + right
            return None
        if isinstance(node.op, ast.Mod):
            # "SELECT %s FROM payments" % ("pan",) names a column; the more
            # common "... WHERE id = %s" % (id,) supplies a VALUE. Substitute
            # when every operand is statically known, and fall back to the
            # template otherwise -- the template is still the statement, and a
            # %s standing in for a value changes no column name.
            template = _fold(node.left, names, depth + 1)
            if template is None:
                return None
            values = []
            if isinstance(node.right, (ast.Tuple, ast.List)):
                values = [_fold(e, names, depth + 1) for e in node.right.elts]
            else:
                values = [_fold(node.right, names, depth + 1)]
            if values and all(v is not None for v in values):
                try:
                    return template % tuple(values)
                except (TypeError, ValueError, KeyError):
                    return template
            return template
        return None
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("format", "join"):
            if func.attr == "format":
                template = _fold(func.value, names, depth + 1)
                if template is None:
                    return None
                # Substitute the ARGUMENTS, not just the template. `"SELECT {}
                # FROM payments".format("pan")` is a live read of pan, and
                # returning the template alone hid it -- `{}` carries no column
                # name, and the standalone "pan" carries no SQL context.
                # Positional `{}` / `{0}` and named `{col}` only; anything whose
                # value is not statically known keeps its placeholder, which is
                # then reported as unresolved at the execution call.
                args = [_fold(a, names, depth + 1) for a in node.args]
                kwargs = {
                    kw.arg: _fold(kw.value, names, depth + 1)
                    for kw in node.keywords if kw.arg
                }
                if any(a is None for a in args) or any(v is None for v in kwargs.values()):
                    return None
                try:
                    return template.format(*args, **kwargs)
                except (IndexError, KeyError, ValueError):
                    # A template this checker cannot fill is one it must not
                    # pretend to have read.
                    return None
            # "".join([...]) of static parts
            parts = []
            for arg in node.args:
                for element in getattr(arg, "elts", []):
                    piece = _fold(element, names, depth + 1)
                    parts.append(piece if piece is not None else _RUNTIME_HOLE)
            sep = _fold(func.value, names, depth + 1) or ""
            return sep.join(parts) if parts else None
        return None
    if isinstance(node, ast.Name):
        return names.get(node.id)
    return None


_SCOPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _nodes_in_scope(scope: ast.AST):
    """Every node belonging to `scope`, without descending into nested scopes.

    A nested function is its own scope with its own bindings; walking into it
    from here would let one function's `COL = "last4"` mask another's
    `COL = "pan"`. Reviewed on PR #15.
    """
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, _SCOPES):
            continue
        yield child
        for inner in ast.walk(child):
            if inner is child:
                continue
            if isinstance(inner, _SCOPES):
                continue
            yield inner


def _child_scopes(scope: ast.AST):
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, _SCOPES):
            yield child
        else:
            for inner in ast.walk(child):
                if isinstance(inner, _SCOPES):
                    yield inner


def _bindings_for_scope(scope: ast.AST, inherited: dict[str, str]) -> dict[str, str]:
    """Names bound to static SQL in this scope, over what it inherits.

    Innermost wins, which is how the name would actually resolve at runtime.
    Collected in source order so a later rebinding replaces an earlier one.
    """
    names = dict(inherited)
    for node in _nodes_in_scope(scope):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                text = _fold(node.value, names)
                if text is not None:
                    names[target.id] = text
                else:
                    # Rebound to something we cannot resolve: the old value is
                    # no longer what this name means here.
                    names.pop(target.id, None)
    return names


def _expressions_of(stmt):
    """Every expression node in one statement, not entering a nested scope."""
    for node in ast.walk(stmt):
        if isinstance(node, _SCOPES) and node is not stmt:
            continue
        yield node


def _check_expression(node, names, doc_nodes, hits) -> None:
    if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)):
        return
    if id(node) in doc_nodes:
        return
    text = _fold(node, names)
    if text and _SQL_CONTEXT.search(text) and _BARE_COLUMN.search(text):
        excerpt = " ".join(
            line.strip() for line in text.splitlines()
            if _BARE_COLUMN.search(line)
        ) or text.strip()
        hits[(node.lineno, excerpt[:160])] = None


def _has_unresolved_field(node, names) -> bool:
    """An f-string with a substitution this checker cannot resolve.

    `f"SELECT {column} FROM payments"` folds to `SELECT ? FROM payments` --
    a string with no column name in it, which read as clean. Consistent with
    `.format()`, a hole in SQL that actually reaches the database is an
    unresolved statement, not a clean one. Reviewed on PR #15.
    """
    if not isinstance(node, ast.JoinedStr):
        return False

    # Only a hole in the PROJECTION matters -- the part that chooses columns.
    # `f"SELECT last4 FROM payments WHERE id = {loan_id}"` is ordinary
    # parameterised SQL: the hole is a value, it cannot name a column, and
    # failing closed on it would refuse most legitimate queries in the
    # repository. `f"SELECT {column} FROM payments"` is the risky shape,
    # because the hole IS the column list.
    rendered = []
    hole_positions = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            rendered.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            resolved = _fold(value.value, names)
            if resolved is None:
                hole_positions.append(sum(len(r) for r in rendered))
                rendered.append(_RUNTIME_HOLE)
            else:
                rendered.append(resolved)
    if not hole_positions:
        return False
    text = "".join(rendered)
    match = re.search(r"\bfrom\b", text, re.IGNORECASE)
    boundary = match.start() if match else len(text)
    return any(pos < boundary for pos in hole_positions)


def _looks_like_built_sql(node) -> bool:
    """Whether this argument is a STRING being assembled, as opposed to an
    opaque name.

    Fail-closed only means something when the checker has positive reason to
    think it is looking at constructed SQL. A bare name of unknown provenance
    is not that: it is a pass-through parameter, a loop variable, or a
    SQLAlchemy construct, and calling those "unresolved dynamic SQL" would make
    the tool permanently red without telling anyone anything.
    """
    if isinstance(node, (ast.JoinedStr,)):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        if not isinstance(func, ast.Attribute):
            return False
        # `.join` only counts when the receiver is a string literal: SQLAlchemy's
        # `select(...).join(...)` is a query construct, not string building, and
        # treating it as one marked every ORM statement in the repo unresolved.
        if func.attr == "join":
            return isinstance(func.value, ast.Constant) and isinstance(func.value.value, str)
        return func.attr == "format"
    return False


def _check_execution(node, names, hits, params=frozenset(), unresolved=frozenset()) -> None:
    """SQL handed to the database, resolved at THIS call's program point.

    Fails closed: a statement that cannot be resolved is reported rather than
    assumed clean, because a clean exit is what authorises dropping the columns.
    """
    if not isinstance(node, ast.Call):
        return
    func = node.func
    attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if attr not in _EXECUTE_CALLS or not node.args:
        return

    first = node.args[0]
    if _has_unresolved_field(first, names) and attr in _FAIL_CLOSED_CALLS:
        hits[(node.lineno, f"unresolved dynamic SQL passed to {attr}()")] = None
        return
    text = _fold(first, names)
    if text is None:
        # A pass-through wrapper -- `def query(sql, params): cur.execute(sql)` --
        # is not dynamic SQL. Every service here has one, and its callers' query
        # text is scanned where it is written. Failing closed on those would
        # make this checker permanently red and therefore useless: it could
        # never authorise the drop it exists to gate.
        if attr not in _FAIL_CLOSED_CALLS:
            return
        if isinstance(first, ast.Name) and first.id in params:
            return
        # Positive evidence only: a string being built here, or a name this
        # scope assigned from something it could not resolve (`sql = build()`
        # then `execute(sql)`). Anything else is opaque, not dynamic.
        built = _looks_like_built_sql(first)
        assigned_unresolvable = isinstance(first, ast.Name) and first.id in unresolved
        if built or assigned_unresolvable:
            hits[(node.lineno, f"unresolved dynamic SQL passed to {attr}()")] = None
        return
    if _SQL_CONTEXT.search(text) and _BARE_COLUMN.search(text):
        label = f"{first.id} = {text.strip()[:140]}" if isinstance(first, ast.Name) else text.strip()[:160]
        hits[(node.lineno, label)] = None


def _names_assigned_in(body) -> set:
    """Names any statement in `body` binds, however deeply nested."""
    assigned = set()
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned.add(target.id)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                if isinstance(node.target, ast.Name):
                    assigned.add(node.target.id)
    return assigned


def _walk_statements(body, names, doc_nodes, hits, nested, params=frozenset(), unresolved=None):
    """Statements in SOURCE ORDER, binding as we go.

    The previous version precomputed a scope's final binding map and then
    scanned expressions against it, so a rebinding BELOW an execute() decided
    what that execute() meant:

        COL = "pan"
        conn.execute("SELECT " + COL + " FROM payments")   # live read
        COL = "last4"                                      # ...and this hid it

    A name is now whatever it was bound to at the point the statement runs, and
    a later assignment cannot reach backwards. Reviewed on PR #15.
    """
    if unresolved is None:
        unresolved = set()
    for stmt in body:
        if isinstance(stmt, _SCOPES):
            nested.append((stmt, dict(names)))
            continue

        for node in _expressions_of(stmt):
            if isinstance(node, _SCOPES):
                continue
            _check_expression(node, names, doc_nodes, hits)
            _check_execution(node, names, hits, params, unresolved)

        # Nested scopes defined inside this statement (a def inside an if)
        # inherit the bindings as they stand HERE.
        for node in ast.iter_child_nodes(stmt):
            if isinstance(node, _SCOPES):
                nested.append((node, dict(names)))

        # Bodies that run in this same scope.
        #
        # A `with` block runs unconditionally, so its statements continue the
        # same binding sequence. An `if`/`for`/`while`/`try` body MIGHT not run,
        # and assuming it did was the reviewed defect:
        #
        #     COL = "pan"
        #     if migrated: COL = "last4"
        #     execute(f"SELECT {COL} FROM payments")   # reads pan when False
        #
        # Rather than model both branches -- that is the control-flow analysis
        # this tool deliberately does not do -- a name a conditional body
        # rebinds becomes UNCERTAIN afterwards: dropped from the bindings and
        # marked unresolved, so a query built from it fails closed instead of
        # being cleared by one branch. Reviewed on PR #15.
        conditional = isinstance(stmt, (ast.If, ast.For, ast.While, ast.Try, ast.AsyncFor))
        for field in ("body", "orelse", "finalbody"):
            inner = getattr(stmt, field, None)
            if not isinstance(inner, list) or isinstance(stmt, _SCOPES):
                continue
            branch_names = dict(names) if conditional else names
            branch_unresolved = set(unresolved) if conditional else unresolved
            _walk_statements(inner, branch_names, doc_nodes, hits, nested, params, branch_unresolved)
            if conditional:
                for name in _names_assigned_in(inner):
                    # Only names that ARE resolvable SQL strings -- before the
                    # branch or inside it. A name that was never a string is
                    # opaque (a SQLAlchemy construct, a cursor), and calling it
                    # "unresolved dynamic SQL" would refuse every ORM statement
                    # in the repository.
                    if name in names or name in branch_names:
                        names.pop(name, None)
                        unresolved.add(name)
        for handler in getattr(stmt, "handlers", []) or []:
            branch_names = dict(names)
            _walk_statements(handler.body, branch_names, doc_nodes, hits, nested, params, set(unresolved))
            for name in _names_assigned_in(handler.body):
                if name in names or name in branch_names:
                    names.pop(name, None)
                    unresolved.add(name)

        # ...then this statement's own effect on the bindings.
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                text = _fold(stmt.value, names)
                if text is not None:
                    names[target.id] = text
                    unresolved.discard(target.id)
                else:
                    names.pop(target.id, None)
                    # Assigned from STRING construction this checker could not
                    # finish resolving -- an f-string or concatenation with an
                    # unknown piece. If that reaches execute(), it is dynamic
                    # SQL and the run fails closed.
                    #
                    # A plain call (`stmt = select(...)`, `sql = build()`) is
                    # deliberately NOT marked: telling a SQLAlchemy construct
                    # apart from a function returning a SQL string means
                    # following calls across the application, which this tool
                    # does not do. Stated in the module docstring as a limit
                    # rather than guessed at.
                    if _looks_like_built_sql(stmt.value):
                        unresolved.add(target.id)


def _scan_scope(scope, inherited, doc_nodes, hits) -> None:
    """One scope, in source order, then the scopes it contains."""
    names = dict(inherited)
    nested: list[tuple[ast.AST, dict[str, str]]] = []
    args = getattr(scope, "args", None)
    params = frozenset(
        a.arg for a in (list(getattr(args, "posonlyargs", []) or [])
                        + list(getattr(args, "args", []) or [])
                        + list(getattr(args, "kwonlyargs", []) or []))
    ) if args else frozenset()
    _walk_statements(getattr(scope, "body", []), names, doc_nodes, hits, nested, params, set())
    for child, child_names in nested:
        _scan_scope(child, child_names, doc_nodes, hits)


def _sql_literal_hits(source: str) -> list[tuple[int, str]]:
    """(line, text) for every statically-resolvable SQL expression naming a
    legacy column.

    Whole EXPRESSIONS, resolved in their own lexical scope. A projection spread
    over ten lines, built by concatenation, or interpolated from a name is one
    expression; and a name means what it means WHERE IT IS WRITTEN, so one
    function's `COL = "last4"` cannot vouch for another's `COL = "pan"`.
    Docstrings are excluded by position rather than by quoting style.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    hits: dict[tuple[int, str], None] = {}
    _scan_scope(tree, {}, _docstring_nodes(tree), hits)
    return sorted(hits)


def scan() -> list[tuple[str, int, str, str]]:
    hits: list[tuple[str, int, str, str]] = []
    for path in sorted(SERVICES.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = source.splitlines()
        rel = str(path.relative_to(REPO_ROOT))

        # SQL, as whole literals.
        for lineno, excerpt in _sql_literal_hits(source):
            hits.append((rel, lineno, "SQL column reference", excerpt))

        # Everything that is not SQL -- ORM mappings, attribute reads -- is a
        # per-line question and stays one. Real docstrings are excluded by
        # position; see _docstring_lines.
        doc_lines = _docstring_lines(source)
        for n, line in enumerate(lines, 1):
            if n in doc_lines or _is_comment(line):
                continue
            for pattern, kind in PATTERNS:
                if pattern.search(line):
                    hits.append((rel, n, kind, line.strip()))
                    break
    return sorted(hits)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    hits = scan()
    if not hits:
        print("OK: no service source in this revision reads payments.pan or payments.cvv.")
        print()
        print("This does NOT mean production is clean. Before running migration 0031:")
        print("  1. Confirm every deployed service image is built from a revision at")
        print("     or after this one -- no instance predating the last4 cutover may")
        print("     still be serving traffic.")
        print("  2. Confirm db/init no longer seeds pan/cvv (docs/DEBT.md D5b) --")
        print("     a fresh database would otherwise reintroduce card data.")
        print("  3. Then run 0031 with: SET meridian.pan_drop_acknowledged = 'yes';")
        print("See docs/RUNBOOK-pan-cvv-contract.md.")
        return 0

    print(f"REFUSING: {len(hits)} live reference(s) to payments.pan / payments.cvv remain.")
    print("Dropping the columns now would break these call sites:")
    print()
    for path, n, kind, line in hits:
        print(f"  {path}:{n}  [{kind}]")
        if args.verbose:
            print(f"      {line}")
    print()
    print("Remove or gate these before running db/migrations/0031.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
