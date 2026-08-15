"""What the ORM models, the DDL comments and ARCHITECTURE.md say about the
database must match the database.

Every claim pinned here was, until this file existed, written in the present
tense and false:

  * `servicing-service/app/models.py` and `ARCHITECTURE.md` said `balances` is
    "still a single mutable balance column (no ledger)". ADR 0010 step 2 made it
    a projection of `ledger_entries` -- in a migration mirrored into the same
    `db/init/001_schema.sql` whose own comment said "No ledger, no transaction
    history" thirty-four lines above the ledger it creates.
  * Both ORM model docstrings said the `pan`/`cvv` columns "still exist in the
    database" and that the DROP was a future step "on its own PR". `0031` had
    dropped them; `db/init/001_schema.sql` creates neither.
  * `servicing-service/app/routers/loans.py::_display_last4` opened with "THE
    `pan` FALLBACK IS DELIBERATE AND TEMPORARY" and instructed a later PR to
    remove it, describing a card-number read the body no longer performs.

None of these is cosmetic. A PCI reviewer reading `_display_last4` would have
found a PAN read that does not exist; an engineer reading either model would
have believed card columns were still on disk; anyone reasoning about
concurrency from `ARCHITECTURE.md` would have believed a lost update was still
possible. `docs/DEBT.md` D5c records what this costs: a comment that overstates
a defect produces false findings as reliably as one that understates it, and it
produced two here.

Pinned as exact retired sentences rather than as banned words, so the
replacements can quote the history they correct. Reads files and, where a claim
is about the database, checks the database -- those cases need real PostgreSQL
and skip without it, which is stated rather than hidden.
"""
import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVICING_MODELS = REPO / "services" / "servicing-service" / "app" / "models.py"
PAYMENT_MODELS = REPO / "services" / "payment-service" / "app" / "models.py"
LOANS_ROUTER = REPO / "services" / "servicing-service" / "app" / "routers" / "loans.py"
ARCHITECTURE = REPO / "ARCHITECTURE.md"
SCHEMA = REPO / "db" / "init" / "001_schema.sql"
SEED_BULK = REPO / "db" / "init" / "003_seed_bulk.sql"
ROADMAP = REPO / "docs" / "ROADMAP.md"

DATABASE_URL = os.getenv("DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

#: (file, retired sentence, why it is false)
RETIRED = [
    (SERVICING_MODELS, "still a single mutable balance column (no ledger)",
     "db/migrations/0035 makes balances a projection of ledger_entries"),
    (SERVICING_MODELS, "single mutable column, no ledger (debt)",
     "the projection trigger maintains this column"),
    (SERVICING_MODELS, "The columns still exist in the database",
     "db/migrations/0031 dropped payments.pan and payments.cvv"),
    (PAYMENT_MODELS, "the columns themselves still exist in the database",
     "db/migrations/0031 dropped payments.pan and payments.cvv"),
    (LOANS_ROUTER, "THE `pan` FALLBACK IS DELIBERATE AND TEMPORARY",
     "_display_last4 reads last4 only; the fallback was removed"),
    (LOANS_ROUTER, "REMOVE THIS FALLBACK IN PR #15",
     "that removal has happened; the instruction outlived it"),
    (ARCHITECTURE, "is still a single mutable column (no ledger)",
     "balances is a projection of ledger_entries"),
    (SCHEMA, "No ledger, no transaction history",
     "this same file creates ledger_entries"),
    (SEED_BULK, "single mutable float column (no ledger",
     "the ledger exists, and the columns are NUMERIC, not float"),
]


#: A retired sentence may reappear only INSIDE QUOTES -- as something the file is
#: reporting, never as something it is saying. Without that allowance a
#: correction cannot show what it corrected, and the quote is the useful half: it
#: is what someone greps for after meeting the old claim in a stale checkout or
#: an old review comment.
#:
#: The first version of this check looked for nearby words like "said" or
#: "previously" instead. It did not bite: mutating ARCHITECTURE.md back to
#: `balances` "is still a single mutable column (no ledger)" left the test
#: passing, because that paragraph already contains "used to be" two clauses
#: earlier, about something else entirely. A proximity heuristic in a document
#: full of historical narration approves everything. Quote marks are structural,
#: so this asks for those.
OPENING_QUOTES = ('"', "“")


def _stated_as_current_fact(text: str, sentence: str) -> list[str]:
    """Occurrences of `sentence` that are asserted rather than quoted.

    Whitespace is normalised first because prose wraps: a quoted retired
    sentence routinely opens on one line and closes on the next, and a
    line-by-line check would force every correction onto a single long line to
    pass its own test.
    """
    flat = " ".join(text.split())
    flat_sentence = " ".join(sentence.split())
    offenders = []
    start = 0
    while (i := flat.find(flat_sentence, start)) != -1:
        before = flat[:i].rstrip()
        if not before.endswith(OPENING_QUOTES):
            offenders.append(flat[max(0, i - 60):i + len(flat_sentence) + 20])
        start = i + len(flat_sentence)
    return offenders


@pytest.mark.parametrize(
    "path,sentence,why_false", RETIRED,
    ids=[f"{p.stem}:{s[:34]}" for p, s, _ in RETIRED],
)
def test_a_retired_database_claim_is_only_ever_quoted_as_history(path, sentence, why_false):
    assert path.is_file(), f"{path} is gone -- update this pin with the move"
    offenders = _stated_as_current_fact(path.read_text(encoding="utf-8"), sentence)
    assert not offenders, (
        f"{path.relative_to(REPO)} states {sentence!r} as current fact. It is "
        f"false: {why_false}. If this line is narrating history, say so on the "
        f"same line.\n  " + "\n  ".join(o[:150] for o in offenders)
    )


def test_the_fresh_schema_really_does_create_the_ledger():
    """The anchor. If the ledger is ever removed from the fresh install, the
    corrected comments become the stale ones and this fails first."""
    sql = SCHEMA.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS ledger_entries" in sql, (
        "db/init/001_schema.sql no longer creates ledger_entries, so a freshly "
        "initialised database has no ledger while every comment says it does"
    )
    assert "project_ledger_entry" in sql, (
        "the projection function is absent from the fresh schema -- balances "
        "would be a mutable column again, exactly as the retired comments said"
    )


