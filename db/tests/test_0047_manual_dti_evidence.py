"""db/migrations/0047 -- manual DTI as evidence, and only as evidence (RF-25).

The client answered RF-25 on 2026-08-29: staff may apply DTI manually, but only
on a REFERRED application, only as an underwriter or admin, and only from approved
SYNTHETIC source documents -- with gross monthly income, monthly debt obligations,
source-document references, the calculation, staff identity, role, timestamp and
reason all required, and a bare percentage explicitly insufficient.

And the rule that governs the design: **a manual DTI is human-review EVIDENCE and
must not approve, deny, override, mutate a decision or trigger model output.**

What the database guarantees on its own -- which after Codex round 2 is more than
this file first claimed. Referred-only and underwriter/admin-only were described
here as route concerns tested with the API; both are now enforced by
`manual_dti_permitted` and covered below (MDTI-M01). The schema holds: the
application is currently REFERRED, the assessor is the active user the row names
and holds the role it records, the ratio is reproducible from its own inputs, the
evidence is append-only, a document that is not approved and synthetic cannot be
cited, and a cited document cannot change underneath the evidence citing it.

The API enforces these again at its own layer -- a route that let a caller supply
an identity would be wrong even with the database refusing the row -- but the
guarantees here do not depend on it.

Against real PostgreSQL in a throwaway schema built from `db/init` and migrated
with 0047 -- so these are migration-path tests as well as constraint tests, and
`db/init` and the migration are proven to agree.
"""
import os
import pathlib
import threading
from decimal import Decimal

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.getenv("DATABASE_URL")

REPO = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = "manual_dti_test"
INIT = REPO / "db" / "init"
MIGRATION = REPO / "db" / "migrations" / "0047_manual_dti_evidence.sql"
INIT_FILES = ("001_schema.sql", "002_seed.sql", "003_seed_bulk.sql",
              "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql", "007_ledger_opening_balances.sql")


def _apply_migration(conn):
    sql = MIGRATION.read_text(encoding="utf-8")
    sql = sql.replace("BEGIN;", "", 1)
    sql = "".join(sql.rsplit("COMMIT;", 1))
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql)
    conn.commit()


@pytest.fixture(scope="module")
def db():
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.commit()
        for name in INIT_FILES:
            path = INIT / name
            if not path.exists():
                continue
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                cur.execute(path.read_text(encoding="utf-8"))
            conn.commit()
        _apply_migration(conn)
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.commit()
        conn.close()


@pytest.fixture()
def cur(db):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        yield c
    db.rollback()


def _a_referred_application(c):
    c.execute("INSERT INTO applicants (name) VALUES ('DTI Fixture') RETURNING id")
    applicant = c.fetchone()["id"]
    c.execute(
        "INSERT INTO applications (applicant_id, amount, term_months, status) "
        "VALUES (%s, 15000, 36, 'in_review') RETURNING id", (applicant,))
    app_id = c.fetchone()["id"]
    c.execute("INSERT INTO decisions (app_id, outcome) VALUES (%s, 'refer')", (app_id,))
    return app_id


def _a_staff_user(c, role="underwriter"):
    c.execute(
        "INSERT INTO users (username, password_hash, role, display_name) "
        "VALUES (%s, 'x', %s, 'DTI Staff') RETURNING id",
        (f"dti-{role}-{os.urandom(4).hex()}", role))
    return c.fetchone()["id"]


def _doc(c, ref="SYN-PAYSTUB-001"):
    c.execute("SELECT id FROM manual_dti_source_documents WHERE doc_ref = %s", (ref,))
    return c.fetchone()["id"]


