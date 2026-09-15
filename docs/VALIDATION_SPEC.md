# Validation spec — jobs_bi_brazil

## 1. Testing philosophy — TDD

For every pure transformation function (parsing, cleaning, extraction,
formatting), the workflow is:

1. Write a failing test that encodes the expected behavior, including at
   least one edge case (empty input, malformed input, ambiguous input).
2. Write the minimum code to make it pass.
3. Refactor with the test as a safety net.

Functions with I/O (network download, file extraction, DB writes) are
tested through thin wrappers: the I/O boundary is mocked, and the pure
logic on either side of it is unit-tested directly. Integration tests
(actually hitting the CAGED FTP or a real BigQuery dataset) are marked and run
separately from the fast unit suite.

## 2. Unit test coverage — required before merge

| Module | Must be covered by tests |
|---|---|
| `ingestion/caged_ingest.py` (`to_fact_format`) | Column mapping is correct for a known input row; `event_type` maps `1` → `admission` and `-1` → `termination`; unmapped/unexpected codes do not silently become a wrong category — they must surface as `None` plus a warning, not a guess |
| `ingestion/load_to_bigquery.py` (pending) | A row with a foreign key that doesn't yet exist in a dimension table raises a clear error rather than being silently dropped |
| `dim_occupation` seeding script (pending) | A CBO code with no `display_name` entry yet fails loudly rather than falling back to the raw official title silently — the governance rule in DATA_SPEC.md §3 must be enforced in code, not just in review |

## 3. Data quality checks — run after each pipeline execution

These are checks on the *data*, not the *code*, and run against the
warehouse after a load:

- **Row count sanity**: a CAGED monthly load should not vary more than
  an order of magnitude from the prior month, without a manual override
  flag. A 10x drop usually means the source file was truncated or the
  column mapping broke, not that the labor market actually changed
  that fast. With the full national dataset (no occupation filter),
  this check matters even more — a partial download is easy to miss
  when the absolute row count is already large.
- **No orphan facts**: every `fact_jobs.occupation_id`, `location_id`,
  `time_id` must resolve to an existing dimension row. Zero tolerance —
  fail the load, don't insert nulls.
- **Display name governance**: every `dim_occupation.display_name` must
  be reviewed against DATA_SPEC.md §3 before being added — specifically,
  it must not claim a level of specificity the underlying CBO code does
  not support. This is a manual review step, logged in the PR that adds
  or edits a `dim_occupation` row. Given the full-dataset scope, this
  review has to scale to many more rows than the earlier tech-only
  draft — flag any batch import of `dim_occupation` rows that skips
  individual review.

## 4. Acceptance criteria per pipeline stage

| Stage | Acceptance criteria |
|---|---|
| Download | File hash or size is logged; a failed download raises, it never proceeds silently with a partial file |
| Extract & filter | Row count before/after the CBO filter is logged, so a filter that accidentally matches zero rows is visible immediately, not discovered downstream |
| Transform | Output schema matches `fact_jobs` column-for-column before load is attempted |
| Load | Load is transactional per source file — a failure partway through does not leave a half-loaded month in the warehouse |
| API | Every response includes `sources` and per-source `last_updated`, per TECH_SPEC.md §6 |
| Dashboard | No panel renders a number without a visible source label, matching the pattern established in the validated wireframes |

## 5. What "done" looks like for v1

- `pytest` passes with the coverage in §2.
- One full real CAGED month has been loaded end-to-end and manually
  spot-checked against the source file (not just against the pipeline's
  own output).
- Every claim in PRD.md §5 (General Labor Market Dashboard) is backed by
  a query that can be pointed to a specific `fact_jobs`/dimension
  combination — no hard-coded or placeholder numbers remain in the
  shipped dashboard.

## 6. Validation log

| Period | Check | Result |
|---|---|---|
| 2025-07 | Admissions/terminations count vs. official government release ([Agência Gov](https://agenciagov.ebc.com.br/noticias/202508/novo-caged-brasil-gera-mais-de-129-mil-empregos-formais-em-julho)) | Pipeline: 2,251,440 admissions / 2,121,665 terminations. Official: same figures, net balance +129,775. Match confirmed. |
| 2026-07 | Admissions/terminations count vs. official government release (press coverage citing MTE) | Pipeline: 2,262,888 admissions / 2,204,320 terminations. Official: same figures, net balance +58,568. Match confirmed on first run, no fixes needed — confirms column layout is stable across periods. |
| 2025-07 (SC only) | First real BigQuery load — row count and event_type totals vs. source parquet | 280,769 rows loaded (141,771 admissions / 138,998 terminations), sum matches total rows exactly. No orphan facts (dimension resolution ran before fact insert). Confirms end-to-end pipeline: FTP -> parquet -> GCS -> BigQuery. |
| 2025-07 (full national dataset, no filter) | Full BigQuery load — event_type totals vs. official government release | 4,373,105 rows loaded (2,251,440 admissions / 2,121,665 terminations). Exact match with official figures. Confirms pipeline is correct at full national volume, not just on the SC test slice. |
| 2025-06 (full pipeline, non-consecutive month) | Full automated Workflow run vs. official government release | 4,111,743 rows loaded (2,139,182 admissions / 1,972,561 terminations, net balance +166,621). Matches the original/preliminary release exactly. Note: a later (2026-07) article cites revised figures for the same month (2,220,131 / 2,074,970) — the government revises past months over time due to late declarations; this pipeline's monthly "MOV" file reflects the point-in-time snapshot as filed, not retroactive revisions. Worth documenting as a known characteristic of CAGED data, not a pipeline discrepancy. |
