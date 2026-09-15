"""
Tests for the public aggregation API.

The endpoint functions are called directly rather than through an HTTP
test client: that keeps the suite dependency-free (pytest only) and
still covers everything that is ours — SQL shape, aggregation and
error handling. What it does NOT cover is FastAPI's own query-param
validation (e.g. limit > 200 -> 422), which is framework behaviour.

Because the functions are called directly, every parameter must be
passed explicitly — an omitted one would arrive as FastAPI's `Query(...)`
object, not as its default value.

The BigQuery boundary is replaced by a fake client, per
docs/VALIDATION_SPEC.md §1 (mock the I/O boundary, unit-test the logic
on either side of it). The fake also records the SQL it was given, so
the tests can assert on query shape without a warehouse.
"""

import sys
import os
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import pytest
from fastapi import HTTPException

import main
from main import (
    AGE_BUCKETS,
    age_bucket_case_sql,
    by_region,
    by_state,
    demographics,
    demographics_age,
    national_summary,
    national_timeseries,
    period_range_to_ref_dates,
    period_to_ref_date,
    quartiles_from_approx,
    salary_distribution,
    top_occupations,
)

LOADED_AT = datetime(2026, 9, 14, 20, 9, tzinfo=timezone.utc)


class FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    """Returns each queued result set in order, recording every query."""

    def __init__(self, result_sets):
        self._result_sets = list(result_sets)
        self.queries = []

    def query(self, query, job_config=None):
        self.queries.append(query)
        return FakeQueryJob(self._result_sets.pop(0) if self._result_sets else [])


def use_rows(monkeypatch, *result_sets):
    fake = FakeClient(result_sets)
    monkeypatch.setattr(main, "get_client", lambda: fake)
    return fake


def status_of(call, *args, **kwargs):
    """Run an endpoint expected to fail, returning its HTTP status code."""
    with pytest.raises(HTTPException) as exc_info:
        call(*args, **kwargs)
    return exc_info.value.status_code


# --- period parsing -------------------------------------------------


def test_valid_period_converts_to_ref_date():
    assert period_to_ref_date("202509") == "2025-09-01"


def test_valid_period_january():
    assert period_to_ref_date("202601") == "2026-01-01"


def test_wrong_length_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        period_to_ref_date("2025")
    assert exc_info.value.status_code == 400


def test_non_digit_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        period_to_ref_date("2025ab")
    assert exc_info.value.status_code == 400


def test_invalid_month_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        period_to_ref_date("202513")
    assert exc_info.value.status_code == 400


def test_month_zero_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        period_to_ref_date("202500")
    assert exc_info.value.status_code == 400


def test_period_range_converts_both_bounds():
    assert period_range_to_ref_dates("202506", "202509") == ("2025-06-01", "2025-09-01")


def test_period_range_allows_open_bounds():
    assert period_range_to_ref_dates(None, None) == (None, None)
    assert period_range_to_ref_dates("202506", None) == ("2025-06-01", None)
    assert period_range_to_ref_dates(None, "202509") == (None, "2025-09-01")


def test_period_range_allows_single_month():
    assert period_range_to_ref_dates("202509", "202509") == ("2025-09-01", "2025-09-01")


def test_inverted_period_range_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        period_range_to_ref_dates("202509", "202506")
    assert exc_info.value.status_code == 400


def test_malformed_bound_in_range_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        period_range_to_ref_dates("2025", "202509")
    assert exc_info.value.status_code == 400


# --- age bucketing and quantile helpers -----------------------------


def test_age_bucket_case_covers_every_bucket_and_has_unknown_fallback():
    sql = age_bucket_case_sql("f.age")
    for label, _, _ in AGE_BUCKETS:
        assert f"'{label}'" in sql
    assert sql.startswith("CASE ")
    assert sql.endswith("ELSE 'unknown' END")


def test_age_bucket_case_uses_open_ended_comparisons_at_the_edges():
    sql = age_bucket_case_sql("f.age")
    assert "WHEN f.age <= 17 THEN 'under_18'" in sql
    assert "WHEN f.age BETWEEN 18 AND 24 THEN '18_24'" in sql
    assert "WHEN f.age >= 65 THEN '65_plus'" in sql


def test_age_buckets_are_contiguous_and_non_overlapping():
    bounded = [(low, high) for _, low, high in AGE_BUCKETS if low is not None and high is not None]
    for (_, high), (low, _) in zip(bounded, bounded[1:]):
        assert low == high + 1


def test_quartiles_from_approx_maps_five_boundaries():
    assert quartiles_from_approx([1200, 1518, 2000, 3200, 90000]) == {
        "min": 1200.0,
        "p25": 1518.0,
        "median": 2000.0,
        "p75": 3200.0,
        "max": 90000.0,
    }


@pytest.mark.parametrize("quantiles", [None, [], [1518, 2000, 3200]])
def test_quartiles_from_approx_returns_nulls_for_missing_input(quantiles):
    assert quartiles_from_approx(quantiles) == {
        "min": None,
        "p25": None,
        "median": None,
        "p75": None,
        "max": None,
    }


