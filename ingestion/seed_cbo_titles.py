"""
Seed official CBO occupation titles into dim_occupation.

Downloads the official CBO CSV from the Ministry of Labor (MTE),
matches codes against the 2,604 unreviewed dim_occupation rows, and
does a single UPDATE per row, marking reviewed=True once the official
title is confirmed.

Source (public domain, Creative Commons BY-ND 3.0):
  https://www.gov.br/trabalho-e-emprego/pt-br/assuntos/cbo/servicos/downloads/cbo2002-ocupacao.csv

Usage:
    python seed_cbo_titles.py --dry-run   # preview changes, no writes
    python seed_cbo_titles.py             # apply changes to BigQuery
"""

import argparse
import io
import os

import pandas as pd
import requests
from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv(override=True)

CBO_CSV_URL = (
    "https://www.gov.br/trabalho-e-emprego/pt-br/assuntos/cbo/"
    "servicos/downloads/cbo2002-ocupacao.csv"
)
DATASET = "jobs_bi_brazil"


def download_cbo_csv() -> pd.DataFrame:
    print(f"[download] {CBO_CSV_URL}")
    resp = requests.get(CBO_CSV_URL, timeout=60)
    resp.raise_for_status()
    df = pd.read_csv(
        io.StringIO(resp.content.decode("windows-1252")),
        sep=";",
        dtype=str,
    )
    df.columns = [c.strip().lower() for c in df.columns]
    print(f"[ok] {len(df)} occupation codes downloaded from MTE")
    return df


def build_cbo_lookup(df: pd.DataFrame) -> dict:
    code_col = next((c for c in df.columns if "codigo" in c or "code" in c), None)
    title_col = next((c for c in df.columns if "titulo" in c or "nome" in c or "title" in c), None)

    if not code_col or not title_col:
        raise ValueError(f"Could not identify code/title columns. Found: {list(df.columns)}")

    print(f"[columns] using '{code_col}' as code, '{title_col}' as title")

    lookup = {}
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().replace("-", "").replace(".", "")
        title = str(row[title_col]).strip()
        if code and title:
            lookup[code] = title
    return lookup


def fetch_unreviewed(client: bigquery.Client) -> list:
    query = f"""
        SELECT occupation_id, cbo_code
        FROM `{DATASET}.dim_occupation`
        WHERE NOT reviewed
        ORDER BY cbo_code
    """
    return [
        {"occupation_id": row["occupation_id"], "cbo_code": row["cbo_code"]}
        for row in client.query(query).result()
    ]


def update_occupation(client: bigquery.Client, occupation_id: int, official_title: str, dry_run: bool) -> None:
    if dry_run:
        return
    query = f"""
        UPDATE `{DATASET}.dim_occupation`
        SET official_title = @title, display_name = @title, reviewed = TRUE
        WHERE occupation_id = @id
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("title", "STRING", official_title),
            bigquery.ScalarQueryParameter("id", "INT64", occupation_id),
        ]
    )
    client.query(query, job_config=job_config).result()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    cbo_df = download_cbo_csv()
    cbo_lookup = build_cbo_lookup(cbo_df)

    client = bigquery.Client()
    unreviewed = fetch_unreviewed(client)
    print(f"[info] {len(unreviewed)} unreviewed occupation rows in dim_occupation")

    matched = 0
    not_found = []

    for row in unreviewed:
        code = row["cbo_code"].replace("-", "").replace(".", "").strip()
        title = cbo_lookup.get(code)
        if title:
            update_occupation(client, row["occupation_id"], title, args.dry_run)
            matched += 1
            if matched <= 5 or args.dry_run:
                print(f"  {'[dry]' if args.dry_run else '[ok]'} {code} → {title}")
        else:
            not_found.append(code)

    print(f"\n[summary]")
    print(f"  matched and {'would update' if args.dry_run else 'updated'}: {matched}")
    print(f"  not found in MTE CSV: {len(not_found)}")
    if not_found:
        print(f"  sample not found: {not_found[:10]}")


if __name__ == "__main__":
    main()