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
            # "SELECT pan FROM payments WHERE id = %s" % (id,) -- the left side
            # is the statement; the right side is parameters.
            return _fold(node.left, names, depth + 1)
        return None
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("format", "join"):
            if func.attr == "format":
                return _fold(func.value, names, depth + 1)
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


def _static_strings(tree: ast.AST) -> dict[str, str]:
    """Names bound to SQL that can be resolved statically.

    Covers the common shape the reviewer named: a query assigned to a variable
    and handed to `execute` further down. Assignments are walked in source
    order so a later rebinding wins, which is the reading a human would give
    the file.
    """
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                text = _fold(node.value, names)
                if text is not None:
                    names[target.id] = text
    return names


def _sql_literal_hits(source: str) -> list[tuple[int, str]]:
    """(line, text) for every statically-resolvable SQL expression naming a
    legacy column.

    Whole EXPRESSIONS, not lines and not bare literals: a projection spread
    over ten lines, built by concatenation, or interpolated into an f-string is
    one expression, and the line window this replaced could only ever guess at
    its extent. Docstrings are excluded by position rather than by quoting
    style.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    doc_nodes = _docstring_nodes(tree)
    names = _static_strings(tree)

    hits: dict[tuple[int, str], None] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)):
            continue
        if id(node) in doc_nodes:
            continue
        text = _fold(node, names)
        if not text:
            continue
        if _SQL_CONTEXT.search(text) and _BARE_COLUMN.search(text):
            excerpt = " ".join(
                line.strip() for line in text.splitlines()
                if _BARE_COLUMN.search(line)
            ) or text.strip()
            hits[(node.lineno, excerpt[:160])] = None

    # A name carrying SQL is reported where it is EXECUTED, since that is the
    # live read -- the assignment may be far away or in another module.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if attr not in _EXECUTE_CALLS:
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Name):
                continue
            text = names.get(arg.id)
            if text and _SQL_CONTEXT.search(text) and _BARE_COLUMN.search(text):
                hits[(node.lineno, f"{arg.id} = {text.strip()[:140]}")] = None

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
