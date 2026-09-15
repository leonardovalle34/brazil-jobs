import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import pytest
from fastapi import HTTPException
from main import period_to_ref_date


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