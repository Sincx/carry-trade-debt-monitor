"""Central configuration: paths, thresholds, source URLs, network settings."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "db" / "monitor.db"

# SEC requires a descriptive User-Agent identifying the requester.
SEC_USER_AGENT = "Carry Trade Debt Monitor sinclair.ma77@gmail.com"

# Norton AV on this machine intercepts TLS to unrecognized domains with a
# malformed root cert (see project memory: market_watchlist_ssl_ca_bundle_issue).
# No CA-bundle swap fixes this -- each new domain must be excluded in Norton's
# Web Shield HTTPS-scan settings. Collectors fail gracefully (logged to
# collector_runs) rather than crashing when a domain is still blocked.
REQUIRED_EXTERNAL_DOMAINS = [
    "www.sec.gov",
    "data.sec.gov",
    "home.treasury.gov",
    "api.e-stat.go.jp",
    "www.boj.or.jp",
    "api.frankfurter.dev",  # frankfurter.app 301-redirects here now
    "www.marketwatch.com",
    "news.google.com",
]

HTTP_TIMEOUT_SECONDS = 20

# --- History depth (Section 2 / 7.7) ---
MACRO_HISTORY_DAYS = 180        # Japan CPI, USD/JPY, US 30Y yield
CASHFLOW_HISTORY_QUARTERS = 8   # trailing 4-8 quarters per 7.7

# --- Alert thresholds (Section 3.7, configurable) ---
ALERT_THRESHOLDS = {
    "treasury_30y_intraday_bps": 10,   # flag intraday move beyond this
    "treasury_30y_weekly_bps": 25,     # flag week-over-week move beyond this
    "boj_alert_on_any_change": True,   # any policy rate change
    "boj_alert_on_hold_streak_break": True,  # "no change" alert if it breaks an expected-hold pattern
    "new_issuance_lookback_days": 7,   # "issued new debt in the last N days"
    "correlation_regime_shift_delta": 0.3,   # abs change in rolling correlation vs prior window to flag
}

# Rolling correlation windows (days) per 3.5.1
CORRELATION_WINDOWS = [30, 90, 180]

# --- Entity list (Section 6) ---
# CIK values are zero-padded 10-digit SEC identifiers. Verified against
# SEC's public ticker map where connectivity allowed; CoreWeave's CIK is
# unverified pending Norton exclusion for www.sec.gov (see 7.3: must be
# validated against a known filing before being trusted -- collectors
# re-verify on first successful run and flag mismatches).
ENTITIES = [
    # Hyperscalers -- structured tier
    {"entity_id": "MSFT", "name": "Microsoft Corporation", "category": "hyperscaler",
     "is_public": True, "ticker": "MSFT", "cik": "0000789019", "data_tier": "structured"},
    {"entity_id": "AMZN", "name": "Amazon.com, Inc.", "category": "hyperscaler",
     "is_public": True, "ticker": "AMZN", "cik": "0001018724", "data_tier": "structured"},
    {"entity_id": "GOOGL", "name": "Alphabet Inc.", "category": "hyperscaler",
     "is_public": True, "ticker": "GOOGL", "cik": "0001652044", "data_tier": "structured"},
    {"entity_id": "META", "name": "Meta Platforms, Inc.", "category": "hyperscaler",
     "is_public": True, "ticker": "META", "cik": "0001326801", "data_tier": "structured"},
    {"entity_id": "ORCL", "name": "Oracle Corporation", "category": "hyperscaler",
     "is_public": True, "ticker": "ORCL", "cik": "0001341439", "data_tier": "structured"},

    # Other AI-infrastructure-adjacent, public -- structured tier
    {"entity_id": "PLTR", "name": "Palantir Technologies Inc.", "category": "other",
     "is_public": True, "ticker": "PLTR", "cik": "0001321655", "data_tier": "structured"},

    # Neoclouds -- mostly private, reported/unverified tier (7.5)
    {"entity_id": "CRWV", "name": "CoreWeave, Inc.", "category": "neocloud",
     "is_public": True, "ticker": "CRWV", "cik": "0001769628",
     "data_tier": "structured",
     "notes": "Public since 2025 IPO. CIK verified 2026-09-10 against SEC company-facts entityName."},
    {"entity_id": "NEBIUS", "name": "Nebius Group N.V.", "category": "neocloud",
     "is_public": True, "ticker": "NBIS", "cik": None,
     "data_tier": "reported_unverified",
     "notes": "Listed (Nasdaq: NBIS) but tracked in the qualitative tier for now -- SEC filer status/CIK not yet verified."},
    {"entity_id": "CRUSOE", "name": "Crusoe Energy Systems", "category": "neocloud",
     "is_public": False, "ticker": None, "cik": None, "data_tier": "reported_unverified"},
    {"entity_id": "LAMBDA", "name": "Lambda, Inc.", "category": "neocloud",
     "is_public": False, "ticker": None, "cik": None, "data_tier": "reported_unverified"},

    # Frontier AI labs -- reported/unverified tier
    {"entity_id": "OPENAI", "name": "OpenAI", "category": "ai_lab",
     "is_public": False, "ticker": None, "cik": None, "data_tier": "reported_unverified"},
    {"entity_id": "ANTHROPIC", "name": "Anthropic", "category": "ai_lab",
     "is_public": False, "ticker": None, "cik": None, "data_tier": "reported_unverified"},
]

STRUCTURED_ENTITY_IDS = [e["entity_id"] for e in ENTITIES if e["data_tier"] == "structured" and e["cik"]]
REPORTED_TIER_ENTITY_IDS = [e["entity_id"] for e in ENTITIES if e["data_tier"] == "reported_unverified"]
