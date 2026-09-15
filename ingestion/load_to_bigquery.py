"""
Load a processed CAGED parquet file into the BigQuery star schema.

This is the final step of the pipeline:
  [CAGED .7z] -> raw (parquet, GCS) -> transform -> BigQuery (this script)

Dimension resolution strategy (see docs/DATA_SPEC.md §3 and §8):
  - dim_time: auto-created from ref_date — fully mechanical.
  - dim_location: auto-created from the IBGE state code via
    reference_data.resolve_state() — also fully mechanical.
  - dim_occupation: auto-created ONLY with a placeholder title
    ("CBO {code} (unreviewed)") and reviewed=FALSE. Never guesses a
    real display name — see docs/DATA_SPEC.md §3 and §8.

PERFORMANCE NOTE (see docs/TECH_SPEC.md §4): an earlier version of this
script resolved dimensions one row at a time, making one BigQuery round
trip per distinct value encountered during a plain Python loop over
potentially millions of rows — this was too slow to be usable even on a
single-state test slice. This version resolves each dimension in BULK:
compute the distinct values with pandas first, look up which already
exist in ONE query per dimension, insert only the missing ones in ONE
batch insert, then map ids back onto the DataFrame with a vectorized
pandas .map() — no per-row network calls at all.

BigQuery has no auto-increment, so surrogate keys are computed as
MAX(existing_id) + 1 per table.

Usage:
    python load_to_bigquery.py --period 202507
    python load_to_bigquery.py --period 202507 --state 42
"""

import argparse
import os
from datetime import datetime, timezone
import pandas as pd
from dotenv import load_dotenv
from google.cloud import bigquery

from reference_data import resolve_state

load_dotenv(override=True)

DATASET = "jobs_bi_brazil"


def compute_time_fields(ref_date: pd.Timestamp) -> dict:
    """Pure function: derive year/month/quarter from a reference date."""
    return {
        "ref_date": ref_date.date().isoformat(),
        "year": ref_date.year,
        "month": ref_date.month,
        "quarter": (ref_date.month - 1) // 3 + 1,
    }


def build_location_row(ibge_code: str) -> dict:
    """Pure function: build a dim_location row from an IBGE state code."""
    info = resolve_state(ibge_code)
    return {
        "state": info["abbreviation"],
        "city": None,
        "ibge_code": ibge_code,
        "region": info["region"],
    }


def build_occupation_placeholder_row(cbo_code: str) -> dict:
    """Pure function: build a dim_occupation row for a never-seen CBO code."""
    placeholder = f"CBO {cbo_code} (unreviewed)"
    return {
        "cbo_code": cbo_code,
        "official_title": placeholder,
        "display_name": placeholder,
        "sector_cnae": None,
        "reviewed": False,
    }


def _next_id(client: bigquery.Client, table: str, id_column: str) -> int:
    """I/O: compute the next surrogate key for a table with no auto-increment."""
    query = f"SELECT MAX({id_column}) AS max_id FROM `{DATASET}.{table}`"
    result = list(client.query(query).result())
    max_id = result[0]["max_id"]
    return 1 if max_id is None else max_id + 1


def resolve_ids_bulk(
    client: bigquery.Client,
    table: str,
    id_column: str,
    lookup_column: str,
    values: list,
    build_row_fn,
) -> dict:
    """
    Resolve a list of business-key values (e.g. CBO codes) to surrogate
    ids in bulk: one SELECT to find what already exists, one INSERT for
    whatever is missing. Returns {value: id} for every input value.
    """
    if not values:
        return {}

    quoted_values = ", ".join(f"'{v}'" for v in values)
    query = (
        f"SELECT {id_column}, {lookup_column} FROM `{DATASET}.{table}` "
        f"WHERE {lookup_column} IN ({quoted_values})"
    )
    existing = {row[lookup_column]: row[id_column] for row in client.query(query).result()}

    missing_values = [v for v in values if v not in existing]
    if missing_values:
        next_id = _next_id(client, table, id_column)
        new_rows = []
        for i, value in enumerate(missing_values):
            new_id = next_id + i
            new_rows.append({id_column: new_id, **build_row_fn(value)})
            existing[value] = new_id

        errors = client.insert_rows_json(f"{DATASET}.{table}", new_rows)
        if errors:
            raise RuntimeError(f"BigQuery insert errors while seeding {table}: {errors}")

    return existing

def check_period_not_already_loaded(client: bigquery.Client, ref_date_str: str) -> None:
    """
    Idempotency guard: refuse to load a period that already has fact
    rows, rather than silently duplicating them.
    """
    query = f"""
        SELECT COUNT(*) AS existing_rows
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        WHERE t.ref_date = '{ref_date_str}'
    """
    result = list(client.query(query).result())
    existing_rows = result[0]["existing_rows"]
    if existing_rows > 0:
        raise ValueError(
            f"Period {ref_date_str} already has {existing_rows} rows in fact_jobs — "
            "refusing to load again to avoid duplicating data. If this period genuinely "
            "needs to be reloaded (e.g. a source correction), drop and recreate the "
            "affected tables first — see docs/DATA_SPEC.md §9."
        )


