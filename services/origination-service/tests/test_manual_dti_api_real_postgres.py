"""RF-25's API, against a real Postgres.

WHY REAL POSTGRES AND NOT A MOCKED CURSOR. Almost everything this API promises is
enforced by `db/migrations/0047`, not by Python: referred-only, the role held
rather than the role claimed, approved synthetic documents only, at-least-one
document at COMMIT, append-only, and the reproducibility CHECK that ties the
stored ratio to the two stored inputs. A mocked cursor would prove that the route
sends the SQL it was written to send, which is the one thing never in doubt.

WHAT IS ASSERTED HERE, IN THE ORDER IT MATTERS

  1. The authorization matrix -- every role, with and without the internal token,
     on all three routes, and every refusal returning the SAME body so a rejected
     caller learns nothing from which failure it hit.
  2. That nothing the CALLER says about identity, authority or the ratio is
     believed: the body cannot carry them, and a caller claiming a role it does
     not hold is refused by the database rather than by a check here.
  3. That recording evidence changes NO decision surface. This is the client's
     hard constraint, so it is a before/after read of `decisions`,
     `applications.status`, `manual_reviews` and `decision_attempts` rather than
     a statement in a docstring.
  4. That every refusal leaves NOTHING behind -- an assessment with no documents,
     or one citing an unapproved document, must not exist even partially.
  5. That the premise each refusal rests on cannot change underneath a commit.
"""
import ast
import os
import threading
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

REPO = Path(__file__).resolve().parents[3]
INIT = REPO / "db" / "init"
INIT_FILES = ("001_schema.sql", "002_seed.sql", "003_seed_bulk.sql",
              "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql", "007_ledger_opening_balances.sql")

SCHEMA = "manual_dti_api_test"
TOKEN = "test-internal-token"

client = TestClient(app)

#: The seeded staff accounts (`db/init/002_seed.sql`). Their ids are read back
#: rather than assumed, because an id hard-coded here would silently point at a
#: different person the day the seed order changes.
_STAFF = {}


def _connect():
    return psycopg2.connect(DATABASE_URL)


@pytest.fixture(scope="module")
def schema():
    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.commit()
        for name in INIT_FILES:
            path = INIT / name
            if not path.exists():                          # pragma: no cover
                continue
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                cur.execute(path.read_text(encoding="utf-8"))
            conn.commit()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute("SELECT id, username, role FROM users "
                        " WHERE role IN ('underwriter','admin','csr','borrower')")
            for row in cur.fetchall():
                _STAFF.setdefault(row["role"], row["id"])
        conn.commit()
        assert {"underwriter", "admin", "csr"} <= set(_STAFF), _STAFF
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.commit()
        conn.close()


@pytest.fixture()
def real_db(schema, monkeypatch):
    """Point the app's own `db` module at the throwaway schema.

    `db.transaction()` opens a fresh connection per call and `db.query()` reads
    `DATABASE_URL`/`_conn` at call time, so redirecting both here is enough --
    the route code under test is untouched.
    """
    schema_url = DATABASE_URL + ("&" if "?" in DATABASE_URL else "?") \
        + f"options=-csearch_path%3D{SCHEMA}"
    monkeypatch.setattr(db, "DATABASE_URL", schema_url)
    monkeypatch.setattr(db, "_conn", None)
    yield
    monkeypatch.setattr(db, "_conn", None)


@pytest.fixture()
def cur(schema):
    with schema.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        yield c
    schema.commit()


def _an_application(c, outcome="refer"):
    """A committed application in the state the test needs.

    Committed, not held open in the fixture's transaction: the route runs on its
    own connection and would not see an uncommitted row.
    """
    c.execute("INSERT INTO applicants (name) VALUES ('Manual DTI Fixture') RETURNING id")
    applicant_id = c.fetchone()["id"]
    c.execute("INSERT INTO applications (applicant_id, amount, term_months, purpose, status) "
              "VALUES (%s, 10000, 36, 'auto', 'in_review') RETURNING id", (applicant_id,))
    app_id = c.fetchone()["id"]
    if outcome is not None:
        c.execute("INSERT INTO decisions (app_id, outcome) VALUES (%s, %s)",
                  (app_id, outcome))
    c.connection.commit()
    return app_id


#: `None` is a legitimate VALUE to test with (a header explicitly absent), so it
#: cannot also mean "use the default". This sentinel keeps the two apart -- the
#: first version used `None` for both, so the same-body test silently sent a
#: perfectly valid request and then asserted it was refused.
_DEFAULT = object()