# --- /national/summary ----------------------------------------------


def test_summary_returns_net_balance(monkeypatch):
    use_rows(
        monkeypatch,
        [{"event_type": "admission", "total": 2292492}, {"event_type": "termination", "total": 2079490}],
        [{"last_updated": LOADED_AT}],
    )

    body = national_summary(period="202509")

    assert body["net_balance"] == 213002
    assert body["last_updated"] == LOADED_AT.isoformat()


def test_summary_with_no_data_returns_404(monkeypatch):
    use_rows(monkeypatch, [])
    assert status_of(national_summary, period="202509") == 404


# --- /national/by-state ---------------------------------------------


def test_by_state_returns_net_balance_per_state(monkeypatch):
    use_rows(
        monkeypatch,
        [{"state": "SP", "region": "Southeast", "admissions": 900, "terminations": 800}],
    )

    body = by_state(period="202509")

    assert body["states"][0]["state"] == "SP"
    assert body["states"][0]["net_balance"] == 100


def test_by_state_with_no_data_returns_404(monkeypatch):
    use_rows(monkeypatch, [])
    assert status_of(by_state, period="202509") == 404


# --- /occupations/top -----------------------------------------------


def test_top_occupations_returns_net_balance_and_review_flag(monkeypatch):
    use_rows(
        monkeypatch,
        [
            {
                "cbo_code": "514320",
                "display_name": "Faxineiro",
                "reviewed": False,
                "admissions": 150000,
                "terminations": 140000,
            }
        ],
    )

    body = top_occupations(period="202509", limit=5)

    assert body["occupations"][0]["net_balance"] == 10000
    assert body["occupations"][0]["reviewed"] is False


def test_top_occupations_with_no_data_returns_404(monkeypatch):
    use_rows(monkeypatch, [])
    assert status_of(top_occupations, period="202509", limit=20) == 404


# --- /demographics --------------------------------------------------


def test_demographics_returns_raw_codes_and_average_age(monkeypatch):
    use_rows(
        monkeypatch,
        [{"gender": "1", "total": 1200}, {"gender": "3", "total": 900}],
        [{"education_level": "7", "total": 2100}],
        [{"avg_age": 33.27}],
    )

    body = demographics(period="202509")

    assert body["gender"] == {"1": 1200, "3": 900}
    assert body["average_age"] == 33.3
    assert "raw source codes" in body["note"]


def test_demographics_with_no_data_returns_404(monkeypatch):
    use_rows(monkeypatch, [], [], [{"avg_age": None}])
    assert status_of(demographics, period="202509") == 404


# --- /national/timeseries -------------------------------------------


def test_timeseries_returns_one_entry_per_loaded_month(monkeypatch):
    use_rows(
        monkeypatch,
        [
            {"ref_date": date(2025, 6, 1), "admissions": 2139182, "terminations": 1972561, "last_updated": LOADED_AT},
            {"ref_date": date(2025, 9, 1), "admissions": 2292492, "terminations": 2079490, "last_updated": LOADED_AT},
        ],
    )

    body = national_timeseries(start=None, end=None)

    assert body["start"] == "202506"
    assert body["end"] == "202509"
    assert [m["period"] for m in body["months"]] == ["202506", "202509"]
    assert body["months"][0]["net_balance"] == 166621
    assert body["last_updated"] == LOADED_AT.isoformat()


def test_timeseries_does_not_zero_fill_gap_months(monkeypatch):
    """A month that was never loaded must be absent, not reported as zero."""
    use_rows(
        monkeypatch,
        [
            {"ref_date": date(2025, 6, 1), "admissions": 10, "terminations": 4, "last_updated": LOADED_AT},
            {"ref_date": date(2025, 9, 1), "admissions": 12, "terminations": 5, "last_updated": LOADED_AT},
        ],
    )

    periods = [m["period"] for m in national_timeseries(start=None, end=None)["months"]]

    assert "202507" not in periods
    assert "202508" not in periods


def test_timeseries_passes_both_range_bounds_to_the_query(monkeypatch):
    fake = use_rows(
        monkeypatch,
        [{"ref_date": date(2025, 6, 1), "admissions": 10, "terminations": 4, "last_updated": LOADED_AT}],
    )

    national_timeseries(start="202506", end="202509")

    assert "t.ref_date >= @start_date" in fake.queries[0]
    assert "t.ref_date <= @end_date" in fake.queries[0]


def test_timeseries_with_no_loaded_months_returns_404(monkeypatch):
    use_rows(monkeypatch, [])
    assert status_of(national_timeseries, start=None, end=None) == 404


def test_timeseries_rejects_inverted_range(monkeypatch):
    use_rows(monkeypatch, [])
    assert status_of(national_timeseries, start="202509", end="202506") == 400


# --- /national/by-region --------------------------------------------


