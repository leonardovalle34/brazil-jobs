"""
Static reference data: IBGE numeric state codes -> abbreviation and region.

This resolves the pending lookup noted in docs/DATA_SPEC.md §7 — CAGED's
"uf" field is the numeric IBGE code, not the two-letter abbreviation.
This table is small, stable (Brazil's state codes haven't changed since
IBGE assigned them), and safe to hard-code rather than fetch from an
external source.

Source: IBGE's own state code table (public, standard reference used
across Brazilian government datasets).
"""

IBGE_STATE_CODE_TO_INFO = {
    "11": {"abbreviation": "RO", "region": "North"},
    "12": {"abbreviation": "AC", "region": "North"},
    "13": {"abbreviation": "AM", "region": "North"},
    "14": {"abbreviation": "RR", "region": "North"},
    "15": {"abbreviation": "PA", "region": "North"},
    "16": {"abbreviation": "AP", "region": "North"},
    "17": {"abbreviation": "TO", "region": "North"},
    "21": {"abbreviation": "MA", "region": "Northeast"},
    "22": {"abbreviation": "PI", "region": "Northeast"},
    "23": {"abbreviation": "CE", "region": "Northeast"},
    "24": {"abbreviation": "RN", "region": "Northeast"},
    "25": {"abbreviation": "PB", "region": "Northeast"},
    "26": {"abbreviation": "PE", "region": "Northeast"},
    "27": {"abbreviation": "AL", "region": "Northeast"},
    "28": {"abbreviation": "SE", "region": "Northeast"},
    "29": {"abbreviation": "BA", "region": "Northeast"},
    "31": {"abbreviation": "MG", "region": "Southeast"},
    "32": {"abbreviation": "ES", "region": "Southeast"},
    "33": {"abbreviation": "RJ", "region": "Southeast"},
    "35": {"abbreviation": "SP", "region": "Southeast"},
    "41": {"abbreviation": "PR", "region": "South"},
    "42": {"abbreviation": "SC", "region": "South"},
    "43": {"abbreviation": "RS", "region": "South"},
    "50": {"abbreviation": "MS", "region": "Central-West"},
    "51": {"abbreviation": "MT", "region": "Central-West"},
    "52": {"abbreviation": "GO", "region": "Central-West"},
    "53": {"abbreviation": "DF", "region": "Central-West"},
    "99": {"abbreviation": "NI", "region": "Unidentified"},
}


def resolve_state(ibge_code: str) -> dict:
    """
    Look up a state's abbreviation and region from its IBGE code.

    Raises KeyError for an unrecognized code rather than returning a
    guessed/default value — an unrecognized code means either bad source
    data or a gap in this table, and both deserve investigation, not a
    silent fallback.
    """
    if ibge_code not in IBGE_STATE_CODE_TO_INFO:
        raise KeyError(f"Unrecognized IBGE state code: {ibge_code!r}")
    return IBGE_STATE_CODE_TO_INFO[ibge_code]