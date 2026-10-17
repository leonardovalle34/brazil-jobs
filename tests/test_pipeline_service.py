"""
Tests for the pipeline service (app.py): GET /next-period, the "no_file"
404 from /download, and the 409 from /load.

Endpoints are called directly (no HTTP test client), as in test_api.py.
Both I/O boundaries are faked per docs/VALIDATION_SPEC.md §1: BigQuery
by a fake client returning rows, the MTE FTP by a fake ftplib.FTP that
knows which archives are "published".
"""

import ftplib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))

import pytest

import app
import caged_ingest
from caged_ingest import current_period, iter_periods, missing_periods, remote_path_for
from load_to_bigquery import PeriodAlreadyLoadedError, get_loaded_periods

TODAY = "202610"  # "now" for every /next-period test


class FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeBigQuery:
    def __init__(self, loaded_periods):
        self._rows = [{"period": p} for p in sorted(loaded_periods)]
        self.queries = []

    def query(self, query, job_config=None):
        self.queries.append(query)
        return FakeQueryJob(self._rows)


class FakeFTP:
    """
    Answers SIZE for published archives and 550 for everything else.
    No retrbinary unless `retr_error` is given, so a test fails loudly if
    /next-period ever tries to download instead of just checking.
    """

    def __init__(self, published=(), size_error=None, retr_error=None):
        self.published = {remote_path_for(p) for p in published}
        self.size_error = size_error
        self.retr_error = retr_error
        self.sized = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def voidcmd(self, cmd):
        return "200 OK"

    def size(self, path):
        self.sized.append(path)
        if self.size_error:
            raise self.size_error
        if path not in self.published:
            raise ftplib.error_perm(f"550 {path}: No such file or directory")
        return 123_456_789

    def retrbinary(self, cmd, callback):
        if self.retr_error:
            callback(b"partial bytes")
            raise self.retr_error
        raise AssertionError("unexpected download")


def months(start, end):
    return set(iter_periods(start, end))


def body_of(response):
    return json.loads(response.body)


@pytest.fixture
def service(monkeypatch):
    """Wire fake BigQuery + FTP into app; returns a configurator."""

    def configure(loaded, published, size_error=None):
        bq = FakeBigQuery(loaded)
        ftp = FakeFTP(published, size_error=size_error)
        monkeypatch.setattr(app, "get_bigquery_client", lambda: bq)
        monkeypatch.setattr(app, "open_ftp", lambda: ftp)
        monkeypatch.setattr(app, "current_period", lambda: TODAY)
        return bq, ftp

    return configure


# --- period helpers ------------------------------------------------------

def test_iter_periods_crosses_year_boundary():
    assert iter_periods("202511", "202602") == ["202511", "202512", "202601", "202602"]


def test_missing_periods_keeps_order_and_skips_loaded():
    loaded = {"202506", "202508"}
    assert missing_periods(loaded, "202506", "202509") == ["202507", "202509"]


def test_current_period_uses_sao_paulo_time_not_utc():
    # 02:00 UTC on Nov 1st is still Oct 31st, 23:00 in São Paulo.
    now = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    assert current_period(now) == "202610"


def test_get_loaded_periods_returns_yyyymm_set():
    bq = FakeBigQuery({"202506", "202608"})
    assert get_loaded_periods(bq) == {"202506", "202608"}
    assert "fact_jobs" in bq.queries[0]
    assert "FORMAT_DATE('%Y%m', ref_date)" in bq.queries[0]


# --- GET /next-period ----------------------------------------------------

def test_next_period_skips_loaded_months(service):
    # Real state on 2026-10-15: 2025-06..09 and 2026-08 loaded.
    service(
        loaded=months("202506", "202509") | {"202608"},
        published=months("202506", "202608"),
    )
    assert app.next_period() == {"status": "ok", "period": "202510"}


def test_next_period_returns_newly_published_month(service):
    service(loaded=months("202506", "202608"), published=months("202506", "202609"))
    assert app.next_period() == {"status": "ok", "period": "202609"}


