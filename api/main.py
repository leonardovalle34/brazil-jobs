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

AGE_BUCKETS = [
    ("under_18", None, 17),
    ("18_24", 18, 24),
    ("25_29", 25, 29),
    ("30_39", 30, 39),
    ("40_49", 40, 49),
    ("50_64", 50, 64),
    ("65_plus", 65, None),
]

SALARY_GROUPINGS = {
    "state": ("l.state", "dim_location", "location_id"),
    "occupation": ("o.display_name", "dim_occupation", "occupation_id"),
}

# CORS: open for now (no dashboard domain exists yet to restrict to).
# TODO before real deploy of the dashboard: replace "*" with the actual
# dashboard origin(s) — see docs/TECH_SPEC.md.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def period_range_to_ref_dates(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    """
    Pure function: validate an optional YYYYMM..YYYYMM range and convert
    it to ref_date bounds. Either bound may be omitted (open range).
 
    An inverted range (start after end) is rejected as a 400 rather than
    quietly returning an empty series, which on a chart is
    indistinguishable from "no data was loaded for those months".
    """
    start_date = period_to_ref_date(start) if start else None
    end_date = period_to_ref_date(end) if end else None
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail=f"start {start!r} is after end {end!r}.")
    return start_date, end_date

def age_bucket_case_sql(column: str) -> str:
    """Pure function: build the CASE expression that maps an age column onto AGE_BUCKETS."""
    whens = []
    for label, low, high in AGE_BUCKETS:
        if low is None:
            condition = f"{column} <= {high}"
        elif high is None:
            condition = f"{column} >= {low}"
        else:
            condition = f"{column} BETWEEN {low} AND {high}"
        whens.append(f"WHEN {condition} THEN '{label}'")
    return "CASE " + " ".join(whens) + " ELSE 'unknown' END"

def quartiles_from_approx(quantiles: list | None) -> dict:
    """
    Pure function: turn BigQuery's APPROX_QUANTILES(salary, 4) output —
    five boundary values (min, p25, median, p75, max) — into a dict.
 
    Returns nulls instead of raising when the group had no salary rows,
    so one salary-less state cannot fail the whole response.
    """
    if not quantiles or len(quantiles) != 5:
        return {"min": None, "p25": None, "median": None, "p75": None, "max": None}
    values = [float(q) for q in quantiles]
    return {
        "min": values[0],
        "p25": values[1],
        "median": values[2],
        "p75": values[3],
        "max": values[4],
    }
 

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

@app.get("/national/timeseries")
def national_timeseries(
    start: str | None = Query(None, description="YYYYMM, inclusive. Omit for the earliest loaded month."),
    end: str | None = Query(None, description="YYYYMM, inclusive. Omit for the latest loaded month."),
):
    """
    Monthly admissions/terminations/net balance across every loaded period.
 
    Only months actually present in fact_jobs are returned — gaps are NOT
    filled with zeros. The pipeline has been run for non-consecutive
    months (see README "Status"), and a zero-filled gap month would read
    on a line chart as "the labour market stopped", not "not loaded yet".
    """
    start_date, end_date = period_range_to_ref_dates(start, end)
    client = get_client()
 
    query = f"""
        SELECT
          t.ref_date,
          COUNTIF(f.event_type = 'admission') AS admissions,
          COUNTIF(f.event_type = 'termination') AS terminations,
          MAX(f.load_timestamp) AS last_updated
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        WHERE (@start_date IS NULL OR t.ref_date >= @start_date)
          AND (@end_date IS NULL OR t.ref_date <= @end_date)
        GROUP BY t.ref_date
        ORDER BY t.ref_date
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
        ]
    )
 
    months = []
    last_updated = None
    for row in client.query(query, job_config=job_config).result():
        months.append(
            {
                "period": row["ref_date"].strftime("%Y%m"),
                "ref_date": row["ref_date"].isoformat(),
                "admissions": row["admissions"],
                "terminations": row["terminations"],
                "net_balance": row["admissions"] - row["terminations"],
            }
        )
        if row["last_updated"] and (last_updated is None or row["last_updated"] > last_updated):
            last_updated = row["last_updated"]
 
    if not months:
        raise HTTPException(status_code=404, detail="No data loaded for the requested range.")
 
    return {
        "start": months[0]["period"],
        "end": months[-1]["period"],
        "months": months,
        "last_updated": last_updated.isoformat() if last_updated else None,
    }
 
 
