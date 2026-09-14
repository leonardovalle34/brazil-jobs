import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))

import pandas as pd
import pytest
from unittest.mock import patch, MagicMock
from load_to_bigquery import (
    compute_time_fields,
    build_location_row,
    build_occupation_placeholder_row,
    check_period_not_already_loaded
)


def test_compute_time_fields_for_first_quarter():
    result = compute_time_fields(pd.Timestamp("2025-02-01"))
    assert result == {"ref_date": "2025-02-01", "year": 2025, "month": 2, "quarter": 1}


def test_compute_time_fields_for_last_quarter():
    result = compute_time_fields(pd.Timestamp("2025-11-01"))
    assert result["quarter"] == 4


def test_compute_time_fields_quarter_boundary():
    # Month 7 (July) is the first month of Q3 — a common off-by-one spot.
    result = compute_time_fields(pd.Timestamp("2025-07-01"))
    assert result["quarter"] == 3


def test_build_location_row_resolves_known_state():
    row = build_location_row("35")
    assert row["state"] == "SP"
    assert row["region"] == "Southeast"
    assert row["ibge_code"] == "35"


def test_build_location_row_leaves_city_null():
    # City is not yet extracted from source — see docs/DATA_SPEC.md.
    row = build_location_row("42")
    assert row["city"] is None


def test_build_occupation_placeholder_never_invents_a_real_title():
    row = build_occupation_placeholder_row("317110")
    assert row["reviewed"] is False
    assert "317110" in row["official_title"]
    assert "unreviewed" in row["official_title"]
    # display_name must match official_title exactly for placeholders —
    # they must never diverge until a human reviews the row, per
    # docs/DATA_SPEC.md §3 and §8.
    assert row["display_name"] == row["official_title"]

def test_raises_when_period_already_has_rows():
    mock_client = MagicMock()
    mock_result = [{"existing_rows": 4373105}]
    mock_client.query.return_value.result.return_value = mock_result

    with pytest.raises(ValueError, match="already has 4373105 rows"):
        check_period_not_already_loaded(mock_client, "2025-07-01")


def test_does_not_raise_when_period_is_new():
    mock_client = MagicMock()
    mock_result = [{"existing_rows": 0}]
    mock_client.query.return_value.result.return_value = mock_result

    check_period_not_already_loaded(mock_client, "2025-09-01")