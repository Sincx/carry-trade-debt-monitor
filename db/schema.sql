-- Carry Trade / US 30Y Treasury / Hyperscaler & Neocloud Debt Monitor
-- SQLite schema per spec Section 5

PRAGMA foreign_keys = ON;

-- Japan macro indicators (CPI headline/core, MoM/YoY)
CREATE TABLE IF NOT EXISTS japan_macro_indicators (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,          -- ISO date the observation refers to
    indicator       TEXT NOT NULL,          -- e.g. 'cpi_headline_yoy', 'cpi_core_mom'
    value           REAL NOT NULL,
    unit            TEXT NOT NULL,          -- 'percent', 'index'
    source          TEXT NOT NULL,
    release_type    TEXT,                   -- 'preliminary' | 'final' | 'revision'
    collected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(date, indicator, source)
);

-- BOJ policy rate decisions / meeting events
CREATE TABLE IF NOT EXISTS boj_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    event_type      TEXT NOT NULL,          -- 'rate_decision' | 'scheduled_meeting' | 'statement'
    rate_before     REAL,
    rate_after      REAL,
    statement_tone  TEXT,                   -- 'hawkish' | 'dovish' | 'neutral' | NULL (heuristic, see 7.4)
    tone_keywords    TEXT,                  -- comma-separated keywords that drove the tone classification
    is_upcoming     INTEGER NOT NULL DEFAULT 0,  -- 1 = future scheduled meeting, not yet occurred
    source_url      TEXT,
    collected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(date, event_type)
);

-- FX rates (USD/JPY spot, and any other pairs)
CREATE TABLE IF NOT EXISTS fx_rates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    pair            TEXT NOT NULL,          -- 'USDJPY'
    rate            REAL NOT NULL,
    source          TEXT NOT NULL,
    collected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(timestamp, pair, source)
);

-- Short-term interest rate differential (carry attractiveness proxy)
CREATE TABLE IF NOT EXISTS rate_differentials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    us_short_rate   REAL,                   -- e.g. 3-month Treasury yield
    jp_short_rate   REAL,                   -- BOJ policy rate (short-term proxy)
    differential    REAL,                   -- us_short_rate - jp_short_rate
    source          TEXT NOT NULL,
    collected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(date)
);

-- US Treasury yields (official daily close + intraday scrape snapshots)
CREATE TABLE IF NOT EXISTS us_treasury_yields (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    tenor           TEXT NOT NULL,          -- '30Y', '10Y', '3M', etc.
    yield           REAL NOT NULL,
    is_official_close INTEGER NOT NULL,     -- 1 = Treasury.gov daily par yield; 0 = intraday scrape
    source          TEXT NOT NULL,
    collected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(timestamp, tenor, is_official_close, source)
);

-- Tracked entities
CREATE TABLE IF NOT EXISTS entities (
    entity_id       TEXT PRIMARY KEY,       -- short slug, e.g. 'MSFT'
    name            TEXT NOT NULL,
    category        TEXT NOT NULL CHECK(category IN ('hyperscaler','neocloud','ai_lab','other')),
    is_public       INTEGER NOT NULL,       -- 1 = public/SEC filer, 0 = private
    ticker          TEXT,
    cik             TEXT,                   -- SEC CIK, zero-padded 10-digit string, NULL if private
    data_tier       TEXT NOT NULL DEFAULT 'structured' CHECK(data_tier IN ('structured','reported_unverified')),
    notes           TEXT
);

