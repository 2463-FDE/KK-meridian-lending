"""The servicing portfolio list: what it orders by, and what it filters on.

A loan boards with the highest id. The list ordered by `id` ASC and the page
holds 25, so a freshly boarded loan landed on the LAST page -- rank 192 of 192 in
the case that prompted this -- and the UI's search box filtered only the rows
already fetched, so typing the id on page 1 found nothing. The loan existed, the
balance existed, the detail route worked. The list was the defect.

Two properties fix it and both are the server's job:

  * **newest first by default**, so a loan that was just boarded is the first
    thing an operator sees;
  * **`loan_id` filters the whole portfolio**, not the page in hand.

Ordering is on `id`, not `opened_at`. `id` is the primary key and is assigned
monotonically at boarding, so it is both "most recently boarded" and a TOTAL
order. `opened_at` is neither -- the seeded portfolio holds 10 distinct
timestamps across 184 loans, and a non-unique sort key under LIMIT/OFFSET lets a
row appear on one page and vanish from the next. These tests assert the column
and the direction for that reason, not as a style preference.

The statements are compiled and read rather than run against a database: what is
under test is which SQL the route builds, and a fake session makes the ORDER BY
and the WHERE clauses directly observable without a Postgres round trip. The
behavioural half -- that a newly boarded loan is findable in the browser -- is
pinned in `frontend/e2e/servicing-portfolio-discoverability.spec.ts`.
"""
import datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import get_session


class _Loan:
    def __init__(self, loan_id):
        self.id = loan_id
        self.applicant_name = "Fictional Borrower"
        self.principal = 15000.0
        self.note_rate_pct = 7.99
        self.term_months = 36
        self.status = "current"
        self.opened_at = datetime.datetime(2026, 8, 28, tzinfo=datetime.timezone.utc)


class _Balance:
    def __init__(self, loan_id):
        self.loan_id = loan_id
        self.balance = 15000.0
        self.past_due = 0.0


class _FakeSession:
    """Records every statement the route builds, and answers with canned rows."""

    def __init__(self, loan_ids, total=None):
        self.rows = [(_Loan(i), _Balance(i)) for i in loan_ids]
        self.total = len(loan_ids) if total is None else total
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


def _list(query="", loan_ids=(7298, 7296), total=None):
    session = _FakeSession(list(loan_ids), total=total)

    def _fake_get_session():
        yield session

    main.app.dependency_overrides[get_session] = _fake_get_session
    try:
        response = TestClient(main.app).get("/loans" + (("?" + query) if query else ""))
    finally:
        main.app.dependency_overrides.pop(get_session, None)
    return response, session


def _order_direction(sql: str) -> str:
    tail = sql.upper().split("ORDER BY", 1)[1]
    assert "LOANS.ID" in tail, f"the list must order by loans.id, not: {tail[:80]}"
    return "DESC" if "DESC" in tail.split("LIMIT", 1)[0] else "ASC"


def test_the_default_order_is_newest_first():
    """The whole point: a loan boarded a moment ago is on the first page."""
    response, session = _list()

    assert response.status_code == 200
    assert _order_direction(session.page_sql) == "DESC"


def test_newest_orders_by_descending_id():
    _, session = _list("order=newest")

    assert _order_direction(session.page_sql) == "DESC"


def test_oldest_orders_by_ascending_id():
    _, session = _list("order=oldest")

    assert _order_direction(session.page_sql) == "ASC"


def test_the_list_never_orders_by_opened_at():
    """`opened_at` is not unique -- 10 distinct values across 184 seeded loans --
    so using it under LIMIT/OFFSET would let rows repeat across pages."""
    for query in ("", "order=newest", "order=oldest"):
        _, session = _list(query)
        order_clause = session.page_sql.upper().split("ORDER BY", 1)[1]
        assert "OPENED_AT" not in order_clause


@pytest.mark.parametrize("bad", ["sideways", "asc", "desc", "id", "", "NEWEST"])
def test_an_unrecognised_order_is_refused(bad):
    """A validated enum, never a column or direction taken from the caller."""
    response, _ = _list("order=%s" % bad)

    assert response.status_code == 422


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.5"])
def test_an_invalid_loan_id_is_refused(bad):
    response, _ = _list("loan_id=%s" % bad)

    assert response.status_code == 422


def test_an_exact_loan_id_filters_the_query():
    _, session = _list("loan_id=7298", loan_ids=(7298,))

    assert "loans.id = 7298" in session.page_sql.lower().replace("  ", " ")


def test_the_loan_id_filter_reaches_the_count_as_well_as_the_page():
    """`total` must describe the filtered set. A count that ignored the filter
    would report 184 next to a single row and drive the pager to 8 pages."""
    _, session = _list("loan_id=7298", loan_ids=(7298,), total=1)

    assert "loans.id = 7298" in session.count_sql.lower()
    assert "loans.id = 7298" in session.page_sql.lower()


def test_a_loan_id_that_matches_nothing_is_an_empty_page_not_a_404():
    """This is a list answering "which loans match". None matching is an answer,
    and the UI needs the 200 to say which id it looked for."""
    response, _ = _list("loan_id=999999", loan_ids=(), total=0)

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_status_and_loan_id_compose_on_both_statements():
    _, session = _list("loan_id=7298&status=current", loan_ids=(7298,), total=1)

    for sql in (session.page_sql.lower(), session.count_sql.lower()):
        assert "loans.id = 7298" in sql
        assert "loans.status = 'current'" in sql


def test_the_status_filter_still_reaches_the_count_on_its_own():
    _, session = _list("status=paid_off", loan_ids=(), total=0)

    assert "loans.status = 'paid_off'" in session.count_sql.lower()


def test_status_all_is_not_treated_as_a_status():
    """The UI sends `all` for "no status filter"; it must not become a WHERE."""
    _, session = _list("status=all")

    assert "loans.status" not in session.count_sql.lower()


def test_limit_and_offset_semantics_are_unchanged():
    response, session = _list("limit=5&offset=10", loan_ids=(7298,), total=184)

    body = response.json()
    assert (body["limit"], body["offset"], body["total"]) == (5, 10, 184)
    assert "LIMIT 5" in session.page_sql.upper()
    assert "OFFSET 10" in session.page_sql.upper()


def test_ordering_is_applied_with_limit_and_offset_together():
    """Ordering has to be part of the same statement as the window, or the page
    is a window over an undefined order."""
    _, session = _list("order=oldest&limit=5&offset=25")

    upper = session.page_sql.upper()
    assert upper.index("ORDER BY") < upper.index("LIMIT")
    assert _order_direction(session.page_sql) == "ASC"
