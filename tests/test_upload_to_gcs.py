import sys
import os
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))

from upload_to_gcs import build_gcs_path, upload_to_gcs, parse_gcs_uri, download_from_gcs


# --- Pure function: no mock needed, no network involved ---

def test_build_gcs_path_uses_year_and_period_subfolders():
    path = build_gcs_path("202507", "fact_caged_202507.parquet")
    assert path == "raw/caged/2025/202507/fact_caged_202507.parquet"


def test_build_gcs_path_preserves_filename_exactly():
    path = build_gcs_path("202608", "some_other_file.parquet")
    assert path.endswith("some_other_file.parquet")


# --- I/O boundary: mocked, per docs/VALIDATION_SPEC.md §1 ---

@patch("upload_to_gcs.storage.Client")
def test_upload_to_gcs_calls_blob_upload_with_correct_path(mock_client_class):
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob

    result = upload_to_gcs("/tmp/fact_caged_202507.parquet", "202507", bucket_name="my-test-bucket")

    mock_client.bucket.assert_called_once_with("my-test-bucket")
    mock_bucket.blob.assert_called_once_with("raw/caged/2025/202507/fact_caged_202507.parquet")
    mock_blob.upload_from_filename.assert_called_once_with("/tmp/fact_caged_202507.parquet")


@patch("upload_to_gcs.storage.Client")
def test_upload_to_gcs_returns_gs_uri(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.bucket.return_value.blob.return_value = MagicMock()

    result = upload_to_gcs("/tmp/fact_caged_202507.parquet", "202507", bucket_name="my-test-bucket")

    assert result == "gs://my-test-bucket/raw/caged/2025/202507/fact_caged_202507.parquet"


@patch("upload_to_gcs.storage.Client")
def test_upload_to_gcs_never_touches_real_network(mock_client_class):
    upload_to_gcs("/tmp/fact_caged_202507.parquet", "202507", bucket_name="my-test-bucket")
    assert mock_client_class.called

def test_parse_gcs_uri_splits_bucket_and_path():
    bucket, blob_path = parse_gcs_uri("gs://jobs-bi-brazil-uploader/raw/caged/2025/202507/fact_caged_202507.parquet")
    assert bucket == "jobs-bi-brazil-uploader"
    assert blob_path == "raw/caged/2025/202507/fact_caged_202507.parquet"


def test_parse_gcs_uri_rejects_non_gs_uri():
    with pytest.raises(ValueError):
        parse_gcs_uri("https://example.com/file.parquet")


def test_parse_gcs_uri_rejects_bucket_with_no_object_path():
    with pytest.raises(ValueError):
        parse_gcs_uri("gs://just-a-bucket")


@patch("upload_to_gcs.storage.Client")
def test_download_from_gcs_calls_blob_download_with_correct_path(mock_client_class):
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob

    result = download_from_gcs(
        "gs://jobs-bi-brazil-uploader/raw/caged/2025/202507/fact_caged_202507.parquet",
        "/tmp/fact_caged_202507.parquet",
    )

    mock_client.bucket.assert_called_once_with("jobs-bi-brazil-uploader")
    mock_bucket.blob.assert_called_once_with("raw/caged/2025/202507/fact_caged_202507.parquet")
    mock_blob.download_to_filename.assert_called_once_with("/tmp/fact_caged_202507.parquet")
    assert result == "/tmp/fact_caged_202507.parquet"