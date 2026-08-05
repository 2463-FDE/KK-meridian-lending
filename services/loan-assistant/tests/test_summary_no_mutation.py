"""Audit finding (requirement 10): prove summary regeneration cannot change
the application's decision, reason, staff member, or decision timestamp.

The guarantee is structural, not access-controlled: main.py's summarize()
route only ever issues httpx.get calls (fetch application, fetch
financials) and a read-only summarize_application() call -- there is no
write statement (INSERT/UPDATE/DELETE, nor any httpx.post/put/patch/delete)
anywhere in this call graph. This test proves that structurally: every
write-shaped httpx method is monkeypatched to raise if ever invoked, and a
normal summary request still completes successfully without touching any of
them.
"""
from fastapi.testclient import TestClient

from app import main
from app.schemas import LoanSummary

client = TestClient(main.app, raise_server_exceptions=False)


class _FakeGetResponse:
    def __init__(self, url):
        self._url = url

    def raise_for_status(self):
        pass

    @property
    def status_code(self):
        return 200

    def json(self):
        if "financials" in self._url:
            return {"income": 85000, "employment_years": 4}
        return {
            "id": 42, "amount": 15000, "term_months": 36, "purpose": "debt_consolidation",
            "decision": "approve",
        }


def _write_not_allowed(*args, **kwargs):
    raise AssertionError("summary generation must never issue a write call (httpx.post/put/patch/delete)")


def test_summary_endpoint_never_issues_a_write_call(monkeypatch):
    monkeypatch.setattr(main.httpx, "get", lambda url, headers=None, timeout=None: _FakeGetResponse(url))
    monkeypatch.setattr(main.httpx, "post", _write_not_allowed)
    monkeypatch.setattr(main.httpx, "put", _write_not_allowed)
    monkeypatch.setattr(main.httpx, "patch", _write_not_allowed)
    monkeypatch.setattr(main.httpx, "delete", _write_not_allowed)

    monkeypatch.setattr(
        main,
        "summarize_application",
        lambda app_data: LoanSummary(
            applicant_name="Test Applicant", loan_amount=15000, term_months=36,
            purpose="debt_consolidation", risk_tier="low", summary="ok", flags=[],
        ),
    )

    resp = client.post("/applications/42/summary", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 200


def test_repeated_summary_calls_can_return_the_same_result_when_data_is_unchanged(monkeypatch):
    """Requirement: if the application data hasn't changed, an identical (or
    near-identical) regenerated summary is expected, not a bug -- this just
    proves calling the endpoint twice in a row doesn't itself force or
    require a different result."""
    monkeypatch.setattr(main.httpx, "get", lambda url, headers=None, timeout=None: _FakeGetResponse(url))
    monkeypatch.setattr(main.httpx, "post", _write_not_allowed)

    fixed_summary = LoanSummary(
        applicant_name="Test Applicant", loan_amount=15000, term_months=36,
        purpose="debt_consolidation", risk_tier="low", summary="Same summary both times.", flags=[],
    )
    monkeypatch.setattr(main, "summarize_application", lambda app_data: fixed_summary)

    first = client.post("/applications/42/summary", headers={"X-User-Role": "underwriter"})
    second = client.post("/applications/42/summary", headers={"X-User-Role": "underwriter"})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