def _headers(role="underwriter", token=TOKEN, user_id=_DEFAULT):
    h = {}
    if role is not None:
        h["X-User-Role"] = role
    if token is not None:
        h["X-Internal-Token"] = token
    uid = _STAFF.get(role) if user_id is _DEFAULT else user_id
    if uid is not None:
        h["X-User-Id"] = str(uid)
    return h


def _body(**over):
    payload = {
        "gross_monthly_income": "5000.00",
        "monthly_debt_obligations": "1500.00",
        "document_refs": ["SYN-PAYSTUB-001", "SYN-DEBTSCH-001"],
        "reason": "Documented income and obligations from the synthetic packet.",
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------------------
# 1. The authorization matrix.
# ---------------------------------------------------------------------------

_ALLOWED = ("underwriter", "admin")
_REFUSED_ROLES = ("csr", "borrower", "applicant", "", None)


@pytest.mark.parametrize("role", _ALLOWED)
def test_an_underwriter_or_admin_may_record_evidence(real_db, cur, role):
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                    headers=_headers(role))
    assert r.status_code == 201, r.text
    assert r.json()["assessed_role"] == role


@pytest.mark.parametrize("role", _REFUSED_ROLES)
def test_every_other_role_is_refused(real_db, cur, role):
    """A CSR is staff for the rest of this service and is deliberately not here.

    The client authorised manual DTI for underwriters and admins. `_STAFF_ROLES`
    -- the set the older routes use -- includes CSR, so reusing it would have
    widened the client's rule by reusing a convenient constant.
    """
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                    headers=_headers(role, user_id=_STAFF["underwriter"]))
    assert r.status_code == 403, r.text


def test_the_internal_token_is_required_even_with_a_staff_role(real_db, cur):
    """The role header alone proves nothing.

    A caller reaching this service directly inside the compose network can set
    `X-User-Role: admin` itself. The gateway attaches the shared secret; a direct
    caller does not know it.
    """
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                    headers=_headers("underwriter", token=None))
    assert r.status_code == 403


def test_a_wrong_token_is_refused(real_db, cur):
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                    headers=_headers("admin", token="not-the-secret"))
    assert r.status_code == 403


def test_every_refusal_returns_the_same_body(real_db, cur):
    """No oracle. Wrong role, wrong token, no token and no id all read alike.

    A message that distinguished them would tell an unauthenticated caller which
    half of the check to work on next.
    """
    app_id = _an_application(cur)
    bodies = set()
    for headers in (_headers("csr"),
                    _headers("underwriter", token=None),
                    _headers("admin", token="wrong"),
                    _headers("underwriter", user_id=None),
                    {}):
        r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                        headers=headers)
        assert r.status_code == 403, (headers, r.text)
        bodies.add(r.text)
    assert len(bodies) == 1, bodies


@pytest.mark.parametrize("role", _REFUSED_ROLES)
def test_the_read_routes_are_gated_the_same_way(real_db, cur, role):
    app_id = _an_application(cur)
    assert client.get(f"/applications/{app_id}/manual-dti",
                      headers=_headers(role)).status_code == 403
    assert client.get("/manual-dti/source-documents",
                      headers=_headers(role)).status_code == 403


def test_a_missing_user_id_is_refused_rather_than_defaulted(real_db, cur):
    """There is no fallback identity. Evidence signed by nobody is not evidence."""
    app_id = _an_application(cur)
    headers = _headers("underwriter")
    headers.pop("X-User-Id")
    assert client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                       headers=headers).status_code == 403


@pytest.mark.parametrize("bad", ["", "abc", "0", "-4", "1.5", "1 OR 1=1"])
def test_a_malformed_user_id_is_refused(real_db, cur, bad):
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                    headers=_headers("underwriter", user_id=bad))
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 2. Nothing about identity, authority or the ratio is taken from the caller.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("assessed_by", 1),
    ("assessed_role", "admin"),
    ("dti_bp", 1),
    ("dti", 30.0),
    ("dti_pct", "30%"),
    ("assessed_at", "2020-01-01T00:00:00Z"),
])
def test_the_body_cannot_carry_identity_authority_or_the_ratio(real_db, cur, field, value):
    """422, not a silent drop.

    Pydantic's default is to ignore unknown fields. A caller that sent
    `assessed_by` and got a 201 would reasonably believe the assessment was
    attributed to that person -- one request, two beliefs about what was
    recorded. `extra="forbid"` makes the disagreement impossible.
    """
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti",
                    json=_body(**{field: value}), headers=_headers("underwriter"))
    assert r.status_code == 422, r.text
    assert field in r.text


