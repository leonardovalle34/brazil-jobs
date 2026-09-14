"""
CAGED microdata ingestion (admissions/terminations) — full national dataset.

Source: Brazilian Ministry of Labor public FTP
  ftp://ftp.mtps.gov.br/pdet/microdados/NOVO CAGED/

Files are released as one .7z archive per reference month, containing
admission/termination records across every occupation (CBO), sector,
state, city and salary — no industry filter is applied (see
docs/PRD.md §3 — this project intentionally covers the general labor
market, not a single industry).

See docs/DATA_SPEC.md for source documentation and known limitations.

Usage:
    python caged_ingest.py --period 202508
"""

import argparse
import os
import ftplib
import py7zr
import pandas as pd

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "raw")
FTP_HOST = "ftp.mtps.gov.br"
FTP_BASE_PATH = "/pdet/microdados/NOVO CAGED"

EVENT_TYPE_MAP = {"1": "admission", "-1": "termination"}


def download_period(period: str) -> str:
    """
    Download the .7z file for a reference period (YYYYMM format), return
    the local path.

    This source is a plain FTP server, not HTTPS. Remote path has a year
    folder AND a year+month subfolder, e.g.:
      /pdet/microdados/NOVO CAGED/2025/202507/CAGEDMOV202507.7z
    """
    os.makedirs(RAW_DIR, exist_ok=True)
    year = period[:4]
    local_path = os.path.join(RAW_DIR, f"CAGEDMOV{period}.7z")

    if os.path.exists(local_path):
        print(f"[skip] {local_path} already downloaded")
        return local_path

    remote_path = f"{FTP_BASE_PATH}/{year}/{period}/CAGEDMOV{period}.7z"
    print(f"[download] ftp://{FTP_HOST}{remote_path}")

    with ftplib.FTP(FTP_HOST, timeout=180) as ftp:
        ftp.login()  # anonymous — no credentials needed for this public dataset
        with open(local_path, "wb") as f:
            ftp.retrbinary(f"RETR {remote_path}", f.write)

    return local_path


def extract(archive_path: str, period: str) -> pd.DataFrame:
    """
    Extract the .7z into a DataFrame with no occupation/sector filter.

    NOTE: full-month files are large (see docs/TECH_SPEC.md §4). For a
    first pass this reads the whole file into memory; if that becomes a
    bottleneck against real data, switch to chunked reading
    (pd.read_csv(..., chunksize=...)) before this function's contract
    changes for callers.
    """
    extract_dir = os.path.join(RAW_DIR, period)
    with py7zr.SevenZipFile(archive_path, mode="r") as archive:
        archive.extractall(path=extract_dir)

    data_files = [f for f in os.listdir(extract_dir) if f.lower().endswith((".txt", ".csv"))]
    if not data_files:
        raise FileNotFoundError(f"No data file found in {extract_dir}")

    df = pd.read_csv(
        os.path.join(extract_dir, data_files[0]),
        sep=";",
        encoding="utf-8",
        dtype=str,
    )
    df.columns = [c.strip().lower() for c in df.columns]
    print(f"[extract] {len(df)} rows read for {period} (full dataset, no filter)")
    return df


def to_fact_format(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """
    Normalize raw CAGED columns into the fact_jobs shape.

    Known source quirks (confirmed against a real 2025-07 file, not just
    documentation):
      - salary ("salário") uses Brazilian comma-decimal notation
        ("1518,00"), which pd.to_numeric does not parse directly — the
        comma must be replaced with a period first.
      - "uf" is the numeric IBGE state code (e.g. "23", "35"), not the
        two-letter abbreviation (CE, SP). A code-to-abbreviation lookup
        is a pending step — see docs/DATA_SPEC.md — this function keeps
        the raw IBGE code for now rather than guessing a mapping.
      - there is no age-bracket column in the source; only raw "idade"
        (integer age) is available. Bucketing into brackets is a
        transform decision to make deliberately, not silently — this
        function keeps the raw age for now.
    """
    salary_raw = df.get("salário")
    salary_clean = salary_raw.str.replace(",", ".", regex=False) if salary_raw is not None else None

    out = pd.DataFrame({
        "ref_date": pd.to_datetime(period, format="%Y%m"),
        "state_ibge_code": df.get("uf"),  # numeric IBGE code — see docstring
        "cbo_code": df.get("cbo2002ocupação"),
        "salary": pd.to_numeric(salary_clean, errors="coerce") if salary_clean is not None else None,
        "event_type": df.get("saldomovimentação").map(EVENT_TYPE_MAP) if "saldomovimentação" in df else None,
        "gender": df.get("sexo"),
        "age": pd.to_numeric(df.get("idade"), errors="coerce"),  # raw age, not a bracket — see docstring
        "education_level": df.get("graudeinstrução"),
    })
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", required=True, help="YYYYMM format, e.g. 202508")
    args = parser.parse_args()

    archive_path = download_period(args.period)
    df_raw = extract(archive_path, args.period)
    df_fact = to_fact_format(df_raw, args.period)

    out_path = os.path.join(RAW_DIR, f"fact_caged_{args.period}.parquet")
    df_fact.to_parquet(out_path, index=False)
    print(f"[ok] {len(df_fact)} records saved to {out_path}")


if __name__ == "__main__":
    main()