def _assess(c, app_id, staff, *, income="6000.00", debt="1800.00", dti_bp=3000,
            role="underwriter", reason="Verified from synthetic paystub"):
    c.execute(
        "INSERT INTO manual_dti_assessments "
        "(app_id, assessed_by, assessed_role, gross_monthly_income, "
        " monthly_debt_obligations, dti_bp, reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (app_id, staff, role, income, debt, dti_bp, reason))
    return c.fetchone()["id"]


def _attach(c, assessment_id, doc_id):
    c.execute(
        "INSERT INTO manual_dti_assessment_documents (assessment_id, document_id) "
        "VALUES (%s, %s)", (assessment_id, doc_id))


def _complete(c):
    """One valid assessment with a document, committed to the savepoint level."""
    app_id = _a_referred_application(c)
    staff = _a_staff_user(c)
    aid = _assess(c, app_id, staff)
    _attach(c, aid, _doc(c))
    return app_id, staff, aid


# --------------------------------------------------------------------------
# Migration path.
# --------------------------------------------------------------------------

def test_the_migration_is_re_runnable(db):
    """`db/init` already carries these tables, so the fixture applied 0047 on top
    of a schema that had them. Applying it again must still succeed, or the two
    definitions have drifted."""
    _apply_migration(db)
    _apply_migration(db)


def test_the_registry_ships_approved_and_unapproved_rows(cur):
    """The unapproved row is deliberate: a registry where everything is approved
    cannot demonstrate the refusal the client's rule requires."""
    cur.execute("SELECT count(*) AS n FROM manual_dti_source_documents WHERE approved")
    assert cur.fetchone()["n"] >= 5
    cur.execute(
        "SELECT count(*) AS n FROM manual_dti_source_documents WHERE NOT approved")
    assert cur.fetchone()["n"] >= 1


def test_every_registry_row_is_synthetic(cur):
    cur.execute("SELECT bool_and(is_synthetic) AS all_syn FROM manual_dti_source_documents")
    assert cur.fetchone()["all_syn"] is True


def test_a_non_synthetic_document_cannot_be_registered(cur):
    """Synthetic-only is a CHECK, not a default somebody can override."""
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO manual_dti_source_documents "
            "(doc_ref, kind, label, is_synthetic, approved) "
            "VALUES ('REAL-001', 'paystub', 'A real applicant paystub', FALSE, TRUE)")
    cur.execute("ROLLBACK TO SAVEPOINT s")


# --------------------------------------------------------------------------
# The evidence itself.
# --------------------------------------------------------------------------

def test_a_complete_assessment_persists_every_required_field(cur):
    app_id, staff, aid = _complete(cur)
    cur.execute(
        "SELECT app_id, assessed_by, assessed_role, gross_monthly_income, "
        "       monthly_debt_obligations, dti_bp, reason, assessed_at "
        "  FROM manual_dti_assessments WHERE id = %s", (aid,))
    row = cur.fetchone()
    assert row["app_id"] == app_id
    assert row["assessed_by"] == staff                 # identity, as a real FK
    assert row["assessed_role"] == "underwriter"       # role as exercised
    assert row["gross_monthly_income"] == Decimal("6000.00")
    assert row["monthly_debt_obligations"] == Decimal("1800.00")
    assert row["dti_bp"] == 3000
    assert row["reason"] == "Verified from synthetic paystub"
    assert row["assessed_at"] is not None


def test_the_source_documents_are_persisted_and_readable(cur):
    _, _, aid = _complete(cur)
    cur.execute(
        "SELECT d.doc_ref FROM manual_dti_assessment_documents l "
        "  JOIN manual_dti_source_documents d ON d.id = l.document_id "
        " WHERE l.assessment_id = %s", (aid,))
    assert [r["doc_ref"] for r in cur.fetchall()] == ["SYN-PAYSTUB-001"]


