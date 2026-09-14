# Data spec — jobs_bi_brazil

## 1. Source

| Source | What it provides | Grain | Refresh cadence | Access |
|---|---|---|---|---|
| CAGED (Novo CAGED, Brazilian Ministry of Labor) | Formal hiring/termination events (CLT), across all occupations and sectors | One row per admission or termination event | Monthly, ~2 weeks after month close | Public FTP, `.7z` files |

This project intentionally has a single data source. An earlier draft
combined CAGED with Indeed job postings to cover a tech-specific screen;
that screen was dropped, and Indeed is no longer part of this project —
see PRD.md §3.

## 2. Star schema

Fact table `fact_jobs` sits at the center; all dimensions are looked up by
surrogate key. See `sql/schema.sql` for the executable DDL. Summary:

- `dim_time` — one row per calendar month.
- `dim_location` — state, city, IBGE code, region.
- `dim_occupation` — CBO code, **official title**, **display name**, area.
  The official title and display name are intentionally two separate
  columns — see §3.
- `dim_company_size` — company size bracket (micro/small/medium/large).
  CAGED does not expose individual employer identity, only size bracket,
  so this is a small lookup dimension, not a company registry.
- `fact_jobs` — one row per hiring event, always `source = 'caged'`.

## 3. Known limitation: occupation code granularity is inconsistent

CBO (Classificação Brasileira de Ocupações) codes are the only occupation
taxonomy CAGED provides. Their granularity is not uniform: some codes map
cleanly to a single, narrow job title; others bundle several real-world
roles under one code, because CBO was designed for general labor
statistics, not for fine-grained role distinction in any one field.

**Rule enforced by this project:** `dim_occupation.display_name` may
rename a CBO's bureaucratic title into a readable label, but it must
never imply a level of specificity the CBO code doesn't actually have.
This was a real mistake caught during design on an earlier draft of this
project (a tech-focused label made an occupation-level number look like
a technology-level number) — the fix was reverting the label to match
the code's actual grain, not just making the label vaguer.

## 4. Known limitation: CAGED measures events, not people searching

CAGED reports hiring and termination events that already happened. It
does not report how many people are currently looking for a job in any
occupation, sector, or region. Any "net hiring balance" figure
(admissions − terminations) shown in this project answers "is this
occupation/sector growing or shrinking," never "how easy is it to find
a job" or "how many people want this job."

## 5. Refresh cadence and staleness

CAGED refreshes monthly, and the pipeline pulls at most once per month —
pulling more often wastes compute against a source that has not changed
in between. Every dashboard must show a "last updated" timestamp derived
from `MAX(fact_jobs.load_timestamp)`.

## 6. Volume consideration

Without a narrow occupation filter, a single CAGED month covers every
formal hiring/termination event in Brazil. This is orders of magnitude
larger than the tech-only subset an earlier draft of this project
planned around. Ingestion, storage, and query design must account for
this from the start — see TECH_SPEC.md §4.
