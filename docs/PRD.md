# PRD — jobs_bi_brazil

## 1. Problem statement

Brazil's formal labor market (CLT — regime celetista) generates rich
public data every month, but it's scattered across dense government PDFs
and paywalled portals. There's no accessible, source-transparent dashboard
that lets someone explore hiring trends, sector health, salary
distribution, and workforce demographics at a glance.

## 2. Goals

- Build a public, reproducible data pipeline around a single official
  source (CAGED — Brazilian Ministry of Labor) covering the entire
  formal labor market, not any single industry.
- Ship one dashboard answering: which sectors and occupations are
  growing or shrinking, where, and for whom (demographics, salary,
  contract type).
- Demonstrate professional data engineering practices (spec-driven
  development, TDD, documented data lineage) suitable for a portfolio.

## 3. Non-goals

- This project does not focus on any single industry (tech or otherwise).
  An earlier draft of this PRD scoped a tech-specific screen using
  CAGED + Indeed; that scope was dropped in favor of the general labor
  market, and Indeed is no longer a data source for this project.
- This is not a real-time system. CAGED refreshes monthly by design —
  no sub-monthly freshness is promised anywhere in the product.
- This project does not attempt to measure job seekers or "labor
  demand" directly. CAGED only reports hiring/termination events that
  already happened, not people currently searching for work. Where a
  growth/shrinkage claim is made, it is framed as "net hiring balance"
  (admissions − terminations), never as "demand."

## 4. Users

- Primary: the project owner, as a portfolio piece demonstrating data
  engineering + BI skills to potential employers/clients.
- Secondary: anyone researching the Brazilian formal labor market who
  wants a free, source-transparent alternative to paywalled reports.

## 5. Scope — v1

### General Labor Market Dashboard
- National headline number: net formal jobs created/destroyed this month.
- Top occupations by net hiring balance, national.
- Net balance by economic sector (CNAE).
- Net balance by state.
- Hiring profile: gender, age bracket, education level.
- Average salary by occupation and by state.
- Salary distribution by minimum-wage bracket.
- Typical vs. non-typical employment contracts.
- Hiring by company size bracket.
- Global filters: month/year, state, sector.
- "Last updated" indicator (single source now — CAGED only — but still
  shown explicitly so the monthly cadence is never mistaken for
  something more frequent).

## 6. Success criteria

- Pipeline runs end-to-end on at least one full real CAGED month, with
  no manually-patched numbers in the final dashboard.
- Every number displayed can be traced back to a specific source file
  and transformation step (see VALIDATION_SPEC.md).
- Test suite (unit + data validation) passes before any dashboard deploy.

## 7. Key risks / open questions

- CAGED's CBO occupation codes vary widely in how specific they are —
  some map to a narrow role, others bundle several real-world job
  titles under one code. Any occupation-level claim must stay at the
  grain the CBO code actually supports (see DATA_SPEC.md §3).
- Row volume: without a CBO filter, a full CAGED month covers the
  entire Brazilian formal workforce (tens of millions of records/year
  across all occupations). Ingestion and storage must be designed for
  that volume from the start, not retrofitted later.