def test_the_ratio_must_follow_from_its_own_inputs(cur):
    """A bare percentage is not evidence -- enforced in the schema, not the route.

    $1,800 against $6,000 is 3000bp. Storing any other figure beside those two
    inputs is refused, so a caller cannot supply a DTI that does not follow from
    the evidence it claims to rest on.
    """
    app_id = _a_referred_application(cur)
    staff = _a_staff_user(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _assess(cur, app_id, staff, dti_bp=2500)
    cur.execute("ROLLBACK TO SAVEPOINT s")


@pytest.mark.parametrize("income,debt,expected_bp", [
    ("6000.00", "1800.00", 3000),
    ("5000.00", "2150.00", 4300),   # the 43% the retired policy talked about
    ("4000.00", "0.00", 0),         # no obligations is a real answer
    ("7333.33", "2444.44", 3333),   # rounding, exercised rather than assumed
])
def test_the_ratio_is_reproducible_across_inputs(cur, income, debt, expected_bp):
    app_id = _a_referred_application(cur)
    staff = _a_staff_user(cur)
    aid = _assess(cur, app_id, staff, income=income, debt=debt, dti_bp=expected_bp)
    _attach(cur, aid, _doc(cur))
    cur.execute("SELECT dti_bp FROM manual_dti_assessments WHERE id = %s", (aid,))
    assert cur.fetchone()["dti_bp"] == expected_bp


@pytest.mark.parametrize("income,debt", [("0.00", "100.00"), ("-1.00", "100.00")])
def test_income_must_be_positive(cur, income, debt):
    """Zero income is not a divisor, and a ratio against it would be meaningless."""
    app_id = _a_referred_application(cur)
    staff = _a_staff_user(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises((psycopg2.errors.CheckViolation, psycopg2.errors.DivisionByZero)):
        _assess(cur, app_id, staff, income=income, debt=debt, dti_bp=0)
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_negative_debt_is_refused(cur):
    app_id = _a_referred_application(cur)
    staff = _a_staff_user(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _assess(cur, app_id, staff, debt="-1.00", dti_bp=0)
    cur.execute("ROLLBACK TO SAVEPOINT s")


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_reason_is_refused(cur, blank):
    app_id = _a_referred_application(cur)
    staff = _a_staff_user(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _assess(cur, app_id, staff, reason=blank)
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_only_the_two_authorised_roles_may_be_recorded(cur):
    """A CSR assessment cannot be stored even if a future route forgets to check."""
    app_id = _a_referred_application(cur)
    staff = _a_staff_user(cur, role="csr")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _assess(cur, app_id, staff, role="csr")
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_staff_identity_must_reference_a_real_user(cur):
    """Either the trigger or the foreign key -- both refuse, and the trigger
    reaches it first with a message that names the user."""
    app_id = _a_referred_application(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises((psycopg2.errors.ForeignKeyViolation,
                        psycopg2.errors.RaiseException)):
        _assess(cur, app_id, 999999)
    cur.execute("ROLLBACK TO SAVEPOINT s")


# --------------------------------------------------------------------------
# BDTI-01: referred applications only.
# --------------------------------------------------------------------------

def _an_application_in_state(c, *, status, outcome):
    """An application whose decision is `outcome`, or with no decision at all."""
    c.execute("INSERT INTO applicants (name) VALUES ('DTI State') RETURNING id")
    applicant = c.fetchone()["id"]
    c.execute(
        "INSERT INTO applications (applicant_id, amount, term_months, status) "
        "VALUES (%s, 15000, 36, %s) RETURNING id", (applicant, status))
    app_id = c.fetchone()["id"]
    if outcome is not None:
        c.execute("INSERT INTO decisions (app_id, outcome) VALUES (%s, %s)",
                  (app_id, outcome))
    return app_id


@pytest.mark.parametrize("status,outcome,expected", [
    ("submitted", None, "no decision on record"),
    ("approved", "approve", "not referred"),
    ("denied", "deny", "not referred"),
    ("funded", "approve", "not referred"),
])
def test_only_a_referred_application_may_be_assessed(cur, status, outcome, expected):
    """Codex review BDTI-01.

    The client's rule is manual DTI on a REFERRED application only. The first
    version of this migration left that entirely to the route -- while
    `decisions.outcome` sat right here, which made it a choice not to enforce
    something the database could. Every fixture in this file built a referred
    application, so no test would have noticed.

    A submitted application with no decision at all is refused separately from one
    that was decided the wrong way, because "nobody has looked yet" and "somebody
    looked and approved" are different states and the message should say which.
    """
    staff = _a_staff_user(cur)
    app_id = _an_application_in_state(cur, status=status, outcome=outcome)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _assess(cur, app_id, staff)
    cur.execute("ROLLBACK TO SAVEPOINT s")
    assert expected in str(exc.value)


# --------------------------------------------------------------------------
# BDTI-02: the role recorded must be the role the person holds.
# --------------------------------------------------------------------------

def test_a_csr_cannot_be_recorded_as_an_underwriter(cur):
    """Codex review BDTI-02, and it falsified a claim in the PR body.

    The CHECK constrained the assessed_role STRING to underwriter/admin and tied
    it to nobody, so a CSR's user id stored with `assessed_role = 'underwriter'`
    was accepted -- while the PR claimed "a CSR assessment cannot be stored even if
    a future route forgets to check". The earlier test only rejected the string.
    """
    app_id = _a_referred_application(cur)
    csr = _a_staff_user(cur, role="csr")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _assess(cur, app_id, csr, role="underwriter")
    cur.execute("ROLLBACK TO SAVEPOINT s")
    assert "holds role csr" in str(exc.value)


def test_an_underwriter_cannot_be_recorded_as_an_admin(cur):
    """The mismatch is refused in both directions, not only downward."""
    app_id = _a_referred_application(cur)
    uw = _a_staff_user(cur, role="underwriter")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException):
        _assess(cur, app_id, uw, role="admin")
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_a_borrower_cannot_record_an_assessment(cur):
    app_id = _a_referred_application(cur)
    borrower = _a_staff_user(cur, role="borrower")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException):
        _assess(cur, app_id, borrower, role="underwriter")
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_a_deactivated_user_cannot_record_an_assessment(cur):
    """Authority has to be current. Evidence signed by a deactivated account
    would be worth less than it appears."""
    app_id = _a_referred_application(cur)
    staff = _a_staff_user(cur)
    cur.execute("UPDATE users SET is_active = FALSE WHERE id = %s", (staff,))
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _assess(cur, app_id, staff)
    cur.execute("ROLLBACK TO SAVEPOINT s")
    assert "not active" in str(exc.value)


@pytest.mark.parametrize("role", ["underwriter", "admin"])
def test_both_authorised_roles_are_accepted_when_they_match(cur, role):
    """The positive half. Without it the trigger could refuse everything."""
    app_id = _a_referred_application(cur)
    staff = _a_staff_user(cur, role=role)
    aid = _assess(cur, app_id, staff, role=role)
    _attach(cur, aid, _doc(cur))
    cur.execute("SELECT assessed_role FROM manual_dti_assessments WHERE id = %s", (aid,))
    assert cur.fetchone()["assessed_role"] == role


# --------------------------------------------------------------------------
# BDTI-03: a cited document cannot change underneath its evidence.
# --------------------------------------------------------------------------

def test_a_cited_document_cannot_be_updated(cur):
    """Codex review BDTI-03.

    Assessments and link rows were append-only; the REGISTRY was not, so
    `doc_ref`, `kind`, `label` and `approved` could all be changed after an
    assessment cited the row -- silently altering what that evidence rests on.
    """
    _, _, aid = _complete(cur)
    cited = _doc(cur)
    for column, value in (("approved", "FALSE"), ("doc_ref", "'SYN-RENAMED'"),
                          ("label", "'Something else'")):
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.RaiseException) as exc:
            cur.execute(
                f"UPDATE manual_dti_source_documents SET {column} = {value} "
                " WHERE id = %s", (cited,))
        cur.execute("ROLLBACK TO SAVEPOINT s")
        assert "cited by" in str(exc.value)


def test_a_cited_document_cannot_be_deleted(cur):
    _, _, aid = _complete(cur)
    cited = _doc(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises((psycopg2.errors.RaiseException,
                        psycopg2.errors.ForeignKeyViolation)):
        cur.execute("DELETE FROM manual_dti_source_documents WHERE id = %s", (cited,))
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_an_uncited_document_may_still_be_edited_and_approved(cur):
    """Scoped to CITED rows, deliberately.

    The registry is meant to grow, and approving a new document is exactly the
    workflow it exists for. Freezing the whole table would have made the fix
    bigger than the defect and broken the one operation the table is for.
    """
    cur.execute(
        "INSERT INTO manual_dti_source_documents (doc_ref, kind, label) "
        "VALUES ('SYN-NEW-001', 'paystub', 'Newly added, not yet approved') "
        "RETURNING id")
    fresh = cur.fetchone()["id"]
    cur.execute("UPDATE manual_dti_source_documents SET approved = TRUE "
                " WHERE id = %s", (fresh,))
    cur.execute("SELECT approved FROM manual_dti_source_documents WHERE id = %s",
                (fresh,))
    assert cur.fetchone()["approved"] is True
    cur.execute("DELETE FROM manual_dti_source_documents WHERE id = %s", (fresh,))


# --------------------------------------------------------------------------
# Source documents.
# --------------------------------------------------------------------------

def test_an_assessment_with_no_source_document_cannot_commit(cur, db):
    """"A bare percentage is not enough" -- enforced at COMMIT.

    The links are written after the assessment row, so this cannot be a CHECK. A
    deferred constraint trigger fires at commit, by which time the documents are
    either there or they are not.
    """
    app_id = _a_referred_application(cur)
    staff = _a_staff_user(cur)
    _assess(cur, app_id, staff)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        db.commit()
    assert "cites no source document" in str(exc.value)
    db.rollback()


def test_an_unapproved_document_cannot_be_cited(cur):
    _, _, aid = _complete(cur)
    unapproved = _doc(cur, "SYN-DRAFT-001")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _attach(cur, aid, unapproved)
    cur.execute("ROLLBACK TO SAVEPOINT s")
    assert "not approved" in str(exc.value)


def test_a_document_outside_the_registry_cannot_be_cited(cur):
    _, _, aid = _complete(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises((psycopg2.errors.ForeignKeyViolation,
                        psycopg2.errors.RaiseException)):
        _attach(cur, aid, 999999)
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_several_documents_may_support_one_assessment(cur):
    _, _, aid = _complete(cur)
    _attach(cur, aid, _doc(cur, "SYN-BANK-001"))
    _attach(cur, aid, _doc(cur, "SYN-DEBTSCH-001"))
    cur.execute(
        "SELECT count(*) AS n FROM manual_dti_assessment_documents "
        " WHERE assessment_id = %s", (aid,))
    assert cur.fetchone()["n"] == 3


# --------------------------------------------------------------------------
# Append-only.
# --------------------------------------------------------------------------

def test_an_assessment_cannot_be_updated(cur):
    _, _, aid = _complete(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("UPDATE manual_dti_assessments SET dti_bp = 1 WHERE id = %s", (aid,))
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_an_assessment_cannot_be_deleted(cur):
    _, _, aid = _complete(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("DELETE FROM manual_dti_assessments WHERE id = %s", (aid,))
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_a_document_link_cannot_be_detached(cur):
    """Detaching a document would change what an assessment rests on while leaving
    its ratio and reason untouched."""
    _, _, aid = _complete(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("DELETE FROM manual_dti_assessment_documents "
                    " WHERE assessment_id = %s", (aid,))
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_a_correction_is_a_second_row_not_an_edit(cur):
    """Unlike `manual_reviews`, this is not UNIQUE on app_id.

    A later assessment -- a second reviewer, or the same one with new documents --
    is an additional record. The first one stays, which is what makes the history
    readable.
    """
    app_id, staff, first = _complete(cur)
    second = _assess(cur, app_id, staff, income="6000.00", debt="1200.00", dti_bp=2000)
    _attach(cur, second, _doc(cur, "SYN-BANK-001"))
    cur.execute(
        "SELECT count(*) AS n FROM manual_dti_assessments WHERE app_id = %s", (app_id,))
    assert cur.fetchone()["n"] == 2


# --------------------------------------------------------------------------
# THE CLIENT'S CENTRAL RULE: evidence only.
# --------------------------------------------------------------------------

def test_recording_dti_evidence_changes_no_decision_surface(cur, db):
    """"A manual DTI is human-review EVIDENCE and must not approve, deny,
    override, mutate a decision or trigger model output."

    The four surfaces a decision lives on are captured BEFORE and compared AFTER.
    This is the schema-level half: no trigger, default or cascade introduced by
    0047 touches any of them. The route-level half -- that the API does not write
    them either -- belongs with the API and is tested there.
    """
    app_id = _a_referred_application(cur)
    staff = _a_staff_user(cur)

    def snapshot():
        out = {}
        cur.execute("SELECT outcome FROM decisions WHERE app_id = %s", (app_id,))
        out["decision"] = cur.fetchall()
        cur.execute("SELECT status FROM applications WHERE id = %s", (app_id,))
        out["status"] = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM manual_reviews WHERE app_id = %s", (app_id,))
        out["manual_reviews"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM decision_events WHERE app_id = %s", (app_id,))
        out["decision_events"] = cur.fetchone()["n"]
        return out

    before = snapshot()
    aid = _assess(cur, app_id, staff)
    _attach(cur, aid, _doc(cur))
    db.commit()
    after = snapshot()

    assert after == before, (
        "recording manual DTI evidence changed a decision surface: "
        f"before={before} after={after}")

    # And the evidence really was written -- otherwise this passes vacuously.
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessments WHERE app_id = %s",
                (app_id,))
    assert cur.fetchone()["n"] == 1
    db.rollback()


# --------------------------------------------------------------------------
# MDTI-01: the checks must HOLD the rows they rely on.
#
# Codex round 2. Each trigger read committed state and held no lock, so under
# READ COMMITTED a concurrent writer could invalidate the premise between the
# check and the commit. Three sites, three races -- and each needs TWO
# connections that genuinely overlap. A sequential "write, commit, write" proves
# nothing about a lock, because the second writer looks after the first finished.
#
# The shape of every case below:
#   T1 begins, reaches the protected state, does NOT commit
#   T2 attempts the conflicting change from another connection, in a thread
#   assert T2 is STILL BLOCKED (thread alive)   <- the overlap proof
#   T1 commits
#   assert T2's outcome and the final database truth
# --------------------------------------------------------------------------

def _conn():
    c = psycopg2.connect(DATABASE_URL)
    c.autocommit = False
    with c.cursor() as cur:
        cur.execute("SET search_path TO " + SCHEMA)
    c.commit()
    return c


def _blocked_writer(sql, params):
    """A conflicting write on its own connection, in a thread."""
    outcome = {}

    def _run():
        c = _conn()
        try:
            with c.cursor() as cur:
                cur.execute("SET search_path TO " + SCHEMA)
                cur.execute("SET LOCAL lock_timeout = '30s'")
                cur.execute(sql, params)
            c.commit()
            outcome["state"] = "committed"
        except Exception as exc:                      # noqa: BLE001 - reported
            outcome["state"] = type(exc).__name__
            c.rollback()
        finally:
            c.close()

    return threading.Thread(target=_run, daemon=True), outcome


def _committed_fixture():
    """A referred application, staff user and document, all COMMITTED.

    These races are about a second CONNECTION seeing this transaction's state, so
    the setup has to be visible outside the transaction under test.
    """
    c = _conn()
    with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SET search_path TO " + SCHEMA)
        app_id = _a_referred_application(cur)
        staff = _a_staff_user(cur)
        cur.execute("SELECT id FROM manual_dti_source_documents "
                    " WHERE doc_ref = 'SYN-PAYSTUB-001'")
        doc = cur.fetchone()["id"]
    c.commit()
    return c, app_id, staff, doc


def test_a_decision_cannot_change_under_an_in_flight_assessment(db):
    """MDTI-01 site A: T2 approves the application while T1 is recording DTI."""
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    setup, app_id, staff, doc = _committed_fixture()
    t1 = _conn()
    try:
        with t1.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c1:
            c1.execute("SET search_path TO " + SCHEMA)
            aid = _assess(c1, app_id, staff)
            _attach(c1, aid, doc)

        worker, outcome = _blocked_writer(
            "UPDATE decisions SET outcome = 'approve' WHERE app_id = %s", (app_id,))
        worker.start()
        worker.join(timeout=3.0)
        assert worker.is_alive(), (
            "the decision update completed while the assessment was still open, "
            "so the trigger is not holding the decisions row and evidence can "
            "land against an application that is no longer referred")

        t1.commit()
        worker.join(timeout=30.0)
        assert outcome.get("state") == "committed", outcome

        with t1.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c1:
            c1.execute("SET search_path TO " + SCHEMA)
            c1.execute("SELECT count(*) AS n FROM manual_dti_assessments "
                       " WHERE app_id = %s", (app_id,))
            assert c1.fetchone()["n"] == 1
    finally:
        t1.rollback()
        t1.close()
        setup.close()


def test_authority_cannot_be_revoked_under_an_in_flight_assessment(db):
    """MDTI-01 site B: T2 demotes and deactivates the assessor mid-assessment."""
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    setup, app_id, staff, doc = _committed_fixture()
    t1 = _conn()
    try:
        with t1.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c1:
            c1.execute("SET search_path TO " + SCHEMA)
            aid = _assess(c1, app_id, staff)
            _attach(c1, aid, doc)

        worker, outcome = _blocked_writer(
            "UPDATE users SET role = 'csr', is_active = FALSE WHERE id = %s",
            (staff,))
        worker.start()
        worker.join(timeout=3.0)
        assert worker.is_alive(), (
            "the user was demoted while the assessment was still open, so the "
            "trigger is not holding the users row and evidence can be signed "
            "with authority its author no longer holds")

        t1.commit()
        worker.join(timeout=30.0)
        assert outcome.get("state") == "committed", outcome
    finally:
        t1.rollback()
        t1.close()
        setup.close()


def test_a_cited_document_cannot_be_rewritten_under_an_in_flight_citation(db):
    """MDTI-01 site C, and the subtlest of the three.

    The freeze trigger counts COMMITTED citations. With no lock, T2 updating the
    registry row while T1's citation is uncommitted counts zero, concludes the
    document is uncited, and rewrites it underneath evidence about to cite it.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    setup, app_id, staff, doc = _committed_fixture()
    t1 = _conn()
    try:
        with t1.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c1:
            c1.execute("SET search_path TO " + SCHEMA)
            aid = _assess(c1, app_id, staff)
            _attach(c1, aid, doc)          # citation NOT yet committed

        worker, outcome = _blocked_writer(
            "UPDATE manual_dti_source_documents SET approved = FALSE WHERE id = %s",
            (doc,))
        worker.start()
        worker.join(timeout=3.0)
        assert worker.is_alive(), (
            "the registry row was rewritten while the citation was uncommitted -- "
            "the freeze trigger counted zero citations because the citing "
            "transaction held no lock on the document")

        t1.commit()
        worker.join(timeout=30.0)
        assert outcome.get("state") == "RaiseException", (
            "expected the registry update to be refused once the citation "
            "committed; got " + repr(outcome.get("state")))

        with t1.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c1:
            c1.execute("SET search_path TO " + SCHEMA)
            c1.execute("SELECT approved FROM manual_dti_source_documents "
                       " WHERE id = %s", (doc,))
            assert c1.fetchone()["approved"] is True, (
                "the cited document was un-approved after all")
    finally:
        t1.rollback()
        t1.close()
        setup.close()


def test_two_assessments_on_one_application_do_not_block_each_other(db):
    """FOR SHARE, not FOR UPDATE -- and this is what that choice buys.

    The requirement is "this row must not CHANGE while I commit", not "I own this
    row". A shared lock blocks the writers (proved by the three cases above) while
    letting two legitimate assessments on the same application and the same
    assessor proceed together. FOR UPDATE would have serialised them for no
    reason, and nothing in the client's rule asks for that.

    Also the deadlock check: both transactions acquire applications -> decisions
    -> users -> document in the same order, so no cycle is possible between them.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    setup, app_id, staff, doc = _committed_fixture()
    a = _conn()
    b = _conn()
    try:
        with a.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as ca:
            ca.execute("SET search_path TO " + SCHEMA)
            aid = _assess(ca, app_id, staff)
            _attach(ca, aid, doc)

        # B does the same thing while A is still open. If the lock were exclusive
        # this would block and the join below would time out.
        done = {}

        def _second():
            try:
                with b.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cb:
                    cb.execute("SET search_path TO " + SCHEMA)
                    cb.execute("SET LOCAL lock_timeout = '10s'")
                    bid = _assess(cb, app_id, staff, debt="1200.00", dti_bp=2000)
                    _attach(cb, bid, doc)
                b.commit()
                done["state"] = "committed"
            except Exception as exc:                  # noqa: BLE001 - reported
                done["state"] = type(exc).__name__
                b.rollback()

        t = threading.Thread(target=_second, daemon=True)
        t.start()
        t.join(timeout=15.0)
        assert not t.is_alive(), (
            "a second assessment blocked behind the first -- the lock is stronger "
            "than the invariant needs")
        assert done.get("state") == "committed", done

        a.commit()
        with a.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as ca:
            ca.execute("SET search_path TO " + SCHEMA)
            ca.execute("SELECT count(*) AS n FROM manual_dti_assessments "
                       " WHERE app_id = %s", (app_id,))
            assert ca.fetchone()["n"] == 2
    finally:
        a.rollback(); a.close(); b.close(); setup.close()