def test_the_stored_identity_is_the_sessions_not_the_bodys(real_db, cur):
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                    headers=_headers("admin"))
    assert r.status_code == 201, r.text
    assert r.json()["assessed_by"] == _STAFF["admin"]
    cur.execute("SELECT assessed_by, assessed_role FROM manual_dti_assessments "
                " WHERE app_id = %s", (app_id,))
    row = cur.fetchone()
    assert row["assessed_by"] == _STAFF["admin"]
    assert row["assessed_role"] == "admin"


def test_claiming_a_role_you_do_not_hold_is_refused_by_the_database(real_db, cur):
    """The CSR's user id with an underwriter role header.

    The route's own gate passes -- the header says underwriter -- so this is
    exactly the shape a route bug would take. `manual_dti_is_permitted` compares
    the claim against `users.role` and refuses, which is what makes the stored
    role evidence rather than an assertion.
    """
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                    headers=_headers("underwriter", user_id=_STAFF["csr"]))
    assert r.status_code == 409, r.text
    assert "holds role csr" in r.json()["detail"]
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessments WHERE app_id = %s",
                (app_id,))
    assert cur.fetchone()["n"] == 0


def test_a_deactivated_account_cannot_sign_evidence(real_db, cur):
    app_id = _an_application(cur)
    cur.execute("UPDATE users SET is_active = FALSE WHERE id = %s",
                (_STAFF["underwriter"],))
    cur.connection.commit()
    try:
        r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                        headers=_headers("underwriter"))
        assert r.status_code == 409, r.text
        assert "not active" in r.json()["detail"]
    finally:
        cur.execute("UPDATE users SET is_active = TRUE WHERE id = %s",
                    (_STAFF["underwriter"],))
        cur.connection.commit()


# ---------------------------------------------------------------------------
# 3. The ratio is computed by the database, from the evidence.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("income,debt,expected_bp", [
    ("5000.00", "1500.00", 3000),      # 30.00%
    ("5000.00", "0.00", 0),            # no obligations at all
    ("3000.00", "3000.00", 10000),     # 100%
    ("3000.00", "4500.00", 15000),     # obligations may exceed income
    ("3.00", "1.00", 3333),            # 3333.33... truncates DOWN by rounding
    ("200.00", "0.01", 1),             # exactly .5 -- round() is half-UP
    ("99999999.99", "0.01", 0),        # rounds to zero rather than failing
])
def test_the_ratio_is_derived_from_the_two_inputs(real_db, cur, income, debt, expected_bp):
    """Never supplied, never computed in Python.

    The INSERT evaluates `round(obligations * 10000 / income)` in Postgres, which
    is the same expression `manual_dti_is_reproducible` checks the row against.
    One definition: a Python copy could drift from the constraint, and the drift
    would arrive as an opaque CHECK violation instead of a visible wrong number.
    """
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti",
                    json=_body(gross_monthly_income=income,
                               monthly_debt_obligations=debt),
                    headers=_headers("underwriter"))
    assert r.status_code == 201, r.text
    assert r.json()["dti_bp"] == expected_bp
    cur.execute("SELECT dti_bp, gross_monthly_income, monthly_debt_obligations "
                "  FROM manual_dti_assessments WHERE app_id = %s", (app_id,))
    row = cur.fetchone()
    assert row["dti_bp"] == expected_bp
    # Recomputed from the stored row, which is the whole point of storing both
    # inputs: a reader can check the figure without trusting whoever wrote it.
    #
    # ROUND_HALF_UP explicitly, because Python's built-in `round` is half-to-EVEN
    # while Postgres' `round(numeric)` is half-away-from-zero. The 0.5 case here
    # (0.01 against 200.00) disagrees by one basis point between the two -- which
    # is exactly why the route keeps no Python copy of this formula and lets the
    # database evaluate the one the CHECK constraint verifies the row against.
    recomputed = (row["monthly_debt_obligations"] * 10000
                  / row["gross_monthly_income"]).quantize(
                      Decimal("1"), rounding=ROUND_HALF_UP)
    assert row["dti_bp"] == int(recomputed)


