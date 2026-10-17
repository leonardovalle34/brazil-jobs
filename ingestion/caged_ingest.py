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
from datetime import datetime, timedelta, timezone
import py7zr
import pandas as pd

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "raw")
FTP_HOST = "ftp.mtps.gov.br"
FTP_BASE_PATH = "/pdet/microdados/NOVO CAGED"

EVENT_TYPE_MAP = {"1": "admission", "-1": "termination"}

# First month this project tracks — automatic detection never looks
# further back than this (see /next-period in app.py).
FIRST_PERIOD = "202506"

# Brazil has had no daylight saving time since 2019, so a fixed UTC-3
# offset is exact for America/Sao_Paulo and avoids depending on tzdata
# being present in the slim container image.
SAO_PAULO_TZ = timezone(timedelta(hours=-3))


class PeriodNotPublishedError(Exception):
    """The CAGED archive for a period does not exist on the FTP (yet)."""

    def __init__(self, period: str):
        self.period = period
        super().__init__(f"Arquivo do CAGED para {period} ainda não foi publicado")


def remote_path_for(period: str) -> str:
    """
    Pure function: FTP path of a period's archive. The remote layout has
    a year folder AND a year+month subfolder, e.g.:
      /pdet/microdados/NOVO CAGED/2025/202507/CAGEDMOV202507.7z
    """
    return f"{FTP_BASE_PATH}/{period[:4]}/{period}/CAGEDMOV{period}.7z"


def _is_not_found(error: ftplib.error_perm) -> bool:
    """FTP reply 550 = file/directory unavailable — i.e. not published yet."""
    return str(error).startswith("550")


def open_ftp() -> ftplib.FTP:
    """I/O: anonymous login — no credentials needed for this public dataset."""
    ftp = ftplib.FTP(FTP_HOST, timeout=180)
    ftp.login()
    return ftp


def period_exists_on_ftp(ftp: ftplib.FTP, period: str) -> bool:
    """
    Check whether a period's archive is published WITHOUT downloading it:
    a SIZE command only returns metadata. SIZE requires binary mode on
    many servers, hence the TYPE I first. Any error other than 550
    (network, permission) is propagated — it is not proof of absence.
    """
    ftp.voidcmd("TYPE I")
    try:
        ftp.size(remote_path_for(period))
        return True
    except ftplib.error_perm as e:
        if _is_not_found(e):
            return False
        raise


def iter_periods(start: str, end: str) -> list:
    """Pure function: every YYYYMM from start to end, inclusive."""
    year, month = int(start[:4]), int(start[4:])
    periods = []
    while f"{year:04d}{month:02d}" <= end:
        periods.append(f"{year:04d}{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return periods


def current_period(now: datetime = None) -> str:
    """The current month (YYYYMM) in America/Sao_Paulo."""
    now = now or datetime.now(SAO_PAULO_TZ)
    return now.astimezone(SAO_PAULO_TZ).strftime("%Y%m")


def missing_periods(loaded: set, start: str, end: str) -> list:
    """Pure function: periods in [start, end] not yet loaded, oldest first."""
    return [p for p in iter_periods(start, end) if p not in loaded]


def find_next_period(ftp: ftplib.FTP, loaded: set, start: str, end: str):
    """
    Oldest period in [start, end] that is not loaded yet AND already
    published on the FTP, or None. A gap that is genuinely missing from
    the FTP is skipped rather than blocking later months.
    """
    for period in missing_periods(loaded, start, end):
        if period_exists_on_ftp(ftp, period):
            return period
    return None


def download_period(period: str) -> str:
    """
    Download the .7z file for a reference period (YYYYMM format), return
    the local path. Raises PeriodNotPublishedError if the FTP answers 550.

    This source is a plain FTP server, not HTTPS. The archive is written
    to a ".part" file and only renamed on success: an earlier version
    opened the final path before RETR, so a failed transfer left an
    empty file behind that the "[skip] already downloaded" check would
    then happily reuse on a warm Cloud Run instance.
    """
    os.makedirs(RAW_DIR, exist_ok=True)
    local_path = os.path.join(RAW_DIR, f"CAGEDMOV{period}.7z")

    if os.path.exists(local_path):
        print(f"[skip] {local_path} already downloaded")
        return local_path

    remote_path = remote_path_for(period)
    partial_path = local_path + ".part"
    print(f"[download] ftp://{FTP_HOST}{remote_path}")

    try:
        with open_ftp() as ftp:
            with open(partial_path, "wb") as f:
                ftp.retrbinary(f"RETR {remote_path}", f.write)
    except BaseException as e:
        if os.path.exists(partial_path):
            os.remove(partial_path)
        if isinstance(e, ftplib.error_perm) and _is_not_found(e):
            raise PeriodNotPublishedError(period) from e
        raise

    os.replace(partial_path, local_path)
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