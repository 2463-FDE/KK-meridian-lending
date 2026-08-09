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
]

# A bare `pan` / `cvv` token counts as a column reference only on a line that is
# also doing SQL. Without that qualifier the word appears in ordinary prose.
_SQL_CONTEXT = re.compile(r"\b(select|insert|update|delete|set|where|values)\b", re.IGNORECASE)
# Case-insensitive: PostgreSQL folds an unquoted identifier to lower case, so
# `SELECT PAN` reads the very column being dropped. This was case-sensitive and
# missed it. Reviewed on PR #15.
_BARE_COLUMN = re.compile(r"\b(pan|cvv)\b", re.IGNORECASE)

# How many lines above a bare column still count as the same SQL statement.
# Raw SQL here is written as adjacent string literals, and a projection is
# routinely split across them:
#
#     "SELECT id, "
#     "pan, "
#     "last4 FROM payments"
#
# Requiring the keyword and the column on the SAME line missed every one of
# those, so the checker printed OK over a live reader -- and that green result
# is the runbook's prerequisite for acknowledging the destructive migration.
# Small on purpose: a statement-body window, not a file-wide search, so an
# unrelated `pan` far below a SELECT is still not a hit. Reviewed on PR #15.
_SQL_WINDOW = 6

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

        # Real docstrings only. This codebase documents the PAN/CVV defect at
        # length in module docstrings; those sentences are documentation, not
        # reads, and counting them buries the real hits. A triple-quoted SQL
        # constant is NOT one of them -- see _docstring_lines.
        doc_lines = _docstring_lines(source)
        for n, line in enumerate(lines, 1):
            if n in doc_lines or _is_comment(line):
                continue

            matched = None
            for pattern, kind in PATTERNS:
                if pattern.search(line):
                    matched = kind
                    break
            if matched is None and _BARE_COLUMN.search(line):
                # The keyword may be on this line or on one of the few above it,
                # because a projection split across adjacent string literals is
                # one statement written over several lines.
                start = max(0, n - 1 - _SQL_WINDOW)
                window = lines[start:n]
                if any(
                    _SQL_CONTEXT.search(w)
                    for w in window
                    if not _is_comment(w)
                ):
                    matched = "SQL column reference"
            if matched:
                hits.append((str(path.relative_to(REPO_ROOT)), n, matched, line.strip()))
    return hits


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
