# Tech spec — jobs_bi_brazil

## 1. Architecture overview

```
[CAGED .7z, monthly] --- raw (parquet, GCS) --- transform --- BigQuery (star schema) --- API (FastAPI) --- Vue 3 + ApexCharts
                                                                      ^
                                                        Cloud Scheduler (cron)
                                                                      |
                                                                      v
                                                             Cloud Workflows
                                                       (orchestrates: download step
                                                        -> transform step -> load step,
                                                        each as an HTTP call to a
                                                        Cloud Run job)
```

Single source, single monthly workflow. Cloud Workflows itself is a thin
orchestration layer (YAML, HTTP calls) — the actual Python ingestion logic
in `ingestion/` runs inside a Cloud Run job that each workflow step calls.
This mirrors the owner's existing production setup (Cloud Workflows +
Cloud Scheduler at RIA Catalyst), which is the deliberate reason for this
choice over Airflow — see docs/DATA_SPEC.md §1 for the source history.

## 2. Stack

| Layer | Choice | Why |
|---|---|---|
| Ingestion | Python (pandas, requests, py7zr), packaged as a Cloud Run job | Matches the project owner's existing production stack |
| Orchestration | Google Cloud Workflows + Cloud Scheduler | Same tools used in the owner's day job at RIA Catalyst — reinforces transferable, resume-relevant skill. Stays inside GCP's free tier at this project's volume (one execution/month, well under the 5,000 free steps/month) |
| Storage | BigQuery | Same tool used in the owner's day job; free tier covers 10GB storage + 1TB queries/month, comfortably enough for this project's scope |
| Raw file staging | Google Cloud Storage | Free tier (5GB) covers the monthly `.7z`/parquet staging needs |
| API | FastAPI, deployed on Cloud Run | Thin aggregation layer over BigQuery; the frontend must never query BigQuery directly. Cloud Run free tier covers low-traffic portfolio use |
| Frontend | Vue 3 + TypeScript + ApexCharts, deployed on Vercel | Matches the project owner's existing frontend stack (HireUp), free tier |
| Testing | pytest | See VALIDATION_SPEC.md for TDD workflow |

**Cost note:** every service above has a free tier that comfortably
covers this project's actual usage (one CAGED pull/month, low-traffic
dashboard). GCP services require a billing account with a card on file
to be enabled at all, even for free-tier usage — that's a one-time setup
requirement, not a recurring cost, and no step in this pipeline is
expected to cross into billed usage. If actual usage ever approaches a
free-tier limit, that should be treated as a signal to revisit this
table, not silently absorbed.

## 3. Repository layout

```
jobs_bi_brazil/
  docs/            PRD.md, DATA_SPEC.md, TECH_SPEC.md, VALIDATION_SPEC.md
  sql/             schema.sql (BigQuery DDL)
  ingestion/        caged_ingest.py, load_to_bigquery.py (pending)
  workflows/       caged_monthly.yaml (Cloud Workflows definition)
  dashboard/       Vue app (pending)
  tests/           pytest suite, mirrors ingestion/ structure
  requirements.txt
```

## 4. Volume and its consequences

Per DATA_SPEC.md §6, dropping the tech-only CBO filter means the pipeline
now processes the full national dataset. Consequences:

- The `.7z` monthly archive and its extracted contents are significantly
  larger — download and extraction steps need streaming/chunked handling
  rather than loading the whole file into memory at once, and staging
  through Cloud Storage (rather than a Cloud Run job's ephemeral local
  disk) is the safer default.
- BigQuery's free tier (10GB storage, 1TB queries/month) needs to be
  monitored against real monthly volume once the pipeline runs against
  actual data — see docs/VALIDATION_SPEC.md for the row-count sanity
  check that would catch a load blowing past expectations.
- `dim_occupation` needs to be populated for every CBO code encountered,
  not a hand-picked list of five — this is a larger, more mechanical
  seeding task than the earlier tech-only draft required, though the
  `display_name` governance rule (DATA_SPEC.md §3) still applies to
  every row added.

## 5. Pipeline design principles

- **Raw layer is immutable.** Downloaded CAGED archives and their
  extracted contents are never mutated in place; transformations always
  read raw and write to a new artifact (parquet), so a bug in a
  transform can be fixed and re-run without re-downloading.
- **Every fact row is traceable.** `fact_jobs.load_timestamp` records
  when the row entered the warehouse. No fact row is inserted without it.
- **No frontend-to-database access.** The Vue app only ever calls the
  FastAPI layer, which runs on Cloud Run with its own service account —
  BigQuery credentials never ship to the frontend build.

## 6. API design (draft — to be implemented)

- `GET /national/summary?period=` — headline net balance number
- `GET /national/by-sector?period=` — net balance by CNAE sector
- `GET /national/by-state?period=` — net balance by state
- `GET /occupations/top?period=` — top occupations by net hiring balance
- `GET /occupations/salary?occupation=&period=&state=` — average salary
- `GET /demographics?period=` — hiring profile by gender, age, education
- `GET /contracts/type-split?period=` — typical vs. non-typical split

Each endpoint's response includes a `last_updated` field, per
DATA_SPEC.md §5.
