import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))

import pandas as pd
from caged_ingest import to_fact_format


def _sample_df(saldomovimentacao_values, cbo="317110"):
    n = len(saldomovimentacao_values)
    return pd.DataFrame({
        "uf": ["42"] * n,
        "cbo2002ocupação": [cbo] * n,
        "salário": ["5000,00"] * n,
        "saldomovimentação": saldomovimentacao_values,
        "sexo": ["M"] * n,
        "idade": ["30"] * n,
        "graudeinstrução": ["Ensino Médio Completo"] * n,
    })


def test_admission_code_maps_to_admission_event():
    df = _sample_df(["1"])
    result = to_fact_format(df, "202508")
    assert result.loc[0, "event_type"] == "admission"


def test_termination_code_maps_to_termination_event():
    df = _sample_df(["-1"])
    result = to_fact_format(df, "202508")
    assert result.loc[0, "event_type"] == "termination"


def test_unmapped_code_becomes_none_not_a_guess():
    df = _sample_df(["99"])
    result = to_fact_format(df, "202508")
    assert pd.isna(result.loc[0, "event_type"])


def test_no_occupation_filter_is_applied():
    df = _sample_df(["1"], cbo="999999")
    result = to_fact_format(df, "202508")
    assert result.loc[0, "cbo_code"] == "999999"


def test_ref_date_reflects_the_requested_period():
    df = _sample_df(["1"])
    result = to_fact_format(df, "202508")
    assert result.loc[0, "ref_date"] == pd.Timestamp("2025-08-01")


def test_demographic_fields_are_preserved():
    df = _sample_df(["1"])
    result = to_fact_format(df, "202508")
    assert result.loc[0, "gender"] == "M"
    assert result.loc[0, "age"] == 30
    assert result.loc[0, "education_level"] == "Ensino Médio Completo"


def test_brazilian_comma_decimal_salary_is_parsed_correctly():
    df = _sample_df(["1"])
    result = to_fact_format(df, "202508")
    assert result.loc[0, "salary"] == 5000.00


def test_state_is_kept_as_raw_ibge_code_not_guessed_as_abbreviation():
    df = _sample_df(["1"])
    result = to_fact_format(df, "202508")
    assert result.loc[0, "state_ibge_code"] == "42"