def test_the_fresh_schema_creates_no_card_columns():
    sql = SCHEMA.read_text(encoding="utf-8")
    payments = sql[sql.index("CREATE TABLE IF NOT EXISTS payments"):]
    payments = payments[:payments.index(");")]
    for column in ("pan", "cvv"):
        assert f"\n    {column} " not in payments, (
            f"db/init/001_schema.sql creates payments.{column} again"
        )


def test_the_display_helper_reads_no_card_number():
    """The claim `_display_last4`'s docstring now makes, held to the code.

    Asserted on the function body rather than on the docstring, because the
    docstring is what was wrong.
    """
    import ast

    tree = ast.parse(LOANS_ROUTER.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_display_last4")
    body = ast.unparse(fn)
    body_without_docstring = body.replace(ast.get_docstring(fn) or "", "")
    for attr in (".pan", ".cvv"):
        assert attr not in body_without_docstring, (
            f"_display_last4 reads {attr} -- the columns do not exist, so this "
            f"raises rather than falling back, and PCI scope changes with it"
        )


def test_the_roadmap_planning_surface_agrees_with_start_next():
    """The summary at the top and the decision at the bottom are one claim.

    They disagreed: the planning bullets led with `G-INTAKE-401` and D1, both
    recorded closed further down the same file, while "Start next" named the
    maker-checker work. A reader who trusts the summary is sent at finished work.
    """
    text = ROADMAP.read_text(encoding="utf-8")
    surface = text[text.index("## Current planning surface"):text.index("## Status at a glance")]

    for closed in ("G-INTAKE-401", "G-D1"):
        assert f"close `{closed}`" not in surface, (
            f"the planning surface still directs the reader to close {closed}, "
            f"which this file records as closed"
        )
    assert "G-SERVICING-ROLE" in surface and "G-MAKER-CHECKER" in surface, (
        "the planning surface does not name the work 'Start next' selects"
    )
    start_next = text[text.index("## Start next"):]
    for gap in ("G-SERVICING-ROLE", "G-MAKER-CHECKER"):
        assert gap in start_next[:1200], (
            f"'Start next' no longer leads with {gap} while the summary does"
        )


def test_the_roadmap_does_not_call_week_5_landed():
    """Week 5's row is Partial (`G-D2-LEGACY`). The summary called Weeks 1-5
    landed, which is the matrix and its own summary disagreeing about a money
    path."""
    text = ROADMAP.read_text(encoding="utf-8")
    surface = text[text.index("## Current planning surface"):text.index("## Status at a glance")]
    assert "Weeks 1–5 are landed" not in surface and "Weeks 1-5 are landed" not in surface, (
        "the planning surface calls Weeks 1-5 landed while the Week 5 row is "
        "Partial -- servicing's legacy POST /payments is not idempotent"
    )


def test_the_servicing_token_gap_row_counts_every_guarded_route():
    """The closed-gap row said 'all four'. There are five, and the paragraph
    beneath that table is *about* hand-written lists going stale."""
    text = ROADMAP.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines()
               if line.startswith("| **G-SERVICING-TOKEN**"))
    assert "all five" in row, "the row no longer states how many routes are guarded"
    # "all four" may survive only as the quoted history of what this row used to
    # claim -- the same allowance the retired-sentence pins make, for the same
    # reason: a correction is more useful when it shows what it corrected.
    if "all four" in row:
        assert not _stated_as_current_fact(row, "all four"), (
            "the G-SERVICING-TOKEN row states four routes as current fact; the "
            "legacy POST /payments is guarded too, and the token test "
            "parametrizes over five"
        )