def load_period(client: bigquery.Client, parquet_path: str, state_filter: str = None) -> int:
    """Load one period's fact rows, resolving all dimensions in bulk."""
    df = pd.read_parquet(parquet_path)

    if state_filter is not None:
        rows_before = len(df)
        df = df[df["state_ibge_code"] == state_filter]
        print(f"[filter] {rows_before} rows -> {len(df)} for state {state_filter}")

    df = df[df["event_type"].notna()].copy()
    print(f"[filter] {len(df)} rows remain after dropping unmapped event codes")

    unique_dates = df["ref_date"].dt.date.unique()

    for d in unique_dates:
        check_period_not_already_loaded(client, d.isoformat())

    print(f"[resolve] {len(unique_dates)} distinct period(s)")
    time_ids = resolve_ids_bulk(
        client, "dim_time", "time_id", "ref_date",
        [d.isoformat() for d in unique_dates],
        lambda date_str: compute_time_fields(pd.Timestamp(date_str)),
    )
    df["time_id"] = df["ref_date"].dt.date.astype(str).map(time_ids)

    unique_states = df["state_ibge_code"].dropna().unique().tolist()
    print(f"[resolve] {len(unique_states)} distinct state(s)")
    location_ids = resolve_ids_bulk(
        client, "dim_location", "location_id", "ibge_code",
        unique_states, build_location_row,
    )
    df["location_id"] = df["state_ibge_code"].map(location_ids)

    unique_cbos = df["cbo_code"].dropna().unique().tolist()
    print(f"[resolve] {len(unique_cbos)} distinct occupation(s)")
    occupation_ids = resolve_ids_bulk(
        client, "dim_occupation", "occupation_id", "cbo_code",
        unique_cbos, build_occupation_placeholder_row,
    )
    df["occupation_id"] = df["cbo_code"].map(occupation_ids)

    start_id = _next_id(client, "fact_jobs", "fact_job_id")
    df = df.reset_index(drop=True)
    df["fact_job_id"] = start_id + df.index

    # Build AND insert one chunk at a time — never materialize all rows
    # as Python dicts simultaneously. An earlier version built the full
    # list of ~4.3M dicts before inserting anything, which exceeded even
    # 4GiB of container memory in production. Processing chunk-by-chunk
    # keeps peak memory bounded by CHUNK_SIZE, not by the total row count.
    CHUNK_SIZE = 500
    total_rows = len(df)
    print(f"[load] inserting {total_rows} rows in chunks of {CHUNK_SIZE}")
    load_timestamp = datetime.now(timezone.utc).isoformat()

    for start in range(0, total_rows, CHUNK_SIZE):
        chunk_df = df.iloc[start:start + CHUNK_SIZE]
        chunk_records = chunk_df.apply(lambda r: {
            "load_timestamp": load_timestamp,
            "fact_job_id": int(r["fact_job_id"]),
            "time_id": int(r["time_id"]),
            "ref_date": r["ref_date"].date().isoformat(),
            "location_id": int(r["location_id"]),
            "occupation_id": int(r["occupation_id"]),
            "sector_id": None,
            "company_size_id": None,
            "event_type": r["event_type"],
            "salary": float(r["salary"]) if pd.notna(r["salary"]) else None,
            "is_typical": None,
            "gender": r["gender"],
            "age": int(r["age"]) if pd.notna(r["age"]) else None,
            "education_level": r["education_level"],
        }, axis=1).tolist()

        errors = client.insert_rows_json(f"{DATASET}.fact_jobs", chunk_records)
        if errors:
            raise RuntimeError(f"BigQuery insert errors: {errors}")

        if (start // CHUNK_SIZE) % 20 == 0:
            print(f"[load] {min(start + CHUNK_SIZE, total_rows)}/{total_rows} rows inserted")

    return total_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", required=True, help="YYYYMM format, e.g. 202508")
    parser.add_argument(
        "--state", required=False,
        help="Optional IBGE state code to test against a small slice first (e.g. 42 for Santa Catarina)."
    )
    args = parser.parse_args()

    raw_dir = os.path.join(os.path.dirname(__file__), "..", "raw")
    parquet_path = os.path.join(raw_dir, f"fact_caged_{args.period}.parquet")

    if not os.path.exists(parquet_path):
        raise FileNotFoundError(f"{parquet_path} not found — run caged_ingest.py first.")

    client = bigquery.Client()
    rows_loaded = load_period(client, parquet_path, state_filter=args.state)
    print(f"[ok] {rows_loaded} rows loaded into {DATASET}.fact_jobs for period {args.period}")


if __name__ == "__main__":
    main()