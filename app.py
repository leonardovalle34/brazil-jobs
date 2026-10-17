"""
Cloud Run service wrapping the CAGED pipeline as HTTP endpoints, called
by workflows/caged_monthly.yaml:

  GET  /next-period  which month to load next (or "no_file")
  POST /download     FTP -> GCS; 404 {"status": "no_file"} if not published
  POST /transform    GCS raw -> GCS fact parquet
  POST /load         GCS fact parquet -> BigQuery; 409 if already loaded

Statelessness note (see docs/TECH_SPEC.md §1): each endpoint call may
land on a different container instance, so intermediate results are
always passed through GCS (gs:// URIs) between steps, never through a
local disk path assumed to still exist from a previous call.
"""

import os
import sys
import tempfile
import traceback

import pandas as pd
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from google.cloud import bigquery

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "ingestion"))

from caged_ingest import (
    FIRST_PERIOD,
    PeriodNotPublishedError,
    current_period,
    download_period,
    extract,
    find_next_period,
    open_ftp,
    to_fact_format,
)
from upload_to_gcs import upload_to_gcs, download_from_gcs
from load_to_bigquery import PeriodAlreadyLoadedError, get_loaded_periods, load_period

app = FastAPI()

NOTHING_NEW_MESSAGE = "Nenhum mês novo publicado no CAGED"


def get_bigquery_client() -> bigquery.Client:
    """I/O boundary — replaced by a fake in tests."""
    return bigquery.Client()


def error_response(step: str, error: Exception, period: str = None) -> JSONResponse:
    """A real failure (network, permission, disk...): 500 with a readable message."""
    traceback.print_exc()
    return JSONResponse(status_code=500, content={
        "status": "error",
        "period": period,
        "message": f"{step} falhou: {type(error).__name__}: {error}",
    })


class DownloadRequest(BaseModel):
    period: str


class TransformRequest(BaseModel):
    period: str
    raw_path: str


class LoadRequest(BaseModel):
    period: str
    fact_path: str


@app.get("/")
def health():
    """Simple health check — useful to confirm the service deployed correctly."""
    return {"status": "ok", "service": "jobs_bi_brazil-pipeline"}


@app.get("/next-period")
def next_period():
    """
    Oldest month from FIRST_PERIOD up to the current month that is not in
    fact_jobs yet AND is already published on the FTP (checked with SIZE,
    without downloading). Always 200: "no_file" is a normal outcome — the
    MTE publishes each month only near the end of the following one.
    """
    try:
        loaded = get_loaded_periods(get_bigquery_client())
        with open_ftp() as ftp:
            period = find_next_period(ftp, loaded, FIRST_PERIOD, current_period())
    except Exception as e:
        return error_response("next-period", e)

    if period is None:
        print(f"[next-period] {NOTHING_NEW_MESSAGE}")
        return {"status": "no_file", "period": None, "message": NOTHING_NEW_MESSAGE}
    print(f"[next-period] next period to load: {period}")
    return {"status": "ok", "period": period}


@app.post("/download")
def download(req: DownloadRequest):
    """Download from CAGED's FTP, upload the raw extracted data to GCS."""
    try:
        archive_path = download_period(req.period)
        df_raw = extract(archive_path, req.period)

        local_raw_parquet = os.path.join(tempfile.gettempdir(), f"raw_caged_{req.period}.parquet")
        df_raw.to_parquet(local_raw_parquet, index=False)

        gcs_uri = upload_to_gcs(local_raw_parquet, req.period)
    except PeriodNotPublishedError as e:
        print(f"[download] {e}")
        return JSONResponse(status_code=404, content={
            "status": "no_file",
            "period": req.period,
            "message": str(e),
        })
    except Exception as e:
        return error_response("download", e, req.period)
    return {"status": "ok", "raw_path": gcs_uri}


@app.post("/transform")
def transform(req: TransformRequest):
    """Pull the raw data back from GCS, transform it, push the result back to GCS."""
    local_raw_parquet = os.path.join(tempfile.gettempdir(), f"raw_caged_{req.period}.parquet")
    download_from_gcs(req.raw_path, local_raw_parquet)

    df_raw = pd.read_parquet(local_raw_parquet)
    df_fact = to_fact_format(df_raw, req.period)

    local_fact_parquet = os.path.join(tempfile.gettempdir(), f"fact_caged_{req.period}.parquet")
    df_fact.to_parquet(local_fact_parquet, index=False)

    gcs_uri = upload_to_gcs(local_fact_parquet, req.period)
    return {"status": "ok", "fact_path": gcs_uri}


@app.post("/load")
def load(req: LoadRequest):
    """Pull the transformed data back from GCS, load it into BigQuery."""
    local_fact_parquet = os.path.join(tempfile.gettempdir(), f"fact_caged_{req.period}.parquet")
    download_from_gcs(req.fact_path, local_fact_parquet)

    try:
        rows_loaded = load_period(get_bigquery_client(), local_fact_parquet)
    except PeriodAlreadyLoadedError as e:
        # Nothing was written: the guard runs before any insert.
        return JSONResponse(status_code=409, content={
            "status": "already_loaded",
            "period": req.period,
            "message": str(e),
        })
    return {"status": "ok", "rows_loaded": rows_loaded}