@pytest.mark.parametrize("income,debt", [
    ("0.00", "100.00"),        # income must be positive -- division by zero
    ("-1.00", "100.00"),
    ("5000.00", "-0.01"),      # obligations may be zero, never negative
])
def test_impossible_inputs_are_refused(real_db, cur, income, debt):
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti",
                    json=_body(gross_monthly_income=income,
                               monthly_debt_obligations=debt),
                    headers=_headers("underwriter"))
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("reason", ["", "   ", "\t", "\n"])
def test_a_blank_reason_is_refused(real_db, cur, reason):
    """Whitespace included. `btrim` with no argument strips SPACES ONLY, which is
    how a tab-only reason passed the first version of the schema's CHECK."""
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(reason=reason),
                    headers=_headers("underwriter"))
    assert r.status_code in (409, 422), r.text
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessments WHERE app_id = %s",
                (app_id,))
    assert cur.fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# 4. Documents: approved, synthetic, cited by reference, and never partial.
# ---------------------------------------------------------------------------

def test_the_registry_lists_only_approved_synthetic_documents(real_db):
    r = client.get("/manual-dti/source-documents", headers=_headers("underwriter"))
    assert r.status_code == 200, r.text
    refs = [d["doc_ref"] for d in r.json()]
    assert refs, "the registry came back empty"
    assert "SYN-DRAFT-001" not in refs, (
        "the deliberately unapproved row is being offered to staff; it exists so "
        "the refusal path has something real to refuse, not to be selectable")
    assert refs == sorted(refs)


def test_an_unapproved_document_cannot_be_cited(real_db, cur):
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti",
                    json=_body(document_refs=["SYN-DRAFT-001"]),
                    headers=_headers("underwriter"))
    assert r.status_code == 409, r.text
    assert "not approved" in r.json()["detail"]
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessments WHERE app_id = %s",
                (app_id,))
    assert cur.fetchone()["n"] == 0, (
        "the assessment row survived a refused document citation -- an assessment "
        "with no evidence behind it is exactly what RF-25 refuses")


def test_one_bad_reference_rolls_the_whole_thing_back(real_db, cur):
    """A good document and an unapproved one, in one request.

    Partial evidence is worse than none: an assessment that quietly kept only the
    documents that happened to be approved would rest on something other than
    what its author cited.
    """
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti",
                    json=_body(document_refs=["SYN-PAYSTUB-001", "SYN-DRAFT-001"]),
                    headers=_headers("underwriter"))
    assert r.status_code == 409, r.text
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessments WHERE app_id = %s",
                (app_id,))
    assert cur.fetchone()["n"] == 0
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessment_documents")
    before = cur.fetchone()["n"]
    assert before >= 0


def test_an_unknown_reference_is_named_and_nothing_is_written(real_db, cur):
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti",
                    json=_body(document_refs=["SYN-NOPE-999"]),
                    headers=_headers("underwriter"))
    assert r.status_code == 422, r.text
    assert "SYN-NOPE-999" in r.json()["detail"]
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessments WHERE app_id = %s",
                (app_id,))
    assert cur.fetchone()["n"] == 0


@pytest.mark.parametrize("refs,expected", [
    ([], 422),                                             # a bare ratio
    (["SYN-PAYSTUB-001", "SYN-PAYSTUB-001"], 422),         # cited twice
    ([" "], 422),                                          # blank reference
])
def test_the_document_list_itself_is_validated(real_db, cur, refs, expected):
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti",
                    json=_body(document_refs=refs), headers=_headers("underwriter"))
    assert r.status_code == expected, r.text


def test_the_route_accepts_no_document_content_at_all(real_db):
    """RF-25's scope boundary, asserted against the module rather than described.

    No upload, no OCR, no extraction, no embedding, no external storage. A source
    document is a reference to an approved synthetic fixture and nothing else, so
    a file parameter appearing here would be a scope change rather than a
    feature.
    """
    source = (Path(__file__).resolve().parents[1] / "app" / "routers"
              / "manual_dti.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Read as CODE, not as text. The first version of this guard was a substring
    # scan and it flagged the module's own docstring -- the sentence saying there
    # is no OCR here contains the word "OCR". A guard that fires on the prose
    # forbidding a thing, rather than on the thing, is not a guard.
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    outside = imported & {"boto3", "requests", "httpx", "aiohttp", "urllib",
                          "shutil", "tempfile", "pytesseract", "PIL", "openai"}
    assert not outside, (
        "the manual DTI router imports something outside its scope: %s" % outside)

    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"open", "File", "UploadFile"}, called

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.annotation.id for a in ast.walk(tree)
              if isinstance(a, ast.arg) and isinstance(a.annotation, ast.Name)}
    assert not names & {"UploadFile", "File"}, (
        "a file parameter appeared on a manual DTI route; a source document is a "
        "reference to an approved synthetic fixture, and accepting content would "
        "be a scope change rather than a feature")


