"""
Cloud Run service wrapping the CAGED pipeline as three HTTP endpoints,
called in sequence by workflows/caged_monthly.yaml.

Statelessness note (see docs/TECH_SPEC.md §1): each endpoint call may
land on a different container instance, so intermediate results are
always passed through GCS (gs:// URIs) between steps, never through a
local disk path assumed to still exist from a previous call.
"""

import os
import sys
import tempfile

import pandas as pd
from fastapi import FastAPI
from pydantic import BaseModel
from google.cloud import bigquery

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "ingestion"))

from caged_ingest import download_period, extract, to_fact_format
from upload_to_gcs import upload_to_gcs, download_from_gcs
from load_to_bigquery import load_period

app = FastAPI()


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


@app.post("/download")
def download(req: DownloadRequest):
    """Download from CAGED's FTP, upload the raw extracted data to GCS."""
    archive_path = download_period(req.period)
    df_raw = extract(archive_path, req.period)

    local_raw_parquet = os.path.join(tempfile.gettempdir(), f"raw_caged_{req.period}.parquet")
    df_raw.to_parquet(local_raw_parquet, index=False)

    gcs_uri = upload_to_gcs(local_raw_parquet, req.period)
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

    client = bigquery.Client()
    rows_loaded = load_period(client, local_fact_parquet)
    return {"status": "ok", "rows_loaded": rows_loaded}