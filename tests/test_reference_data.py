import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))

import pytest
from reference_data import resolve_state


def test_resolves_known_state_code():
    result = resolve_state("35")
    assert result["abbreviation"] == "SP"
    assert result["region"] == "Southeast"


def test_resolves_another_known_state_code():
    result = resolve_state("42")
    assert result["abbreviation"] == "SC"
    assert result["region"] == "South"


def test_unrecognized_code_raises_instead_of_guessing():
    with pytest.raises(KeyError):
        resolve_state("77")


def test_28_entries_present_27_states_plus_unidentified_code():
    from reference_data import IBGE_STATE_CODE_TO_INFO
    assert len(IBGE_STATE_CODE_TO_INFO) == 28


def test_code_99_resolves_to_unidentified_not_an_error():
    result = resolve_state("99")
    assert result["abbreviation"] == "NI"