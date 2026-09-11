# Carry Trade / US 30Y Treasury / Hyperscaler & Neocloud Debt Monitor

Local, self-hosted monitor testing whether Japan macro/BOJ policy conditions
lead US 30Y Treasury yield moves, and whether both are linked to the cost of
debt carried by hyperscalers, neoclouds, and frontier AI labs. Built per
`carry-trade-debt-monitor-spec_1.md` (v2, 2026-09-07).

Standalone tool — not wired into any other system. Python 3.11+, SQLite,
FastAPI dashboard with self-contained SVG charts (zero external JS/CDN
dependency, by design for a self-hosted tool), all free/public data sources.

## Status (live as of 2026-09-11)

**New:** the dashboard now has a dedicated "Debt & Rate-Exposure Overview" panel (home page) and a per-entity
debt/capex/cash-flow summary (entity pages) — total debt outstanding, near-term maturity concentration, capex/OCF
ratio, and a cash-flow trend chart with a clearly-labeled (and honesty-flagged) next-year capex projection. Built
from the analysis in `wiki/finance/models/model-hyperscaler-debt-capex-rate-exposure-2026-09-10.md`. See Section
2b below for what's new and why the capex projection is deliberately conservative.


All six spec phases are implemented and are now collecting real data end to
end: Japan CPI (headline + core), BOJ policy rate + upcoming meeting
calendar, USD/JPY + rate differential, US 30Y Treasury (daily close +
intraday scrape), SEC EDGAR cash flow for all 6 structured entities, SEC
EDGAR itemized debt for **all 5 debt-carrying entities** (MSFT, AMZN,
GOOGL, META, ORCL — MSFT/GOOGL/META reconcile to the *exact* disclosed
face value; AMZN and ORCL cover ~94-99% of it, see below; PLTR correctly
carries no debt to itemize), the neocloud/AI-lab press feed, correlation
analysis, refinancing stress, and alerting are all running against live
data with zero collector failures. Dashboard at http://127.0.0.1:8420.

Getting here required fixing a Norton AV TLS-interception issue on 9
external domains (see below) plus several source-specific parsing fixes
(wrong e-Stat survey code, SEC filers using inconsistent XBRL tags and debt
note formats, a frankfurter.app domain migration, BOJ's actual rate figure
living in a PDF rather than HTML). All of that is now working; the numbers
below reflect the current known gaps, not blockers.

## 1. One-time setup

```bash
cd "C:\Users\Mike\carry-trade-debt-monitor"
pip install -r requirements.txt
python db/init_db.py
```

### Norton AV is blocking every external domain this tool needs

This machine's Norton Antivirus does local TLS interception with a
malformed root cert (documented previously for yfinance/GitHub — see
project memory `market_watchlist_ssl_ca_bundle_issue`). No CA-bundle swap
fixes it; each new domain needs a one-time exclusion in **Norton → Web
Shield → HTTPS-scan exclusions**. Add all of these:

- `www.sec.gov`
- `data.sec.gov`
- `home.treasury.gov`
- `api.e-stat.go.jp`
- `www.boj.or.jp`
- `api.frankfurter.dev` (frankfurter.app 301-redirects here)
- `www.marketwatch.com`
- `news.google.com`

All 8 are now excluded and confirmed working. If a collector ever fails
again with a `BlockedDomainError`, it's the same pattern on a domain not yet
in this list (both the console and the dashboard's activity feed name the
exact domain to add).

### Japan CPI needs a free e-Stat API key

Register at https://www.e-stat.go.jp/api/ (free, instant), then:

```bash
cp .env.example .env
# edit .env, set ESTAT_APP_ID=...
```

Nothing else needs a key.

## 2. Running it

```bash
# Phase 1: macro & rates (daily cadence)
python scripts/run_macro_collectors.py

# Treasury intraday snapshot (run 4-5x during US trading hours)
python scripts/run_intraday_snapshot.py

# Phase 2: SEC EDGAR debt + cash flow + neocloud/AI-lab press feed (daily)
python scripts/run_debt_collectors.py

# Phase 3/4: correlation, refinancing-stress, alert evaluation (nightly, after the above)
python scripts/run_analysis.py

# Dashboard
python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8420
# then open http://127.0.0.1:8420
```

### 2b. What's new: debt/capex/cash-flow dashboard panels (2026-09-11)

`scripts/run_analysis.py` now also computes `entity_financial_summary` (via
`analysis/entity_financial_summary.py`), which powers two new dashboard panels:

