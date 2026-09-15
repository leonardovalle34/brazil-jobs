# jobs_bi_brazil

A data pipeline and BI dashboard covering Brazil's general formal labor
market (CLT — regime celetista), built from a single public source:
CAGED (official hiring/termination records from the Ministry of Labor).

This project intentionally does not focus on any single industry — an
earlier draft scoped a tech-specific screen combining CAGED with Indeed
job postings; that scope was dropped in favor of the general labor
market (see `docs/PRD.md` §3 for why).

Built as a portfolio project demonstrating spec-driven data engineering:
every design decision here was made — and, where it turned out wrong,
corrected — before code was written to match it.

## Documentation

Read in this order before touching code:

1. [`docs/PRD.md`](docs/PRD.md) — what this project is for, and what it
   deliberately does not attempt to do.
2. [`docs/DATA_SPEC.md`](docs/DATA_SPEC.md) — sources, schema, and the
   known data limitations that shape every design decision below.
3. [`docs/TECH_SPEC.md`](docs/TECH_SPEC.md) — architecture and stack.
4. [`docs/VALIDATION_SPEC.md`](docs/VALIDATION_SPEC.md) — testing
   strategy (TDD) and data quality acceptance criteria.

## Architecture

```
[CAGED .7z, monthly] --- raw (parquet, GCS) --- transform --- BigQuery (star schema) --- API (FastAPI) --- Vue 3 + ApexCharts
```

Orchestrated by Cloud Workflows, triggered monthly by Cloud Scheduler —
see `workflows/caged_monthly.yaml`. All services run inside their GCP
free tier at this project's volume (see `docs/TECH_SPEC.md` §2 for the
cost breakdown).

## Running locally

```bash
pip install -r requirements.txt
pytest tests/ -v                              # run the test suite first
python ingestion/caged_ingest.py --period 202508   # runs against local raw/ output,
                                                     # independent of GCP for this step
```

Loading into BigQuery and deploying the Cloud Workflows definition both
require a GCP project with billing enabled (free tier — see
`docs/TECH_SPEC.md` §2) — not required just to run the ingestion script
and test suite locally.

## Status

End-to-end pipeline is live and automated: CAGED FTP -> GCS (data lake) ->
BigQuery (star schema), orchestrated by Cloud Workflows and triggered
monthly by Cloud Scheduler (15th of each month, 06:00 America/Sao_Paulo).
Validated against official government releases for four non-consecutive
months (2025-06, 07, 08, 09) — see `docs/VALIDATION_SPEC.md` for the
full validation log, including every production issue found and fixed
along the way (memory limits, IAM permissions, HTTP call timeouts,
streaming-buffer DML restrictions).

Not yet done: the FastAPI aggregation layer and the Vue dashboard that
will consume it (see `docs/TECH_SPEC.md` §6 for the planned endpoints),
and populating `dim_occupation` for the full set of CBO codes beyond
the auto-seeded placeholders (see `docs/DATA_SPEC.md` §8).