-- Public-company debt instruments (structured tier)
CREATE TABLE IF NOT EXISTS debt_instruments (
    instrument_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id            TEXT NOT NULL REFERENCES entities(entity_id),
    instrument_type      TEXT NOT NULL,     -- 'bond' | 'note' | 'term_loan' | 'revolver'
    principal             REAL,
    currency              TEXT NOT NULL DEFAULT 'USD',
    coupon_rate           REAL,
    coupon_type           TEXT CHECK(coupon_type IN ('fixed','floating',NULL)),
    reference_rate        TEXT,              -- e.g. 'SOFR' for floating instruments
    reference_spread_bps  REAL,
    issue_date             TEXT,
    maturity_date           TEXT,
    status                TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','matured','retired')),
    yield_at_issuance_30y REAL,              -- US 30Y yield level on issue_date, for 3.5.2 linkage analysis
    source_filing_url     TEXT,
    first_seen_date        TEXT NOT NULL DEFAULT (date('now')),
    last_verified_date     TEXT,
    collected_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_debt_instruments_entity ON debt_instruments(entity_id);
CREATE INDEX IF NOT EXISTS idx_debt_instruments_status ON debt_instruments(status);
CREATE INDEX IF NOT EXISTS idx_debt_instruments_maturity ON debt_instruments(maturity_date);

-- Payment / amortization schedule
CREATE TABLE IF NOT EXISTS debt_payment_schedule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id   INTEGER NOT NULL REFERENCES debt_instruments(instrument_id),
    payment_date    TEXT NOT NULL,
    payment_type    TEXT NOT NULL CHECK(payment_type IN ('coupon','principal')),
    amount          REAL,
    is_estimated    INTEGER NOT NULL DEFAULT 1  -- 1 = derived from standard semi-annual assumption, not disclosed
);

CREATE INDEX IF NOT EXISTS idx_payment_schedule_instrument ON debt_payment_schedule(instrument_id);
CREATE INDEX IF NOT EXISTS idx_payment_schedule_date ON debt_payment_schedule(payment_date);

-- Public company cash flow statements (structured tier)
CREATE TABLE IF NOT EXISTS cashflow_statements (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id                TEXT NOT NULL REFERENCES entities(entity_id),
    period_end                TEXT NOT NULL,
    fiscal_period_type        TEXT,          -- '10-K' | '10-Q'
    operating_cash_flow       REAL,
    capex                     REAL,
    free_cash_flow            REAL,
    cash_and_equivalents      REAL,
    source_filing_url         TEXT,
    collected_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(entity_id, period_end)
);

-- Best-effort qualitative feed for private entities (neocloud / AI-lab "reported/unverified" tier, 7.5)
CREATE TABLE IF NOT EXISTS reported_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL REFERENCES entities(entity_id),
    event_date      TEXT NOT NULL,
    event_type      TEXT NOT NULL,          -- 'funding_round' | 'credit_facility' | 'revenue_report' | 'debt_report' | 'other'
    headline        TEXT NOT NULL,
    amount_usd      REAL,                    -- best-effort, NULL if not disclosed/parseable
    detail          TEXT,
    source_name     TEXT,
    source_url      TEXT,
    confidence      TEXT NOT NULL DEFAULT 'unverified' CHECK(confidence IN ('unverified','press_confirmed')),
    collected_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reported_events_entity ON reported_events(entity_id);

-- Correlation engine output (auditable, pre-computed)
CREATE TABLE IF NOT EXISTS correlation_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date             TEXT NOT NULL,
    series_a              TEXT NOT NULL,
    series_b              TEXT NOT NULL,
    window_days            INTEGER NOT NULL,
    method                 TEXT NOT NULL,    -- 'pearson' | 'spearman' | 'granger'
    correlation_value      REAL,
    lag_days                INTEGER NOT NULL DEFAULT 0,
    sample_size             INTEGER NOT NULL,
    p_value                 REAL,
    regime_shift_flag       INTEGER NOT NULL DEFAULT 0,
    notes                   TEXT,
    collected_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_correlation_results_run ON correlation_results(run_date);
CREATE INDEX IF NOT EXISTS idx_correlation_results_pair ON correlation_results(series_a, series_b, window_days);

-- Refinancing stress estimates (per entity, per run)
CREATE TABLE IF NOT EXISTS refinancing_stress (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date                 TEXT NOT NULL,
    entity_id                 TEXT NOT NULL REFERENCES entities(entity_id),
    maturity_year              INTEGER NOT NULL,
    principal_maturing          REAL NOT NULL,
    weighted_avg_original_coupon REAL,
    current_30y_yield            REAL,
    estimated_coupon_delta_bps    REAL,      -- current benchmark vs original weighted coupon
    estimated_annual_cost_delta   REAL,      -- principal_maturing * delta_bps/10000
    collected_at                   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(run_date, entity_id, maturity_year)
);