# ---------------------------------------------------------------------------
# 5. Referred applications only.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", ["approve", "deny"])
def test_a_decided_application_is_not_eligible(real_db, cur, outcome):
    app_id = _an_application(cur, outcome=outcome)
    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                    headers=_headers("underwriter"))
    assert r.status_code == 409, r.text
    assert "not referred" in r.json()["detail"]


def test_an_application_with_no_decision_is_not_eligible(real_db, cur):
    app_id = _an_application(cur, outcome=None)
    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                    headers=_headers("underwriter"))
    assert r.status_code == 409, r.text
    assert "no decision on record" in r.json()["detail"]


def test_an_application_that_does_not_exist_is_refused_without_confirming_it(real_db):
    r = client.post("/applications/99999999/manual-dti", json=_body(),
                    headers=_headers("underwriter"))
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# 6. The client's hard constraint: evidence decides nothing.
# ---------------------------------------------------------------------------

_DECISION_SURFACE = (
    ("decisions", "SELECT app_id, outcome FROM decisions ORDER BY app_id"),
    ("applications", "SELECT id, status FROM applications ORDER BY id"),
    ("manual_reviews", "SELECT app_id, outcome, reason FROM manual_reviews ORDER BY app_id"),
    ("decision_attempts", "SELECT id, app_id, state FROM decision_attempts ORDER BY id"),
)


def _snapshot(c):
    out = {}
    for name, sql in _DECISION_SURFACE:
        c.execute(sql)
        out[name] = [dict(r) for r in c.fetchall()]
    return out


def test_recording_evidence_changes_no_decision_surface(real_db, cur):
    """Read before and after, every table that could carry a decision.

    RF-25's constraint is that a manual DTI must not approve, deny, override,
    mutate a decision or trigger model output. That is not something a docstring
    can promise: this reads `decisions`, `applications.status`, `manual_reviews`
    and `decision_attempts` on both sides of a successful POST and compares them.
    """
    app_id = _an_application(cur)
    cur.connection.commit()
    before = _snapshot(cur)
    cur.connection.commit()

    r = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                    headers=_headers("underwriter"))
    assert r.status_code == 201, r.text

    cur.connection.commit()
    after = _snapshot(cur)
    for name, _ in _DECISION_SURFACE:
        assert before[name] == after[name], (
            f"recording manual DTI evidence changed {name}; RF-25 requires that "
            "it approve, deny, override and mutate nothing")


def test_the_evidence_itself_is_what_changed(real_db, cur):
    """The negative control for the test above -- it must not pass by writing
    nothing at all."""
    app_id = _an_application(cur)
    cur.connection.commit()
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessments")
    before = cur.fetchone()["n"]
    cur.connection.commit()
    assert client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                       headers=_headers("underwriter")).status_code == 201
    cur.connection.commit()
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessments")
    assert cur.fetchone()["n"] == before + 1


# ---------------------------------------------------------------------------
# 7. Append-only, and read back in full.
# ---------------------------------------------------------------------------

def test_a_second_assessment_is_an_additional_row_not_an_edit(real_db, cur):
    """Not idempotent, and not pretending to be.

    A referred application may be assessed twice -- a second reviewer, or the
    same one after new documents -- so a repeat is a new row. Collapsing them
    would need an idempotency rule the client has not given.
    """
    app_id = _an_application(cur)
    first = client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                        headers=_headers("underwriter"))
    second = client.post(f"/applications/{app_id}/manual-dti",
                         json=_body(monthly_debt_obligations="1600.00",
                                    reason="Second reviewer, additional statement."),
                         headers=_headers("admin"))
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]

    listed = client.get(f"/applications/{app_id}/manual-dti",
                        headers=_headers("underwriter"))
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 2
    assert [r["id"] for r in rows] == sorted([r["id"] for r in rows], reverse=True), (
        "the register reads newest first, so the earlier evidence a later "
        "assessment may correct is still visible below it")
    assert {r["assessed_role"] for r in rows} == {"underwriter", "admin"}


