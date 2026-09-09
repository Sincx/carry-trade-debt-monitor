"""
USD/JPY spot rate + short-term rate differential collector (spec 3.1).

FX source: frankfurter.app -- free, no API key, ECB reference rates, daily
granularity (sufficient for a "carry attractiveness" reference rate per the
spec's own note that free-tier FX APIs aren't tick-level).

Rate differential: US 3-month Treasury yield (already collected by
treasury_yields.py) minus the latest BOJ policy rate (from boj_events) --
a simple, transparent proxy rather than a precise cross-currency basis swap
calculation, which the spec explicitly scopes as an acceptable proxy.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import MACRO_HISTORY_DAYS
from collectors.http_utils import get, BlockedDomainError
from db.connection import db_cursor, log_collector_start, log_collector_end

# frankfurter.app permanently redirects (301) to frankfurter.dev with a /v1/
# path prefix -- call the new host directly so Norton's per-domain TLS
# exclusion (added for the old host) doesn't need to cover the redirect too.
FRANKFURTER_LATEST = "https://api.frankfurter.dev/v1/latest"
FRANKFURTER_HISTORY = "https://api.frankfurter.dev/v1/{start}..{end}"


def collect_usdjpy_history():
    run_id = log_collector_start("fx_usdjpy")
    rows_written = 0
    try:
        end = date.today()
        start = end - timedelta(days=MACRO_HISTORY_DAYS)
        resp = get(FRANKFURTER_HISTORY.format(start=start.isoformat(), end=end.isoformat()),
                    params={"from": "USD", "to": "JPY"})
        data = resp.json()
        rates = data.get("rates", {})
        with db_cursor() as cur:
            for d, vals in rates.items():
                rate = vals.get("JPY")
                if rate is None:
                    continue
                cur.execute(
                    """INSERT INTO fx_rates (timestamp, pair, rate, source)
                       VALUES (?, 'USDJPY', ?, 'frankfurter.app')
                       ON CONFLICT(timestamp, pair, source) DO UPDATE SET rate=excluded.rate""",
                    (d, rate),
                )
                rows_written += 1
        log_collector_end(run_id, "success" if rows_written else "partial", rows_written)
        return rows_written
    except BlockedDomainError as e:
        log_collector_end(run_id, "failed", rows_written, str(e))
        raise
    except Exception as e:
        log_collector_end(run_id, "failed", rows_written, f"{type(e).__name__}: {e}")
        raise


def compute_rate_differential():
    """Pair the latest US 3M Treasury yield with the latest BOJ policy rate."""
    run_id = log_collector_start("rate_differential")
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT timestamp, yield FROM us_treasury_yields WHERE tenor='3M' ORDER BY timestamp DESC LIMIT 1"
            )
            us_row = cur.fetchone()
            cur.execute(
                "SELECT date, rate_after FROM boj_events WHERE rate_after IS NOT NULL ORDER BY date DESC LIMIT 1"
            )
            jp_row = cur.fetchone()

            if not us_row or not jp_row:
                raise ValueError("Missing US 3M yield or BOJ policy rate -- run treasury_yields and boj_events collectors first.")

            us_rate = us_row["yield"]
            jp_rate = jp_row["rate_after"]
            differential = us_rate - jp_rate
            today = date.today().isoformat()

            cur.execute(
                """INSERT INTO rate_differentials (date, us_short_rate, jp_short_rate, differential, source)
                   VALUES (?, ?, ?, ?, 'derived: treasury.gov 3M vs BOJ policy rate')
                   ON CONFLICT(date) DO UPDATE SET
                     us_short_rate=excluded.us_short_rate, jp_short_rate=excluded.jp_short_rate,
                     differential=excluded.differential""",
                (today, us_rate, jp_rate, differential),
            )
        log_collector_end(run_id, "success", 1)
        return differential
    except BlockedDomainError as e:
        log_collector_end(run_id, "failed", 0, str(e))
        raise
    except Exception as e:
        log_collector_end(run_id, "failed", 0, f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    try:
        n = collect_usdjpy_history()
        print(f"USD/JPY: wrote/updated {n} rows.")
    except Exception as e:
        print(f"USD/JPY collector FAILED: {e}")

    try:
        diff = compute_rate_differential()
        print(f"Rate differential (US 3M - BOJ policy): {diff}")
    except Exception as e:
        print(f"Rate differential FAILED: {e}")