-- Alerts log
CREATE TABLE IF NOT EXISTS alerts_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT NOT NULL DEFAULT (datetime('now')),
    alert_type             TEXT NOT NULL,   -- 'yield_move' | 'boj_decision' | 'new_issuance' | 'correlation_regime_shift' | 'collector_failure'
    entity_id_nullable      TEXT REFERENCES entities(entity_id),
    message                 TEXT NOT NULL,
    severity                 TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
    delivered_channel         TEXT NOT NULL DEFAULT 'dashboard',
    acknowledged               INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_alerts_log_timestamp ON alerts_log(timestamp);

-- Collector run log (operational health, supports 7.2's "monitoring for scrape failures")
CREATE TABLE IF NOT EXISTS collector_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    collector_name  TEXT NOT NULL,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','success','partial','failed')),
    rows_written    INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_collector_runs_name ON collector_runs(collector_name, started_at);

-- Per-entity debt/capex/cashflow summary (pre-computed, mirrors the
-- refinancing_stress / correlation_results pattern: the dashboard reads
-- this rather than aggregating debt_instruments/cashflow_statements live).
-- Powers the "Total debt outstanding", "near-term maturity concentration",
-- "capex/OCF ratio", and "CAPEX trend/outlook" dashboard panels -- see
-- analysis/entity_financial_summary.py and the 2026-09-10 research model
-- (wiki: model-hyperscaler-debt-capex-rate-exposure-2026-09-10) this was
-- built from.
CREATE TABLE IF NOT EXISTS entity_financial_summary (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date                     TEXT NOT NULL,
    entity_id                     TEXT NOT NULL REFERENCES entities(entity_id),
    total_debt_outstanding        REAL,             -- sum of open debt_instruments.principal
    weighted_avg_coupon           REAL,              -- principal-weighted, open instruments only
    near_term_maturity_amount      REAL,             -- principal maturing within NEAR_TERM_YEARS (config)
    near_term_maturity_pct          REAL,            -- as % of total_debt_outstanding
    latest_fy_period_end             TEXT,           -- most recent 10-K (full fiscal year) period_end used below
    latest_fy_capex                   REAL,
    latest_fy_ocf                     REAL,
    latest_fy_fcf                     REAL,
    prior_fy_capex                     REAL,
    capex_yoy_growth_pct                REAL,
    capex_ocf_ratio                     REAL,        -- latest_fy_capex / latest_fy_ocf * 100
    projected_next_fy_capex              REAL,       -- simple trend projection -- see analysis module docstring for why this is capped/flagged
    projection_reliable                   INTEGER NOT NULL DEFAULT 1,  -- 0 when YoY growth is too extreme to extrapolate honestly
    is_itemized                            INTEGER NOT NULL DEFAULT 1,  -- 0 = total_debt_outstanding is the XBRL aggregate fallback, not summed from itemized instruments
    collected_at                           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(run_date, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_financial_summary_run ON entity_financial_summary(run_date);

-- Latest XBRL aggregate long-term debt snapshot per entity (noncurrent +
-- current), one row per entity, upserted on every debt-collector run.
-- Needed because itemized debt_instruments coverage is best-effort (7.3)
-- and can legitimately be empty for an entity that still carries real debt
-- (e.g. CoreWeave, whose "Debt" note format isn't handled by the itemized
-- extractor yet) -- without this fallback, entity_financial_summary would
-- indistinguishably show $0 for that case and for an entity with genuinely
-- no debt (e.g. Palantir), which is a real (not cosmetic) accuracy bug.
CREATE TABLE IF NOT EXISTS entity_debt_aggregate (
    entity_id                TEXT PRIMARY KEY REFERENCES entities(entity_id),
    noncurrent                REAL,
    current                    REAL,
    resolved_tag_noncurrent      TEXT,
    resolved_tag_current          TEXT,
    as_of                           TEXT,
    collected_at                     TEXT NOT NULL DEFAULT (datetime('now'))
);
