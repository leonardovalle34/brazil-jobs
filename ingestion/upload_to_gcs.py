"""
Upload processed CAGED parquet files to the data lake (Google Cloud
Storage), before they're loaded into the BigQuery warehouse.

Architecture reminder (see docs/TECH_SPEC.md §1):
  [CAGED .7z, monthly] --- raw (parquet, GCS) --- transform --- BigQuery

GCS is the "raw, immutable" layer — see docs/TECH_SPEC.md §5, "Raw layer
is immutable." Re-running load_to_bigquery.py should always be possible
from what's in GCS, without re-downloading from CAGED's FTP server.

Requires a GCS_BUCKET_NAME environment variable (see .env) and GCP
credentials available to the environment (GOOGLE_APPLICATION_CREDENTIALS).

Usage:
    python upload_to_gcs.py --period 202507
"""

import argparse
import os
from dotenv import load_dotenv
from google.cloud import storage

load_dotenv(override=True)

def build_gcs_path(period: str, filename: str) -> str:
    """
    Compute the destination blob path inside the bucket for a given
    period's file. Pure function — no network call.
    """

    year = period[:4]
    return f"raw/caged/{year}/{period}/{filename}"

def parse_gcs_uri(gcs_uri: str) ->tuple:
    """
    Pure function: split a gs://bucket/path/to/file URI into
    (bucket_name, blob_path). Needed by download_from_gcs to reconstruct
    where to fetch from, since Cloud Run steps only pass the URI string
    between them — see the Cloud Run statelessness note in
    docs/TECH_SPEC.md §1.
    """
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"Not a gs://URI: {gcs_uri!r}")
    without_scheme = gcs_uri[len("gs://"):]
    bucket_name, _, blob_path = without_scheme.partition("/")
    if not blob_path:
        raise ValueError(f"URI has no object path: {gcs_uri!r}")
    return bucket_name, blob_path

def download_from_gcs(gcs_uri: str, local_path: str) -> str:
    """
    Download a file from the data lake bucket to local disk, return the
    local path. The mirror image of upload_to_gcs() — this is what lets
    one Cloud Run step's output become the next step's input, since
    Cloud Run instances don't share local disk between separate HTTP
    calls (see docs/TECH_SPEC.md §1).
    """
    bucket_name, blob_path = parse_gcs_uri(gcs_uri)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.download_to_filename(local_path)

    print(f"[download] {gcs_uri} -> {local_path}")
    return local_path

def upload_to_gcs(local_path: str, period: str, bucket_name: str = None)  -> str:
    """
    Upload a local file to the data lake bucket, return its gs:// URI.
    """
    bucket_name = bucket_name or os.environ["GCS_BUCKET_NAME"]
    filename = os.path.basename(local_path)
    blob_path = build_gcs_path(period, filename)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(local_path)

    gcs_uri = f"gs://{bucket_name}/{blob_path}"
    print(f"[upload] {local_path} -> {gcs_uri}")
    return gcs_uri

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", required=True, help="YYYYMM format, e.g. 202508")
    args = parser.parse_args()

    raw_dir = os.path.join(os.path.dirname(__file__), "..", "raw")
    local_path = os.path.join(raw_dir, f"fact_caged_{args.period}.parquet")

    if not os.path.exists(local_path):
        raise FileNotFoundError(
            f"{local_path} not found — run caged_ingest.py for this period first."
        )

    gcs_uri = upload_to_gcs(local_path, args.period)
    print(f"[ok] {gcs_uri}")


if __name__ == "__main__":
    main()