def test_the_listing_carries_the_documents_each_assessment_rests_on(real_db, cur):
    app_id = _an_application(cur)
    assert client.post(f"/applications/{app_id}/manual-dti",
                       json=_body(document_refs=["SYN-BANK-001", "SYN-TAX-001"]),
                       headers=_headers("underwriter")).status_code == 201
    rows = client.get(f"/applications/{app_id}/manual-dti",
                      headers=_headers("admin")).json()
    assert [d["doc_ref"] for d in rows[0]["documents"]] == ["SYN-BANK-001", "SYN-TAX-001"]
    assert rows[0]["documents"][0]["kind"] == "bank_statement"


def test_an_application_with_no_evidence_reads_as_an_empty_list(real_db, cur):
    app_id = _an_application(cur)
    r = client.get(f"/applications/{app_id}/manual-dti", headers=_headers("underwriter"))
    assert r.status_code == 200 and r.json() == []


def test_recorded_evidence_cannot_be_edited_or_deleted(real_db, cur):
    """There is no route that would, and the database refuses it regardless.

    Evidence that can be edited is not evidence. Asserted at the table rather
    than by noting the absence of a PUT, because "no route exists" is a fact
    about today's code and the immutability is a fact about the record.
    """
    app_id = _an_application(cur)
    assert client.post(f"/applications/{app_id}/manual-dti", json=_body(),
                       headers=_headers("underwriter")).status_code == 201
    cur.connection.commit()
    cur.execute("SELECT id FROM manual_dti_assessments WHERE app_id = %s", (app_id,))
    assessment_id = cur.fetchone()["id"]
    for sql in ("UPDATE manual_dti_assessments SET reason = 'edited' WHERE id = %s",
                "DELETE FROM manual_dti_assessments WHERE id = %s"):
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute(sql, (assessment_id,))
        cur.execute("ROLLBACK TO SAVEPOINT s")

    routes = [r.path for r in app.routes if hasattr(r, "methods")
              and {"PUT", "PATCH", "DELETE"} & set(r.methods)]
    assert not [p for p in routes if "manual-dti" in p], routes


# ---------------------------------------------------------------------------
# 8. The premise cannot change underneath a commit.
# ---------------------------------------------------------------------------

def test_the_referral_cannot_be_decided_while_an_assessment_is_in_flight(schema, cur):
    """Two real connections, genuinely overlapping.

    T1 opens the same transaction the route opens -- insert the assessment,
    do not commit. T2 tries to approve the application. `manual_dti_is_permitted`
    holds `FOR SHARE` on the decisions row, so T2 must BLOCK rather than slip its
    approval in before the evidence lands. Asserted by the worker still being
    alive, not by a sleep.

    Without the lock, T2's approve could commit first and the assessment would
    land against an application that was no longer referred -- evidence recorded
    under a premise that had already stopped being true.
    """
    app_id = _an_application(cur)
    t1 = _connect()
    outcome = {}

    def _approve():
        c2 = _connect()
        try:
            with c2.cursor() as c:
                c.execute(f"SET search_path TO {SCHEMA}")
                c.execute("SET LOCAL lock_timeout = '30s'")
                c.execute("UPDATE decisions SET outcome = 'approve' WHERE app_id = %s",
                          (app_id,))
            c2.commit()
            outcome["state"] = "committed"
        except Exception as exc:                          # noqa: BLE001 - reported
            outcome["state"] = type(exc).__name__
            c2.rollback()
        finally:
            c2.close()

    try:
        with t1.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c1:
            c1.execute(f"SET search_path TO {SCHEMA}")
            c1.execute(
                "INSERT INTO manual_dti_assessments "
                "  (app_id, assessed_by, assessed_role, gross_monthly_income, "
                "   monthly_debt_obligations, dti_bp, reason) "
                "VALUES (%s, %s, 'underwriter', 5000.00, 1500.00, 3000, 'in flight') "
                "RETURNING id", (app_id, _STAFF["underwriter"]))
            assessment_id = c1.fetchone()["id"]
            c1.execute("INSERT INTO manual_dti_assessment_documents "
                       "  (assessment_id, document_id) "
                       "SELECT %s, id FROM manual_dti_source_documents "
                       " WHERE doc_ref = 'SYN-PAYSTUB-001'", (assessment_id,))

        worker = threading.Thread(target=_approve, daemon=True)
        worker.start()
        worker.join(timeout=3.0)
        assert worker.is_alive(), (
            "the application was approved while the assessment was still in "
            "flight -- manual_dti_is_permitted is not holding the decisions row, "
            "so evidence can be recorded against a premise that has already "
            "stopped being true")

        t1.commit()
        worker.join(timeout=30.0)
        assert outcome.get("state") == "committed", outcome
    finally:
        t1.rollback()
        t1.close()
        with cur.connection.cursor() as c:
            c.execute(f"SET search_path TO {SCHEMA}")
            c.execute("UPDATE decisions SET outcome = 'refer' WHERE app_id = %s",
                      (app_id,))
        cur.connection.commit()


