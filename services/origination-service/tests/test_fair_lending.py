"""Tests for the ZIP-level disparate-impact check (fair_lending.py).

Week 8 finding: "Can't be checked at all -- confirmed no ZIP field exists
anywhere in the schema." These tests cover the check the new zip_code field
unblocks -- grouping by ZIP3 and applying the four-fifths rule.
"""
from app import db, fair_lending


def _row(zip_code, outcome):
    return {"zip_code": zip_code, "outcome": outcome}


def test_no_zip_data_returns_empty_report(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [])

    report = fair_lending.zip_disparate_impact_report()

    assert report["groups"] == []
    assert report["max_rate"] is None
    assert report["flagged"] == []


def test_equal_approval_rates_flag_nothing(monkeypatch):
    rows = (
        [_row("20912", "approve")] * 8 + [_row("20912", "deny")] * 2
        + [_row("30301", "approve")] * 8 + [_row("30301", "deny")] * 2
    )
    monkeypatch.setattr(db, "query", lambda sql, params=None: rows)

    report = fair_lending.zip_disparate_impact_report()

    assert report["flagged"] == []
    rates = {g["zip3"]: g["approval_rate"] for g in report["groups"]}
    assert rates == {"209": 0.8, "303": 0.8}


def test_zip_below_four_fifths_of_max_is_flagged(monkeypatch):
    # 209: 90% approval (9/10). 303: 50% approval (5/10) -- 50% < 80% of 90% (72%).
    rows = (
        [_row("20912", "approve")] * 9 + [_row("20912", "deny")] * 1
        + [_row("30301", "approve")] * 5 + [_row("30301", "deny")] * 5
    )
    monkeypatch.setattr(db, "query", lambda sql, params=None: rows)

    report = fair_lending.zip_disparate_impact_report()

    assert report["flagged"] == ["303"]
    assert report["max_rate"] == 0.9


def test_small_group_not_flagged_even_if_low_rate(monkeypatch):
    # 303 has only 2 decisions -- below min_group_size=5, excluded from flagging
    # regardless of its rate, but still reported for visibility.
    rows = (
        [_row("20912", "approve")] * 9 + [_row("20912", "deny")] * 1
        + [_row("30301", "deny")] * 2
    )
    monkeypatch.setattr(db, "query", lambda sql, params=None: rows)

    report = fair_lending.zip_disparate_impact_report(min_group_size=5)

    small_group = next(g for g in report["groups"] if g["zip3"] == "303")
    assert small_group["eligible_for_flagging"] is False
    assert small_group["flagged"] is False
    assert report["flagged"] == []


def test_zip3_groups_full_five_digit_zips_together(monkeypatch):
    rows = [_row("20912", "approve"), _row("20913", "deny"), _row("20999", "approve")]
    monkeypatch.setattr(db, "query", lambda sql, params=None: rows)

    report = fair_lending.zip_disparate_impact_report(min_group_size=1)

    assert len(report["groups"]) == 1
    assert report["groups"][0]["zip3"] == "209"
    assert report["groups"][0]["total"] == 3


def test_null_zip_rows_are_excluded(monkeypatch):
    rows = [_row(None, "approve"), _row("20912", "approve")]
    monkeypatch.setattr(db, "query", lambda sql, params=None: rows)

    report = fair_lending.zip_disparate_impact_report(min_group_size=1)

    assert len(report["groups"]) == 1
    assert report["groups"][0]["total"] == 1
