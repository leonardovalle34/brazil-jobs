-- ============================================================
-- jobs_bi_brazil — star schema (BigQuery DDL)
-- Single source: CAGED (Brazilian Ministry of Labor)
-- Scope: entire formal labor market, no industry-specific filter.
-- See docs/DATA_SPEC.md for full source and limitation documentation.
--
-- NOTE ON KEYS: BigQuery has no auto-increment and does not enforce
-- PRIMARY KEY / FOREIGN KEY constraints — they are declared here as
-- documentation and for query optimizer hints only ("NOT ENFORCED").
-- Surrogate keys are generated at load time in ingestion/load_to_bigquery.py
-- (pending), not by the database itself.
-- ============================================================

CREATE TABLE IF NOT EXISTS `jobs_bi_brazil.dim_time` (
    time_id     INT64 NOT NULL,
    ref_date    DATE NOT NULL,      -- first day of the reference month
    year        INT64 NOT NULL,
    month       INT64 NOT NULL,
    quarter     INT64 NOT NULL,
    PRIMARY KEY (time_id) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS `jobs_bi_brazil.dim_location` (
    location_id INT64 NOT NULL,
    state       STRING NOT NULL,     -- UF
    city        STRING,
    ibge_code   STRING,
    region      STRING,              -- North/Northeast/South/Southeast/Midwest
    PRIMARY KEY (location_id) NOT ENFORCED
);

-- Covers ALL occupations, not a hand-picked subset — see docs/TECH_SPEC.md §4.
CREATE TABLE IF NOT EXISTS `jobs_bi_brazil.dim_occupation` (
    occupation_id   INT64 NOT NULL,
    cbo_code        STRING NOT NULL,
    official_title  STRING NOT NULL,  -- official/bureaucratic government title
    display_name    STRING NOT NULL,  -- readable label — see docs/DATA_SPEC.md §3 for governance rule
    sector_cnae     STRING,           -- economic sector this occupation is typically reported under
    PRIMARY KEY (occupation_id) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS `jobs_bi_brazil.dim_sector` (
    sector_id     INT64 NOT NULL,
    cnae_code     STRING NOT NULL,
    sector_name   STRING NOT NULL,
    macro_sector  STRING,             -- Services / Trade / Industry / Construction / Agriculture
    PRIMARY KEY (sector_id) NOT ENFORCED
);

-- CAGED does not expose individual employer identity, only a size bracket,
-- so this is a small lookup dimension, not a company registry.
CREATE TABLE IF NOT EXISTS `jobs_bi_brazil.dim_company_size` (
    company_size_id INT64 NOT NULL,
    size_bracket     STRING NOT NULL,  -- micro / small / medium / large
    PRIMARY KEY (company_size_id) NOT ENFORCED
);

-- FACT: FORMAL HIRING/TERMINATION EVENTS
-- Partitioned by ref_date and clustered by occupation/location — this
-- table covers the full national dataset (see docs/TECH_SPEC.md §4), so
-- partitioning keeps monthly queries from scanning the whole history and
-- keeps this project inside BigQuery's free query-scan tier.
CREATE TABLE IF NOT EXISTS `jobs_bi_brazil.fact_jobs` (
    fact_job_id       INT64 NOT NULL,
    time_id           INT64 NOT NULL,
    ref_date          DATE NOT NULL,          -- denormalized from dim_time for partitioning
    location_id       INT64 NOT NULL,
    occupation_id     INT64 NOT NULL,
    sector_id         INT64,
    company_size_id   INT64,

    event_type        STRING NOT NULL,        -- 'admission' | 'termination'
    salary            NUMERIC,
    is_typical        BOOL,                    -- typical vs. non-typical contract, see docs/DATA_SPEC.md
    gender            STRING,
    age_bracket       STRING,
    education_level   STRING,

    load_timestamp    TIMESTAMP,

    PRIMARY KEY (fact_job_id) NOT ENFORCED,
    FOREIGN KEY (time_id) REFERENCES `jobs_bi_brazil.dim_time`(time_id) NOT ENFORCED,
    FOREIGN KEY (location_id) REFERENCES `jobs_bi_brazil.dim_location`(location_id) NOT ENFORCED,
    FOREIGN KEY (occupation_id) REFERENCES `jobs_bi_brazil.dim_occupation`(occupation_id) NOT ENFORCED
)
PARTITION BY ref_date
CLUSTER BY occupation_id, location_id;