#: Built the way `db/tests/test_schema_parity.py` builds one: the `db/init`
#: schema files into a throwaway schema.
#:
#: Deliberately NOT whatever `DATABASE_URL` happens to point at. A developer's
#: compose volume is only initialised when its data directory is empty, so a
#: volume created before a schema change keeps serving the old shape forever --
#: the volume on the machine this test was written on was created 2026-08-12 and
#: has no `ledger_entries` at all, three days after the ledger merged. Asserting
#: against it would have "proved" the ORM comments right for the wrong reason on
#: a fresh volume, and failed for the wrong reason on a stale one.
FRESH_SCHEMA = "orm_claims_fresh_init"
INIT_FILES = (
    "001_schema.sql", "004_decision_events.sql", "005_manual_reviews.sql",
    "006_decision_attempts.sql", "007_ledger_opening_balances.sql",
)


@pytest.fixture
def fresh_init():
    """A database built from `db/init`, i.e. what a brand-new deployment gets."""
    import psycopg2

    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = False
    with connection.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {FRESH_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {FRESH_SCHEMA}")
    connection.commit()
    for filename in INIT_FILES:
        with connection.cursor() as cur:
            cur.execute(f"SET search_path TO {FRESH_SCHEMA}")
            cur.execute((REPO / "db" / "init" / filename).read_text(encoding="utf-8"))
        connection.commit()
    yield connection
    with connection.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {FRESH_SCHEMA} CASCADE")
    connection.commit()
    connection.close()


@needs_db
def test_a_fresh_install_has_no_card_columns(fresh_init):
    """The claim is about the database, so it is checked against one."""
    with fresh_init.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'payments'", (FRESH_SCHEMA,)
        )
        columns = {r[0] for r in cur.fetchall()}
    assert columns, "no payments table in the fresh install -- nothing was checked"
    assert "pan" not in columns and "cvv" not in columns, (
        f"a freshly initialised database creates card columns: "
        f"{sorted(columns & {'pan', 'cvv'})}"
    )


@needs_db
def test_a_fresh_install_makes_balances_a_projection(fresh_init):
    """`balances` must be maintained by the ledger trigger on a new deployment,
    not just on one that has run the migrations."""
    with fresh_init.cursor() as cur:
        cur.execute(f"SET search_path TO {FRESH_SCHEMA}")
        cur.execute("SELECT to_regclass(%s)", (f"{FRESH_SCHEMA}.ledger_entries",))
        assert cur.fetchone()[0] is not None, (
            "a fresh install has no ledger_entries, so the ORM and ARCHITECTURE "
            "claims about a projection would be false for every new deployment"
        )
        cur.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass "
            "AND NOT tgisinternal", (f"{FRESH_SCHEMA}.ledger_entries",)
        )
        triggers = {r[0] for r in cur.fetchall()}
    assert "ledger_entries_project" in triggers, (
        f"the projection trigger is not installed on a fresh install (found "
        f"{sorted(triggers)}), so `balances` is maintained by whoever writes it "
        f"last after all"
    )