- **Home page — "Debt & Rate-Exposure Overview"**: total debt outstanding, weighted-avg coupon, near-term
  (next 4yr) maturity %, and capex/OCF ratio for every structured entity, sorted by capex/OCF (highest external-
  financing dependency first). Two exposure channels are shown separately rather than blended into one score —
  near-term maturity (debt that must be refinanced regardless of cash flow) and capex/OCF (capex not covered by
  operating cash flow, meaning new debt/equity must be raised no matter what maturities look like) — a deliberate
  design choice carried over from the research this was built from (see `wiki/finance/models/model-hyperscaler-
  debt-capex-rate-exposure-2026-09-10.md`, Section D9 recommendation #5).
- **Entity pages — debt/capex summary + cash-flow trend chart**: the same four stats plus a multi-series
  (OCF/Capex/FCF) trend chart across full fiscal years only (10-Q rows in this schema are fiscal-year-to-date
  cumulative, not discrete quarters — mixing them into one chronological line would fake a trend), and a "CAPEX
  Outlook" card showing a simple next-year trend projection.

**The CAPEX projection is deliberately conservative and will often refuse to show a number.** Every entity in
this tracked universe is mid-way through an AI-infrastructure buildout with 18–168% YoY capex growth — extrapolating
that forward compounds into obviously-wrong multi-year figures (the research model found even the *slowest* grower's
two-year extrapolation was internally inconsistent). `UNRELIABLE_GROWTH_THRESHOLD` in
`analysis/entity_financial_summary.py` is set to 40% YoY; above that, the dashboard shows "not reliable to
extrapolate" instead of a fabricated-looking number, and always shows the underlying growth rate so the viewer can
judge for themselves. Raise or lower that constant if a different threshold is wanted, but see the module's
docstring before doing so.

**A CoreWeave-shaped accuracy fix that's easy to miss:** `total_debt_outstanding` falls back to the XBRL aggregate
balance-sheet figure (a new `entity_debt_aggregate` table, upserted every collector run) when an entity has no
itemized `debt_instruments` rows — otherwise an entity like CoreWeave (real debt, just not yet itemized — its
"Debt" note uses a table format the extractor doesn't handle) would show identically to Palantir (genuinely zero
debt). The dashboard flags this case with an "aggregate only" badge; near-term maturity % is correctly shown as
"n/a" rather than 0% in that case, since it can't be computed without the itemized breakdown.

## 3. Scheduling (mini PC, Windows Task Scheduler)

The spec calls for cron/systemd timers matched to each source's real
cadence. On Windows, `schtasks` is the equivalent. Example (adjust the
Python path/venv as needed):

```bash
schtasks /create /tn "CarryTradeMonitor_Macro" /tr "python C:\Users\Mike\carry-trade-debt-monitor\scripts\run_macro_collectors.py" /sc daily /st 06:00
schtasks /create /tn "CarryTradeMonitor_Intraday1" /tr "python C:\Users\Mike\carry-trade-debt-monitor\scripts\run_intraday_snapshot.py" /sc daily /st 09:35
schtasks /create /tn "CarryTradeMonitor_Intraday2" /tr "python C:\Users\Mike\carry-trade-debt-monitor\scripts\run_intraday_snapshot.py" /sc daily /st 11:30
schtasks /create /tn "CarryTradeMonitor_Intraday3" /tr "python C:\Users\Mike\carry-trade-debt-monitor\scripts\run_intraday_snapshot.py" /sc daily /st 13:30
schtasks /create /tn "CarryTradeMonitor_Intraday4" /tr "python C:\Users\Mike\carry-trade-debt-monitor\scripts\run_intraday_snapshot.py" /sc daily /st 15:45
schtasks /create /tn "CarryTradeMonitor_Debt" /tr "python C:\Users\Mike\carry-trade-debt-monitor\scripts\run_debt_collectors.py" /sc daily /st 07:00
schtasks /create /tn "CarryTradeMonitor_Analysis" /tr "python C:\Users\Mike\carry-trade-debt-monitor\scripts\run_analysis.py" /sc daily /st 23:00
```

(Alternatively, use the same `mcp__scheduled-tasks` mechanism the other
tools under `.claude\scheduled-tasks\` use, if you'd rather keep everything
in one place — ask and I'll wire that up instead/also.)

The dashboard (`uvicorn dashboard.app:app`) is a long-running process, not a
scheduled job — run it once (e.g. via Task Scheduler "at startup", or just
manually) and leave it up.

## 4. Architecture

```
collectors/   -- one script per data source, idempotent, logs to collector_runs
analysis/     -- correlation.py (rolling/lead-lag/Granger), refinancing_stress.py,
                 entity_financial_summary.py (debt outstanding, capex/OCF, cash-flow trend)
alerts/       -- engine.py, threshold evaluation -> alerts_log
dashboard/    -- FastAPI app + Jinja2 templates + self-contained SVG charts
config/       -- entities.py-equivalent (settings.py), xbrl_tags.py, thresholds
db/           -- schema.sql, init_db.py, monitor.db (SQLite, gitignored)
scripts/      -- orchestration entry points for the scheduler
```

The SQLite DB (`db/monitor.db`) is the shared source of truth — dashboard,
alerting, and any ad-hoc querying (e.g. via Claude Code) all read from it.
Correlation and refinancing-stress results are pre-computed by
`scripts/run_analysis.py` and stored in `correlation_results` /
`refinancing_stress`, not recalculated on page load.

## 5. Known limitations (carried over from the spec, Section 7)

- **Correlation is a low-frequency, small-sample problem.** BOJ decisions
  happen every 6-7 weeks; Japan CPI is monthly. The dashboard always shows
  sample size (N) next to every correlation figure — treat low-N rows as
  directional, not statistically robust. Granger causality rows are
  explicitly labeled exploratory.
- **True intraday 30Y data doesn't exist for free.** The intraday collector
  scrapes MarketWatch's bond quote page; this is inherently brittle (page
  structure can change) and is monitored via `collector_runs` /
  `alerts_log` (`collector_failure` alerts), not silently trusted. Only the
  Treasury.gov daily close (`is_official_close=1`) is authoritative.
- **SEC XBRL itemized debt extraction is best-effort, confirmed against
  live filings.** `debt_instruments` has two layers: a reliable aggregate
  (`LongTermDebtNoncurrent`/`Current` via XBRL company-facts — note Oracle
  uses neither tag, it's `LongTermNotesPayable`; see `config/xbrl_tags.py`)
  and a text-pattern extractor (`collectors/sec_edgar_debt.py`) that finds
  the "Debt" note in the latest 10-K and parses its tranche table. Three
  row/heading formats are handled, one per observed filer style:
  - MSFT/Amazon: tranches grouped by issuance year, size restated inline
    ("2015 issuance of $23.8 billion...") — `ISSUANCE_PATTERN`.
  - Google/Meta: rows labeled by year + note type only, size only in the
    value columns ("2016 US dollar notes...", "August 2022 Notes...") —
    `NOTES_LABEL_PATTERN`. Meta's heading is also a different shape
    ("Note 10. Long-term Debt" vs. "NOTE 10 — DEBT") — `DEBT_NOTE_HEADING`
    allows a few words between the note number and "Debt" to cover this.
  - Oracle: one row per individual bond, no grouping ("$750, 3.125%, due
    July 2025..."), with separate issuance-date and current/prior-year
    amount columns, and a heading with no literal word "NOTE" at all ("6.
    NOTES PAYABLE AND OTHER BORROWINGS") — `ORCL_BOND_PATTERN` +
    `parse_orcl_style_bonds()`, which also drops any bond whose
    current-period column reads "N.A"/a dash (already matured as of the
    filing's own reporting date, so correctly excluded from "open").

  All three patterns are tried in order per filer (whichever finds >=3 rows
  wins — a 1-2-row match is much more likely a stray false positive than a
  real table, confirmed live on Oracle's footnotes). MSFT, GOOGL, and Meta
  now reconcile to the *exact* disclosed face value; Amazon and Oracle
  cover ~99% and ~94% respectively (a handful of non-standard rows --
  Amazon's non-coupon-bearing "Other long-term debt" line, Oracle's
  zero-coupon/floating-rate notes -- use yet other formats and are the
  known remaining gap). Palantir is correctly treated as having no debt to
  itemize (its 10-K states no borrowings are outstanding under its credit
  facility) rather than logged as a parse failure — `collect_all()` skips
  the itemized step entirely when aggregate debt is negligible (<$10M).
  Every extracted row carries `source_filing_url` for spot-checking.
  Extending coverage further to the remaining non-standard rows would mean
  inspecting that specific row's text and adding another anchor pattern,
  following the same approach as these three.
- **Neocloud/AI-lab data is structurally thinner.** `reported_events` is a
  press-feed table (Google News RSS, keyword-filtered), not a financial
  statement. Everything in it starts `confidence='unverified'`; use
  `collectors/reported_events_feed.add_manual_event(...)` to hand-curate
  something you've verified (marks it `press_confirmed`). The dashboard
  visually separates this "reported/unverified" tier from the structured
  SEC-filer tier per entity.
- **BOJ policy rate comes from parsing a PDF, and tone classification is
  genuinely soft.** The current rate/statement lives in a per-meeting
  "Statement on Monetary Policy" PDF (`collectors/boj_events.py` uses
  `pypdf`), not any HTML page — confirmed live, currently 1.0% as of the
  2026-07-31 meeting. The keyword-based tone heuristic (7.4) classified
  that meeting as "hawkish" largely because the statement's dissent
  paragraph (a board member arguing for a higher rate, voted down 8-1) uses
  hawkish-coded language even though the actual decision was a hold — a
  concrete illustration of why 7.4 calls this a rough signal, not
  sentiment analysis. The source PDF link is always one click away in the
  dashboard so this can be judged directly.
- **CoreWeave and Nebius CIK/filer-status are unverified.** `config/settings.py`
  flags this explicitly — resolve via SEC's ticker-to-CIK map once
  `www.sec.gov` is reachable, then update `ENTITIES`.
- **Cash flow history depth** defaults to trailing 8 quarters (7.7's
  suggested default) — change `CASHFLOW_HISTORY_QUARTERS` in
  `config/settings.py` if a different depth is wanted.

## 6. What's not built (Phase 6 "polish")

- No auto-restart/health-check wrapper around the dashboard process itself.
- No historical backtesting of alert thresholds — they're the spec's
  suggested defaults (`config/settings.py: ALERT_THRESHOLDS`), not tuned.
- Alert delivery is dashboard-only, as specified. Wiring a push channel
  (ntfy, Slack) later just means reading `alerts_log` and dispatching — no
  changes needed to detection logic.

## 7. Candidate future data sources (not yet integrated)

- **[Equibles](https://equibles.com/)** (flagged 2026-09-10, not evaluated
  in depth) — a freemium financial-data platform offering SEC filings, 13F
  institutional holdings, insider/Congress trades, earnings call
  transcripts, FRED economic data, and more, via a free MCP server, REST
  API, or spreadsheet import. Worth a look for two specific gaps this tool
  has today: (1) a more structured alternative to the brittle direct-10-K
  scraping in `collectors/sec_edgar_debt.py` (Section 5's per-filer anchor
  patterns are inherently fragile to formatting changes), and (2) earnings
  call transcripts as a richer, more verifiable substitute for the
  press-feed-only neocloud/AI-lab tier (7.5) — assuming those specific
  data types fall inside Equibles' free tier. **Before integrating**:
  confirm what's actually free vs. paid (the site describes "freemium" with
  advanced features gated) — Section 2's binding constraint is free/public
  sources only, so any paid tier is out of scope by design, not just by
  preference. Not yet tested against this tool's actual data needs.

## 8. Remote dashboard (Vercel + Turso)

The dashboard can run on Vercel instead of (or alongside) `localhost:8420`,
reading from a remote [Turso](https://turso.tech) (libSQL, SQLite-wire-compatible)
database instead of the local `db/monitor.db` file. Collectors keep running
locally (mini PC, Task Scheduler) and write to the same remote database —
`db/connection.py` transparently switches backend based on whether
`TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` are set (unset = local SQLite, as
before). See that module's docstring for the compatibility-shim details.

**To set it up:**
1. Create a Turso database and auth token at [app.turso.tech](https://app.turso.tech), add both to `.env`.
2. `python db/init_db.py` — applies the schema to Turso instead of the local file once the env vars are set.
3. Run the normal collector/analysis scripts once to populate it.
4. Import the repo at [vercel.com/new](https://vercel.com/new); `pyproject.toml`'s `[tool.vercel]` entrypoint
   and `vercel.json` handle the rest. Add the same two env vars in Vercel's project settings.

**Resolved (2026-09-11):** the first deploy attempt was blocked by Vercel with *"The deployment was blocked
because the commit author did not have contributing access to the project on Vercel. The Hobby Plan does not
support collaboration for private repositories."* — despite the pushing GitHub account (Sincx) owning both the
repo and the Vercel project/team, and the commit author's email being a verified GitHub email. The actual cause
was the local git identity: `user.name` was never set on this machine, so every commit's author showed as
literally **"unknown"** (email was fine). Vercel's collaborator-access check apparently keys off having a
complete author identity, not just a verified email — once `git config --global user.name` was set and a fresh
commit pushed, the deploy went through immediately. If you hit this same error, check `git log --format="%an <%ae>"`
before anything else.

**Resolved (2026-09-11): "Internal Server Error" after the deploy succeeded.** The build went through fine, but
every request 500'd with `sqlite3.OperationalError: unable to open database file` (from `db/connection.py`'s
local-SQLite fallback path — Vercel's filesystem has no `db/monitor.db`, by design). Cause: `vercel env ls`
showed only `ESTAT_APP_ID` was actually set on the Vercel project — `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` had
never been added (the import-screen step in "To set it up" above was missed), so `get_connection()` correctly
fell through to its local-SQLite fallback, which doesn't exist on Vercel. Fixed via `vercel env add
TURSO_DATABASE_URL production` / `preview` (and the same for `TURSO_AUTH_TOKEN`), then `vercel deploy --prod` to
pick up the new env vars — **adding env vars alone does not update an already-built deployment**, a fresh deploy
is required. Verified working end-to-end afterward (live Turso data rendering on both `/` and `/entity/{id}`).
If you see this same 500, run `vercel env ls` first to confirm both variables are actually present before
looking anywhere else.