def test_next_period_returns_no_file_when_nothing_new_is_published(service):
    _, ftp = service(loaded=months("202506", "202608"), published=months("202506", "202608"))

    assert app.next_period() == {
        "status": "no_file",
        "period": None,
        "message": "Nenhum mês novo publicado no CAGED",
    }
    # Only the unloaded months were checked on the FTP — loaded ones never are.
    assert ftp.sized == [remote_path_for("202609"), remote_path_for("202610")]


def test_next_period_fills_a_gap_in_the_middle_first(service):
    service(
        loaded=months("202506", "202608") - {"202512"},
        published=months("202506", "202609"),
    )
    assert app.next_period() == {"status": "ok", "period": "202512"}


def test_next_period_skips_a_gap_that_is_not_on_the_ftp(service):
    service(
        loaded=months("202506", "202608") - {"202512"},
        published=months("202506", "202609") - {"202512"},
    )
    assert app.next_period() == {"status": "ok", "period": "202609"}


def test_next_period_never_looks_before_2025_06(service):
    _, ftp = service(loaded=months("202506", "202610"), published=months("202401", "202610"))
    assert app.next_period()["status"] == "no_file"
    assert ftp.sized == []


def test_next_period_real_ftp_error_is_a_500_not_no_file(service):
    service(
        loaded=months("202506", "202608"),
        published=(),
        size_error=ftplib.error_temp("421 Service not available"),
    )
    response = app.next_period()
    assert response.status_code == 500
    assert body_of(response)["status"] == "error"
    assert "421" in body_of(response)["message"]


# --- POST /download ------------------------------------------------------

@pytest.fixture
def ftp_download(monkeypatch, tmp_path):
    def configure(retr_error):
        monkeypatch.setattr(caged_ingest, "RAW_DIR", str(tmp_path))
        monkeypatch.setattr(caged_ingest, "open_ftp", lambda: FakeFTP(retr_error=retr_error))
        return tmp_path

    return configure


def test_download_of_unpublished_period_is_404_no_file(ftp_download):
    raw_dir = ftp_download(ftplib.error_perm("550 CAGEDMOV202610.7z: No such file or directory"))

    response = app.download(app.DownloadRequest(period="202610"))

    assert response.status_code == 404
    assert body_of(response) == {
        "status": "no_file",
        "period": "202610",
        "message": "Arquivo do CAGED para 202610 ainda não foi publicado",
    }
    # No empty/partial archive left behind for a warm instance to "reuse".
    assert os.listdir(raw_dir) == []


def test_download_network_error_is_still_a_descriptive_500(ftp_download):
    raw_dir = ftp_download(ConnectionResetError("Connection reset by peer"))

    response = app.download(app.DownloadRequest(period="202609"))

    assert response.status_code == 500
    body = body_of(response)
    assert body["status"] == "error"
    assert body["period"] == "202609"
    assert "ConnectionResetError" in body["message"]
    assert os.listdir(raw_dir) == []


def test_download_permission_error_other_than_550_is_a_500(ftp_download):
    ftp_download(ftplib.error_perm("530 Login incorrect"))
    response = app.download(app.DownloadRequest(period="202609"))
    assert response.status_code == 500
    assert "530" in body_of(response)["message"]


# --- POST /load ----------------------------------------------------------

def test_load_of_already_loaded_period_is_409_and_not_reinserted(monkeypatch):
    def refuse(client, path):
        raise PeriodAlreadyLoadedError("Period 2025-07-01 already has 4373105 rows in fact_jobs")

    monkeypatch.setattr(app, "download_from_gcs", lambda uri, path: path)
    monkeypatch.setattr(app, "get_bigquery_client", lambda: object())
    monkeypatch.setattr(app, "load_period", refuse)

    response = app.load(app.LoadRequest(period="202507", fact_path="gs://b/x.parquet"))

    assert response.status_code == 409
    assert body_of(response)["status"] == "already_loaded"