def test_by_region_returns_net_balance_per_region(monkeypatch):
    use_rows(
        monkeypatch,
        [
            {"region": "Southeast", "admissions": 900, "terminations": 800, "last_updated": LOADED_AT},
            {"region": "South", "admissions": 400, "terminations": 390, "last_updated": LOADED_AT},
        ],
    )

    body = by_region(period="202509")

    assert body["period"] == "202509"
    assert body["regions"][0] == {
        "region": "Southeast",
        "admissions": 900,
        "terminations": 800,
        "net_balance": 100,
    }
    assert body["last_updated"] == LOADED_AT.isoformat()


def test_by_region_with_no_data_returns_404(monkeypatch):
    use_rows(monkeypatch, [])
    assert status_of(by_region, period="202509") == 404


def test_by_region_rejects_malformed_period(monkeypatch):
    use_rows(monkeypatch, [])
    assert status_of(by_region, period="2025") == 400


# --- /salary/distribution -------------------------------------------


def test_salary_distribution_by_state(monkeypatch):
    fake = use_rows(
        monkeypatch,
        [
            {
                "label": "SP",
                "admissions_with_salary": 500000,
                "quantiles": [1000, 1518, 2100, 3400, 120000],
                "last_updated": LOADED_AT,
            }
        ],
    )

    body = salary_distribution(period="202509", group_by="state", limit=20)

    assert body["group_by"] == "state"
    assert body["currency"] == "BRL"
    assert body["groups"][0]["label"] == "SP"
    assert body["groups"][0]["median"] == 2100.0
    assert "dim_location" in fake.queries[0]


def test_salary_distribution_by_occupation_joins_the_occupation_dimension(monkeypatch):
    fake = use_rows(
        monkeypatch,
        [
            {
                "label": "Faxineiro",
                "admissions_with_salary": 1000,
                "quantiles": [1000, 1518, 1600, 1800, 5000],
                "last_updated": LOADED_AT,
            }
        ],
    )

    body = salary_distribution(period="202509", group_by="occupation", limit=20)

    assert body["groups"][0]["label"] == "Faxineiro"
    assert "dim_occupation" in fake.queries[0]


def test_salary_distribution_excludes_terminations_and_unreported_pay(monkeypatch):
    """
    The distribution answers "what does the market pay to hire", so
    terminations and null/zero salaries must be filtered in SQL.
    """
    fake = use_rows(
        monkeypatch,
        [
            {
                "label": "SP",
                "admissions_with_salary": 10,
                "quantiles": [1, 2, 3, 4, 5],
                "last_updated": LOADED_AT,
            }
        ],
    )

    salary_distribution(period="202509", group_by="state", limit=20)

    query = fake.queries[0]
    assert "f.event_type = 'admission'" in query
    assert "f.salary IS NOT NULL" in query
    assert "f.salary > 0" in query


def test_salary_distribution_tolerates_a_group_with_no_quantiles(monkeypatch):
    use_rows(
        monkeypatch,
        [
            {"label": "AC", "admissions_with_salary": 0, "quantiles": None, "last_updated": LOADED_AT},
        ],
    )

    body = salary_distribution(period="202509", group_by="state", limit=20)

    assert body["groups"][0]["median"] is None


def test_salary_distribution_rejects_unknown_group_by(monkeypatch):
    use_rows(monkeypatch, [])
    assert status_of(salary_distribution, period="202509", group_by="sector", limit=20) == 400


def test_salary_distribution_with_no_data_returns_404(monkeypatch):
    use_rows(monkeypatch, [])
    assert status_of(salary_distribution, period="202509", group_by="state", limit=20) == 404


# --- /demographics/age ----------------------------------------------


def test_age_profile_lists_every_bucket_in_order(monkeypatch):
    use_rows(
        monkeypatch,
        [
            {"bucket": "25_29", "admissions": 300, "last_updated": LOADED_AT},
            {"bucket": "18_24", "admissions": 500, "last_updated": LOADED_AT},
        ],
    )

    body = demographics_age(period="202509")

    assert [b["bucket"] for b in body["buckets"]] == [label for label, _, _ in AGE_BUCKETS] + ["unknown"]
    assert body["buckets"][1] == {"bucket": "18_24", "admissions": 500}


def test_age_profile_reports_missing_buckets_as_zero(monkeypatch):
    """A bucket absent from the GROUP BY is a zero, not a hole in the chart."""
    use_rows(monkeypatch, [{"bucket": "30_39", "admissions": 7, "last_updated": LOADED_AT}])

    buckets = {b["bucket"]: b["admissions"] for b in demographics_age(period="202509")["buckets"]}

    assert buckets["30_39"] == 7
    assert buckets["65_plus"] == 0
    assert buckets["unknown"] == 0


def test_age_profile_counts_only_admissions(monkeypatch):
    fake = use_rows(monkeypatch, [{"bucket": "30_39", "admissions": 7, "last_updated": LOADED_AT}])

    demographics_age(period="202509")

    assert "f.event_type = 'admission'" in fake.queries[0]


def test_age_profile_with_no_data_returns_404(monkeypatch):
    use_rows(monkeypatch, [])
    assert status_of(demographics_age, period="202509") == 404