@app.get("/national/by-region")
def by_region(period: str = Query(..., description="YYYYMM, e.g. 202509")):
    ref_date = period_to_ref_date(period)
    client = get_client()
 
    query = f"""
        SELECT
          l.region,
          COUNTIF(f.event_type = 'admission') AS admissions,
          COUNTIF(f.event_type = 'termination') AS terminations,
          MAX(f.load_timestamp) AS last_updated
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        JOIN `{DATASET}.dim_location` l ON f.location_id = l.location_id
        WHERE t.ref_date = @ref_date
        GROUP BY l.region
        ORDER BY (admissions - terminations) DESC
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("ref_date", "DATE", ref_date)]
    )
 
    regions = []
    last_updated = None
    for row in client.query(query, job_config=job_config).result():
        regions.append(
            {
                "region": row["region"],
                "admissions": row["admissions"],
                "terminations": row["terminations"],
                "net_balance": row["admissions"] - row["terminations"],
            }
        )
        if row["last_updated"] and (last_updated is None or row["last_updated"] > last_updated):
            last_updated = row["last_updated"]
 
    if not regions:
        raise HTTPException(status_code=404, detail=f"No data loaded for period {period}.")
 
    return {
        "period": period,
        "regions": regions,
        "last_updated": last_updated.isoformat() if last_updated else None,
    } 
 
@app.get("/salary/distribution")
def salary_distribution(
    period: str = Query(..., description="YYYYMM, e.g. 202509"),
    group_by: str = Query("state", description="state | occupation"),
    limit: int = Query(20, ge=1, le=200),
):
    """
    Admission salary distribution (min/p25/median/p75/max) per state or
    occupation, using APPROX_QUANTILES — exact percentiles over the full
    national fact table would be a sort over tens of millions of rows.
 
    Only admissions are considered: a termination's salary is the wage
    the job paid on the way out, which is a different question from what
    the market currently pays to hire. Rows with a null or zero salary
    are excluded — CAGED uses those for records where pay is not
    reported, and averaging them in drags every figure toward zero.
 
    Values are nominal BRL for the requested month, not inflation
    adjusted, so comparing two distant months compares nominal wages.
    """
    if group_by not in SALARY_GROUPINGS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid group_by: {group_by!r}. Expected one of {sorted(SALARY_GROUPINGS)}.",
        )
 
    ref_date = period_to_ref_date(period)
    label_column, dim_table, dim_key = SALARY_GROUPINGS[group_by]
    dim_alias = "l" if group_by == "state" else "o"
    client = get_client()
 
    query = f"""
        SELECT
          {label_column} AS label,
          COUNT(*) AS admissions_with_salary,
          APPROX_QUANTILES(f.salary, 4) AS quantiles,
          MAX(f.load_timestamp) AS last_updated
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        JOIN `{DATASET}.{dim_table}` {dim_alias} ON f.{dim_key} = {dim_alias}.{dim_key}
        WHERE t.ref_date = @ref_date
          AND f.event_type = 'admission'
          AND f.salary IS NOT NULL
          AND f.salary > 0
        GROUP BY label
        ORDER BY admissions_with_salary DESC
        LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("ref_date", "DATE", ref_date),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
 
    groups = []
    last_updated = None
    for row in client.query(query, job_config=job_config).result():
        groups.append(
            {
                "label": row["label"],
                "admissions_with_salary": row["admissions_with_salary"],
                **quartiles_from_approx(row["quantiles"]),
            }
        )
        if row["last_updated"] and (last_updated is None or row["last_updated"] > last_updated):
            last_updated = row["last_updated"]
 
    if not groups:
        raise HTTPException(status_code=404, detail=f"No salary data loaded for period {period}.")
 
    return {
        "period": period,
        "group_by": group_by,
        "currency": "BRL",
        "groups": groups,
        "last_updated": last_updated.isoformat() if last_updated else None,
        "note": "Nominal monthly salary at admission, not inflation adjusted.",
    }
 
 
@app.get("/demographics/age")
def demographics_age(period: str = Query(..., description="YYYYMM, e.g. 202509")):
    """
    Age profile of admissions, bucketed per AGE_BUCKETS.
 
    Buckets are built here rather than stored: the source has no age
    bracket column, only raw integer age (see ingestion/caged_ingest.py).
    """
    ref_date = period_to_ref_date(period)
    client = get_client()
 
    query = f"""
        SELECT
          {age_bucket_case_sql("f.age")} AS bucket,
          COUNT(*) AS admissions,
          MAX(f.load_timestamp) AS last_updated
        FROM `{DATASET}.fact_jobs` f
        JOIN `{DATASET}.dim_time` t ON f.time_id = t.time_id
        WHERE t.ref_date = @ref_date AND f.event_type = 'admission'
        GROUP BY bucket
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("ref_date", "DATE", ref_date)]
    )
 
    counts = {}
    last_updated = None
    for row in client.query(query, job_config=job_config).result():
        counts[row["bucket"]] = row["admissions"]
        if row["last_updated"] and (last_updated is None or row["last_updated"] > last_updated):
            last_updated = row["last_updated"]
 
    if not counts:
        raise HTTPException(status_code=404, detail=f"No data loaded for period {period}.")
 
    buckets = [{"bucket": label, "admissions": counts.get(label, 0)} for label, _, _ in AGE_BUCKETS]
    buckets.append({"bucket": "unknown", "admissions": counts.get("unknown", 0)})
 
    return {
        "period": period,
        "buckets": buckets,
        "last_updated": last_updated.isoformat() if last_updated else None,
    }
 