# Tech spec — jobs_bi_brazil

## 1. Architecture overview

```
[CAGED .7z, monthly] --- raw (parquet, GCS) --- transform --- BigQuery (star schema) --- API (FastAPI) --- Vue 3 + ApexCharts
                                                                      ^
                                                Cloud Scheduler (weekly cron)
                                                                      |
                                                                      v
                                                             Cloud Workflows
                                                       (loop: next-period -> download
                                                        -> transform -> load, until
                                                        "no_file"; each step
                                                        each as an HTTP call to a
                                                        Cloud Run job)
```

Single source, single workflow, run weekly (see §7). Cloud Workflows itself is a thin
orchestration layer (YAML, HTTP calls) — the actual Python ingestion logic
in `ingestion/` runs inside a Cloud Run job that each workflow step calls.
This mirrors the owner's existing production setup (Cloud Workflows +
Cloud Scheduler at RIA Catalyst), which is the deliberate reason for this
choice over Airflow — see docs/DATA_SPEC.md §1 for the source history.

## 2. Stack

| Layer | Choice | Why |
|---|---|---|
| Ingestion | Python (pandas, requests, py7zr), packaged as a Cloud Run job | Matches the project owner's existing production stack |
| Orchestration | Google Cloud Workflows + Cloud Scheduler | Same tools used in the owner's day job at RIA Catalyst — reinforces transferable, resume-relevant skill. Stays inside GCP's free tier at this project's volume (one execution/week — roughly 10 steps on a week with nothing new, ~15 more per month loaded — well under the 5,000 free steps/month) |
| Storage | BigQuery | Same tool used in the owner's day job; free tier covers 10GB storage + 1TB queries/month, comfortably enough for this project's scope |
| Raw file staging | Google Cloud Storage | Free tier (5GB) covers the monthly `.7z`/parquet staging needs |
| API | FastAPI, deployed on Cloud Run | Thin aggregation layer over BigQuery; the frontend must never query BigQuery directly. Cloud Run free tier covers low-traffic portfolio use |
| Frontend | Vue 3 + TypeScript + ApexCharts, deployed on Vercel | Matches the project owner's existing frontend stack (HireUp), free tier |
| Testing | pytest | See VALIDATION_SPEC.md for TDD workflow |

**Cost note:** every service above has a free tier that comfortably
covers this project's actual usage (one CAGED month loaded per month, plus a cheap weekly "anything new?" check, low-traffic
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

## 7. Scheduling and period detection

The MTE publishes each CAGED month only near the end of the following
month, on no fixed day. The original workflow ran on the 15th and always
asked for "the previous month" — the archive did not exist yet, and the
runs of 2026-09-15 and 2026-10-15 failed with a generic HTTP 500.

Current design:

- **Weekly trigger.** Cloud Scheduler job `caged-monthly-trigger`, cron
  `0 6 * * 1` (Mondays, 06:00 America/Sao_Paulo). A new month is picked
  up within a week of publication; most runs find nothing and cost a
  handful of workflow steps plus a few FTP `SIZE` commands.
- **Detection in the service, not the YAML.** `GET /next-period` reads
  the distinct months in `fact_jobs.ref_date`, walks every month from
  2025-06 to the current month (America/Sao_Paulo), and returns the
  OLDEST one that is not loaded and whose archive exists on the FTP —
  checked with `SIZE`, never downloaded. Response:
  `{"status": "ok", "period": "YYYYMM"}` or
  `{"status": "no_file", "period": null, "message": "Nenhum mês novo publicado no CAGED"}`.
  A month missing from the FTP does not block later months.
- **Fill loop.** Without input, the workflow calls `/next-period`, runs
  download -> transform -> load for the answer and repeats until
  `no_file`, so gaps are backfilled oldest-first and the series then
  stays current. Safety limit: 12 months per execution; the next run
  resumes where it stopped. With input `{"period": "YYYYMM"}` only that
  month is processed (manual override).
- **"Nothing new" is success.** The execution ends SUCCEEDED with
  `{"status": "no_file", "message": "...", "loaded": []}`, or
  `{"status": "ok", "loaded": ["202510", ...]}` when it loaded something.
- **Explicit "no_file".** `POST /download` answers HTTP 404
  `{"status": "no_file", "period": "YYYYMM", "message": "Arquivo do CAGED para YYYYMM ainda não foi publicado"}`
  when the FTP replies 550; the workflow catches it (Cloud Workflows
  raises on any non-2xx) and treats it as "stop, nothing to do". Real
  failures (network, permissions, disk) stay HTTP 500 with a
  `"<step> falhou: <ExceptionType>: <detail>"` message and fail the
  execution.
- **Idempotency.** `load_period` refuses a month that already has rows in
  `fact_jobs` before seeding dimensions or inserting anything; `/load`
  maps that to HTTP 409 `{"status": "already_loaded"}`. Re-running a
  month therefore never duplicates rows. Known limits: the fact insert is
  a chunked streaming insert, not one atomic job, so a load that dies
  midway leaves a partial month that both the guard and `/next-period`
  then treat as "loaded"; and two executions loading the same month at
  the same time could both pass the guard. Avoid overlapping manual and
  scheduled runs until the load moves to an atomic load job.
