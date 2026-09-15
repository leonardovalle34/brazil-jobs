"""
Public, read-only aggregation API for the dashboard frontend.

This is a SEPARATE Cloud Run service from app.py (the pipeline control
API). Cloud Run authentication is per-service, not per-route — the
pipeline endpoints (/download, /transform, /load) must stay behind
"Require authentication", but this API must be public so the dashboard
can call it without a login. Combining them into one service would
force one auth setting on both, which is wrong for at least one of them.

Every endpoint here is read-only aggregation over public government
data (CAGED) — no row-level or personally identifiable data is ever
exposed, consistent with docs/PRD.md's "no login required" decision.

Endpoints NOT implemented yet, and why (see docs/DATA_SPEC.md §8 and
sql/schema.sql): fact_jobs.sector_id and .is_typical are always NULL —
this pipeline has never populated them from source. A "by sector" or
"typical vs non-typical" endpoint would only be able to return nulls,
which is worse than not having the endpoint. Add them once
sector/contract-type extraction is added to caged_ingest.py.
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from google.cloud import bigquery

load_dotenv(override=True)

DATASET = "jobs_bi_brazil"

app = FastAPI(title="jobs_bi_brazil public API")

# CORS: open for now (no dashboard domain exists yet to restrict to).
# TODO before real deploy of the dashboard: replace "*" with the actual
# dashboard origin(s) — see docs/TECH_SPEC.md.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def period_to_ref_date(period: str) -> str:
    """
    Pure function: validate and convert a YYYYMM period string into the
    YYYY-MM-01 date format stored in dim_time.ref_date.

    Raises HTTPException (400) for a malformed period rather than
    letting a bad value reach BigQuery as an opaque query error.
    """
    if len(period) != 6 or not period.isdigit():
        raise HTTPException(status_code=400, detail=f"Invalid period format: {period!r}. Expected YYYYMM.")
    year, month = period[:4], period[4:]
    if not (1 <= int(month) <= 12):
        raise HTTPException(status_code=400, detail=f"Invalid month in period: {period!r}.")
    return f"{year}-{month}-01"


def get_client() -> bigquery.Client:
    return bigquery.Client()


@app.get("/")
def health():
    return {"status": "ok", "service": "jobs_bi_brazil-public-api"}


@app.get("/national/summary")
def national_summary(period: str = Query(..., description="YYYYMM, e.g. 202509")):
    ref_date = period_to_ref_date(period)
    client = get_client()

    query = f"""
        SELECT
          event_type,
          COUNT(*) AS total
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        WHERE t.ref_date = @ref_date
        GROUP BY event_type
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("ref_date", "DATE", ref_date)]
    )
    rows = {row["event_type"]: row["total"] for row in client.query(query, job_config=job_config).result()}

    admissions = rows.get("admission", 0)
    terminations = rows.get("termination", 0)

    if admissions == 0 and terminations == 0:
        raise HTTPException(status_code=404, detail=f"No data loaded for period {period}.")

    last_updated_query = f"""
        SELECT MAX(load_timestamp) AS last_updated
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        WHERE t.ref_date = @ref_date
    """
    last_updated_row = list(client.query(last_updated_query, job_config=job_config).result())[0]

    return {
        "period": period,
        "admissions": admissions,
        "terminations": terminations,
        "net_balance": admissions - terminations,
        "last_updated": last_updated_row["last_updated"].isoformat() if last_updated_row["last_updated"] else None,
    }


@app.get("/national/by-state")
def by_state(period: str = Query(..., description="YYYYMM, e.g. 202509")):
    ref_date = period_to_ref_date(period)
    client = get_client()

    query = f"""
        SELECT
          l.state,
          l.region,
          COUNTIF(f.event_type = 'admission') AS admissions,
          COUNTIF(f.event_type = 'termination') AS terminations
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        JOIN `{DATASET}.dim_location` l ON f.location_id = l.location_id
        WHERE t.ref_date = @ref_date
        GROUP BY l.state, l.region
        ORDER BY (admissions - terminations) DESC
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("ref_date", "DATE", ref_date)]
    )
    results = [
        {
            "state": row["state"],
            "region": row["region"],
            "admissions": row["admissions"],
            "terminations": row["terminations"],
            "net_balance": row["admissions"] - row["terminations"],
        }
        for row in client.query(query, job_config=job_config).result()
    ]

    if not results:
        raise HTTPException(status_code=404, detail=f"No data loaded for period {period}.")

    return {"period": period, "states": results}


@app.get("/occupations/top")
def top_occupations(
    period: str = Query(..., description="YYYYMM, e.g. 202509"),
    limit: int = Query(20, ge=1, le=200),
):
    ref_date = period_to_ref_date(period)
    client = get_client()

    query = f"""
        SELECT
          o.cbo_code,
          o.display_name,
          o.reviewed,
          COUNTIF(f.event_type = 'admission') AS admissions,
          COUNTIF(f.event_type = 'termination') AS terminations
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        JOIN `{DATASET}.dim_occupation` o ON f.occupation_id = o.occupation_id
        WHERE t.ref_date = @ref_date
        GROUP BY o.cbo_code, o.display_name, o.reviewed
        ORDER BY admissions DESC
        LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("ref_date", "DATE", ref_date),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    results = [
        {
            "cbo_code": row["cbo_code"],
            "display_name": row["display_name"],
            "reviewed": row["reviewed"],
            "admissions": row["admissions"],
            "terminations": row["terminations"],
            "net_balance": row["admissions"] - row["terminations"],
        }
        for row in client.query(query, job_config=job_config).result()
    ]

    if not results:
        raise HTTPException(status_code=404, detail=f"No data loaded for period {period}.")

    return {"period": period, "occupations": results}


@app.get("/demographics")
def demographics(period: str = Query(..., description="YYYYMM, e.g. 202509")):
    ref_date = period_to_ref_date(period)
    client = get_client()

    gender_query = f"""
        SELECT gender, COUNT(*) AS total
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        WHERE t.ref_date = @ref_date AND f.event_type = 'admission'
        GROUP BY gender
    """
    education_query = f"""
        SELECT education_level, COUNT(*) AS total
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        WHERE t.ref_date = @ref_date AND f.event_type = 'admission'
        GROUP BY education_level
    """
    avg_age_query = f"""
        SELECT AVG(age) AS avg_age
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        WHERE t.ref_date = @ref_date AND f.event_type = 'admission'
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("ref_date", "DATE", ref_date)]
    )

    gender = {row["gender"]: row["total"] for row in client.query(gender_query, job_config=job_config).result()}
    education = {
        row["education_level"]: row["total"]
        for row in client.query(education_query, job_config=job_config).result()
    }
    avg_age_row = list(client.query(avg_age_query, job_config=job_config).result())[0]

    if not gender:
        raise HTTPException(status_code=404, detail=f"No data loaded for period {period}.")

    return {
        "period": period,
        "gender": gender,
        "education_level": education,
        "average_age": round(avg_age_row["avg_age"], 1) if avg_age_row["avg_age"] else None,
        "note": (
            "gender and education_level keys are raw source codes, not yet "
            "confirmed against the official Novo CAGED code dictionary — "
            "see docs/DATA_SPEC.md §11. Do not display as translated "
            "labels (e.g. 'Masculino'/'Feminino') until confirmed."
        ),
    }