def test_two_assessments_on_one_application_do_not_block_each_other(real_db, cur):
    """FOR SHARE, not FOR UPDATE, and this is the difference it buys.

    Two reviewers assessing the same referred application concurrently is a
    legitimate thing to do. An exclusive lock would serialise them for no reason;
    a shared one still blocks the decision change the previous test proves.
    """
    app_id = _an_application(cur)
    results = []

    def _record(role):
        results.append(client.post(f"/applications/{app_id}/manual-dti",
                                   json=_body(reason=f"concurrent {role}"),
                                   headers=_headers(role)).status_code)

    threads = [threading.Thread(target=_record, args=(r,))
               for r in ("underwriter", "admin")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)
    assert results == [201, 201], results
    cur.connection.commit()
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessments WHERE app_id = %s",
                (app_id,))
    assert cur.fetchone()["n"] == 2


def test_a_cited_document_cannot_be_changed_afterwards(real_db, cur):
    """The registry row an assessment rests on is frozen once cited."""
    app_id = _an_application(cur)
    assert client.post(f"/applications/{app_id}/manual-dti",
                       json=_body(document_refs=["SYN-PAYSTUB-002"]),
                       headers=_headers("underwriter")).status_code == 201
    cur.connection.commit()
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        cur.execute("UPDATE manual_dti_source_documents SET approved = FALSE "
                    " WHERE doc_ref = 'SYN-PAYSTUB-002'")
    cur.execute("ROLLBACK TO SAVEPOINT s")
    assert "cited by" in str(exc.value)


# ---------------------------------------------------------------------------
# 9. The overflow edge. Codex review BDTI-API-01.
#
# Every request that satisfies this schema must either record cleanly or be
# refused with a 422. It could do neither: `0.01` income against
# `999999999999.99` obligations produces roughly 1e17 basis points, past
# `dti_bp INTEGER`, and Postgres' `integer out of range` reached the client as a
# 500. The boundary matrix above had tested the direction that rounds to ZERO
# and not the one that overflows.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("income,debt", [
    ("0.01", "999999999999.99"),      # the reported case, ~1e17 bp
    ("1.00", "214748.37"),            # one hundredth over INT_MAX basis points
    ("0.01", "21474.84"),             # same edge reached from a tiny income
])
def test_a_ratio_too_large_to_store_is_refused_not_a_500(real_db, cur, income, debt):
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti",
                    json=_body(gross_monthly_income=income,
                               monthly_debt_obligations=debt),
                    headers=_headers("underwriter"))
    assert r.status_code == 422, r.text
    assert "too large to record" in r.json()["detail"]
    cur.connection.commit()
    cur.execute("SELECT count(*) AS n FROM manual_dti_assessments WHERE app_id = %s",
                (app_id,))
    assert cur.fetchone()["n"] == 0


def test_the_largest_storable_ratio_is_still_accepted(real_db, cur):
    """The other side of the edge, so the refusal is not simply "large is bad".

    `214748.36` against `1.00` is 2,147,483,600 basis points -- the largest
    multiple of 100 that fits `INTEGER`. It is an absurd debt-to-income ratio and
    it records, because nothing here has authority to decide which ratios are
    plausible; the only limit enforced is the one the column actually has.
    """
    app_id = _an_application(cur)
    r = client.post(f"/applications/{app_id}/manual-dti",
                    json=_body(gross_monthly_income="1.00",
                               monthly_debt_obligations="214748.36"),
                    headers=_headers("underwriter"))
    assert r.status_code == 201, r.text
    assert r.json()["dti_bp"] == 2147483600
