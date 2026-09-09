"""
US 30Y (and reference tenor) Treasury yield collector.

Two distinct sources per spec 3.2 / 7.2:
  1. Official daily close -- Treasury.gov's daily par yield curve CSV feed.
     Authoritative, once/day, is_official_close=1.
  2. Intraday snapshots (4-5x/trading day target) -- scraped from a public
     bond-quote page. No free authoritative intraday feed exists (7.2), so
     this is inherently a scrape: brittle, needs failure monitoring, and is
     flagged is_official_close=0 so downstream analysis can choose to use
     only the clean daily series.

Run this collector on a schedule matched to its source cadence: the daily
close job once/day after market close; the intraday job 4-5x during US
trading hours on weekdays.
"""
import csv
import io
import re
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import SEC_USER_AGENT, MACRO_HISTORY_DAYS
from collectors.http_utils import get, BlockedDomainError
from db.connection import db_cursor, log_collector_start, log_collector_end

TREASURY_CSV_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)

# Marketwatch's bond quote page for the 30Y yield, used as the intraday
# snapshot source. Selector strategy is regex-based against the raw HTML
# rather than a CSS selector, since page structure changes are expected (7.2)
# and a loose regex on the "Yield" label is more resilient to minor markup
# churn than a brittle DOM path.
MARKETWATCH_30Y_URL = "https://www.marketwatch.com/investing/bond/tmubmusd30y"
YIELD_REGEX = re.compile(r'"yield"\s*:\s*"?(\d+\.\d+)"?', re.IGNORECASE)
YIELD_REGEX_FALLBACK = re.compile(r'([\d]{1,2}\.\d{2,3})\s*%')

TENOR_COLUMN_MAP = {
    "30Y": "30 Yr",
    "10Y": "10 Yr",
    "3M": "3 Mo",
}


def collect_official_close(tenors=("30Y", "10Y", "3M")):
    """Fetch the current year's daily par yield curve CSV and upsert rows
    within MACRO_HISTORY_DAYS for the requested tenors."""
    run_id = log_collector_start("treasury_yields_official_close")
    rows_written = 0
    try:
        year = date.today().year
        url = TREASURY_CSV_URL.format(year=year)
        resp = get(url, headers={"User-Agent": SEC_USER_AGENT})
        reader = csv.DictReader(io.StringIO(resp.text))

        cutoff = date.today().toordinal() - MACRO_HISTORY_DAYS
        with db_cursor() as cur:
            for row in reader:
                try:
                    row_date = datetime.strptime(row["Date"], "%m/%d/%Y").date()
                except (KeyError, ValueError):
                    continue
                if row_date.toordinal() < cutoff:
                    continue
                for tenor in tenors:
                    col = TENOR_COLUMN_MAP.get(tenor)
                    val = row.get(col)
                    if not val:
                        continue
                    try:
                        yield_val = float(val)
                    except ValueError:
                        continue
                    cur.execute(
                        """INSERT INTO us_treasury_yields (timestamp, tenor, yield, is_official_close, source)
                           VALUES (?, ?, ?, 1, 'treasury.gov')
                           ON CONFLICT(timestamp, tenor, is_official_close, source) DO UPDATE SET yield=excluded.yield""",
                        (row_date.isoformat(), tenor, yield_val),
                    )
                    rows_written += 1
        log_collector_end(run_id, "success", rows_written)
        return rows_written
    except BlockedDomainError as e:
        log_collector_end(run_id, "failed", rows_written, str(e))
        raise
    except Exception as e:
        log_collector_end(run_id, "failed", rows_written, f"{type(e).__name__}: {e}")
        raise


def collect_intraday_snapshot():
    """Scrape a single current 30Y yield snapshot. Meant to be called 4-5x
    during US trading hours via the scheduler."""
    run_id = log_collector_start("treasury_yields_intraday")
    try:
        resp = get(MARKETWATCH_30Y_URL, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        match = YIELD_REGEX.search(resp.text) or YIELD_REGEX_FALLBACK.search(resp.text)
        if not match:
            raise ValueError("Could not locate a yield value in the page -- source page structure likely changed (see 7.2).")
        yield_val = float(match.group(1))
        now = datetime.now().replace(microsecond=0).isoformat()
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO us_treasury_yields (timestamp, tenor, yield, is_official_close, source)
                   VALUES (?, '30Y', ?, 0, 'marketwatch_scrape')
                   ON CONFLICT(timestamp, tenor, is_official_close, source) DO UPDATE SET yield=excluded.yield""",
                (now, yield_val),
            )
        log_collector_end(run_id, "success", 1)
        return yield_val
    except BlockedDomainError as e:
        log_collector_end(run_id, "failed", 0, str(e))
        raise
    except Exception as e:
        log_collector_end(run_id, "failed", 0, f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "intraday", "both"], default="both")
    args = parser.parse_args()

    if args.mode in ("daily", "both"):
        try:
            n = collect_official_close()
            print(f"Official close: wrote/updated {n} rows.")
        except Exception as e:
            print(f"Official close FAILED: {e}")

    if args.mode in ("intraday", "both"):
        try:
            y = collect_intraday_snapshot()
            print(f"Intraday snapshot: 30Y = {y}%")
        except Exception as e:
            print(f"Intraday snapshot FAILED: {e}")
