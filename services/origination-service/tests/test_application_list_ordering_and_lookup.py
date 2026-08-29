"""The underwriting pipeline list: what it orders by, and what it filters on.

An application is submitted with the highest id, and the console page holds 25.
The search box filtered the rows ALREADY FETCHED -- `page.tsx` said so in a
comment -- so typing the id of an application outside the current page found
nothing. The application existed, the decision existed, the detail route worked.
The list was the defect.

This is the same defect the servicing portfolio carried until #120, on the screen
an underwriter starts their day on, and it is fixed the same way. Both properties
are the server's job:

  * **newest first by default**, so a just-submitted application is the first
    thing on the screen;
  * **`app_id` filters the whole pipeline**, not the page in hand.

Ordering is on `id`, not `created_at`. `id` is the primary key and is assigned in
submission order, so it is both "most recently submitted" and a TOTAL order.
`created_at` is neither -- seeded applications share timestamps, and a non-unique
sort key under LIMIT/OFFSET lets a row appear on one page and vanish from the
next. MC-PAGE-ORDER-01 was that same trap found in a third place, so it is
asserted here as a property rather than left to a style preference.

The statements are compiled and read rather than run against a database: what is
under test is which SQL the route builds, and a fake session makes the ORDER BY
and the WHERE clauses directly observable. The behavioural half -- that an
application outside page one is findable in the browser -- is pinned in
`frontend/e2e/underwriting-discoverability.spec.ts`.
"""
import datetime
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import get_session

#: Role AND the shared internal token: `_is_staff` requires both, because a
#: direct caller can claim any role it likes but cannot know the token.
STAFF = {
    "X-User-Role": "underwriter",
    "X-Internal-Token": os.environ["INTERNAL_SERVICE_TOKEN"],
}


class _Application:
    def __init__(self, app_id):
        self.id = app_id
        self.amount = 15000.0
        self.term_months = 36
        self.purpose = "debt_consolidation"
        self.status = "submitted"
        self.created_at = datetime.datetime(2026, 8, 28, tzinfo=datetime.timezone.utc)


class _FakeSession:
    """Records every statement the route builds, and answers with canned rows."""

    def __init__(self, app_ids, total=None):
        self.rows = [(_Application(i), "Fictional Applicant") for i in app_ids]
        self.total = len(app_ids) if total is None else total
        self.count_sql = None
        self.page_sql = None

    def scalar(self, statement):
        self.count_sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        return self.total

    def execute(self, statement):
        self.page_sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        return self

    def all(self):
        return list(self.rows)


def _list(query="", app_ids=(412, 410), total=None, headers=STAFF):
    session = _FakeSession(list(app_ids), total=total)

    def _fake_get_session():
        yield session

    app.dependency_overrides[get_session] = _fake_get_session
    try:
        response = TestClient(app).get(
            "/applications" + (("?" + query) if query else ""), headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_session, None)
    return response, session


def _order_direction(sql: str) -> str:
    tail = sql.upper().split("ORDER BY", 1)[1]
    assert "APPLICATIONS.ID" in tail, (
        f"the list must order by applications.id, not: {tail[:80]}"
    )
    return "DESC" if "DESC" in tail.split("LIMIT", 1)[0] else "ASC"


def test_the_default_order_is_newest_first():
    """The whole point: an application submitted a moment ago is on page one."""
    response, session = _list()

    assert response.status_code == 200
    assert _order_direction(session.page_sql) == "DESC"


def test_newest_orders_by_descending_id():
    _, session = _list("order=newest")

    assert _order_direction(session.page_sql) == "DESC"


def test_oldest_orders_by_ascending_id():
    _, session = _list("order=oldest")

    assert _order_direction(session.page_sql) == "ASC"


def test_an_unknown_order_is_refused_rather_than_guessed():
    """`order` is a Literal. A column or direction must never arrive as a
    caller-supplied string."""
    response, _ = _list("order=created_at")

    assert response.status_code == 422


def test_app_id_filters_the_page_query():
    _, session = _list("app_id=307", app_ids=(307,))

    assert "applications.id = 307" in session.page_sql.lower().replace('"', "")


def test_app_id_filters_the_COUNT_query_too():
    """The half that is easy to forget and reads as a bug in the data.

    A count that ignored the filter would report the whole pipeline beside a
    single row -- "1 of 190" -- and an underwriter would reasonably conclude the
    search was broken rather than that the total was.
    """
    _, session = _list("app_id=307", app_ids=(307,), total=1)

    assert "applications.id = 307" in session.count_sql.lower().replace('"', "")


def test_status_filters_both_queries():
    _, session = _list("status=submitted")

    for sql in (session.page_sql, session.count_sql):
        assert "applications.status" in sql.lower().replace('"', "")


def test_status_and_app_id_compose():
    """Both filters at once, on both statements. Either one silently dropped
    would return rows the caller did not ask for."""
    _, session = _list("status=submitted&app_id=307", app_ids=(307,))

    for sql in (session.page_sql, session.count_sql):
        flat = sql.lower().replace('"', "")
        assert "applications.status" in flat
        assert "applications.id = 307" in flat


def test_a_filter_and_an_order_compose():
    """A lookup that quietly lost the ordering would page unstably."""
    _, session = _list("app_id=307&order=oldest", app_ids=(307,))

    assert "applications.id = 307" in session.page_sql.lower().replace('"', "")
    assert _order_direction(session.page_sql) == "ASC"


def test_an_id_that_matches_nothing_is_an_empty_page_not_a_404():
    """A list endpoint answers "which applications match", and none matching is
    an answer. A 404 would also make this route an existence oracle for anybody
    past the staff gate."""
    response, _ = _list("app_id=999999", app_ids=(), total=0)

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_a_zero_or_negative_id_is_refused_before_it_reaches_sql():
    """`ge=1`. Ids start at 1, so anything below it is a malformed request
    rather than a lookup that happens to match nothing."""
    for bad in ("0", "-3"):
        response, _ = _list(f"app_id={bad}")
        assert response.status_code == 422, bad


def test_the_total_describes_the_filtered_set():
    _, session = _list("status=submitted", app_ids=(412, 410), total=2)

    assert "applications.status" in session.count_sql.lower().replace('"', "")


def test_a_borrower_cannot_read_the_pipeline_at_all():
    """The ROLE half of the gate, with the token present so that is what fails.

    The gate runs before any query, so there is no existence oracle: a refused
    caller learns nothing about which ids exist.
    """
    response, session = _list(
        "app_id=307",
        headers=dict(STAFF, **{"X-User-Role": "borrower"}),
    )

    assert response.status_code == 403
    assert session.page_sql is None, "a refused request still built a query"
    assert session.count_sql is None


def test_a_claimed_staff_role_without_the_internal_token_is_refused():
    """The TOKEN half, asserted separately.

    A direct caller inside the compose network can set any role header it
    likes; what it cannot do is know the shared token. Testing only the borrower
    case would leave that half unguarded here.
    """
    response, session = _list("app_id=307", headers={"X-User-Role": "underwriter"})

    assert response.status_code == 403
    assert session.page_sql is None


def test_an_unauthenticated_caller_cannot_read_the_pipeline():
    response, session = _list(headers={})

    assert response.status_code == 403
    assert session.page_sql is None
