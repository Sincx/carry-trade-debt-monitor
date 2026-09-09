"""
Japan CPI collector (headline + core, MoM/YoY) via the e-Stat API (spec 3.1/4).

e-Stat requires free registration for an appId: https://www.e-stat.go.jp/api/
Set ESTAT_APP_ID in .env once registered. Without it this collector fails
loudly (logged to collector_runs) rather than silently producing no data.

Confirmed live (2026-09-10) against real e-Stat responses:
  - statsCode for the Consumer Price Index survey is 00200573 (00200571,
    used in an earlier version of this file, is a different survey --
    Retail Price Survey -- and returned zero CPI tables).
  - The CPI dataset is dimensioned by tab (display item), cat01 (item),
    area (region), and time. Nationwide/all-Japan is area code 00000;
    headline ("all items", 総合) is cat01 0001; core ("all items excluding
    fresh food", 生鮮食品を除く総合 -- Japan's standard core-CPI definition)
    is cat01 0161; the raw index level (not %-change) is tab 1. Each
    base-year table (rebased periodically -- e.g. 2020-base, 2025-base)
    carries a RETROACTIVELY RECALCULATED series back to 1990, so any
    current base-year table has ample history for this project's 180-day
    window; no cross-base stitching is needed.
  - `time` codes are 10 digits: YYYY + "00" + MM + MM (e.g. "2026000707"
    = July 2026). A small number of rows are annual averages with "00" in
    the trailing month position -- skipped, not treated as month 0.
  - The exact statsDataId changes when Japan's Statistics Bureau publishes
    a new base year, so this discovers it via getStatsList (searching
    statsCode 00200573) on first run rather than hardcoding one, caching
    the result. Candidates are sorted by @id descending (newest base-year
    table first) since all carry full history.
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors.http_utils import get, BlockedDomainError
from db.connection import db_cursor, log_collector_start, log_collector_end

ESTAT_BASE = "https://api.e-stat.go.jp/rest/3.0/app/json"
CACHE_FILE = Path(__file__).resolve().parent.parent / "db" / "estat_cache.json"
CPI_STATS_CODE = "00200573"  # Consumer Price Index (消費者物価指数) survey

CAT01_HEADLINE = "0001"  # 総合 -- all items
CAT01_CORE = "0161"      # 生鮮食品を除く総合 -- ex-fresh-food ("core CPI" in BOJ/market usage)
AREA_NATIONWIDE = "00000"  # 全国
TAB_INDEX_LEVEL = "1"      # 指数 (raw index; MoM/YoY computed downstream, not pulled as tab 2/3)


def _app_id() -> str:
    app_id = os.environ.get("ESTAT_APP_ID", "")
    if not app_id:
        raise RuntimeError(
            "ESTAT_APP_ID not set. Register a free appId at https://www.e-stat.go.jp/api/ "
            "and add ESTAT_APP_ID=... to .env."
        )
    return app_id


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def _save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _discover_cpi_stats_data_id(app_id: str) -> str:
    cache = _load_cache()
    if "cpi_stats_data_id" in cache:
        return cache["cpi_stats_data_id"]

    resp = get(
        f"{ESTAT_BASE}/getStatsList",
        params={"appId": app_id, "statsCode": CPI_STATS_CODE, "searchWord": "消費者物価指数 全国 総合", "limit": 10},
    )
    data = resp.json()
    tables = data.get("GET_STATS_LIST", {}).get("DATALIST_INF", {}).get("TABLE_INF", [])
    if isinstance(tables, dict):
        tables = [tables]
    if not tables:
        raise ValueError("e-Stat getStatsList returned no CPI tables under statsCode 00200573 -- search terms may need adjustment.")

    tables.sort(key=lambda t: t["@id"], reverse=True)  # newest base-year table first
    stats_data_id = tables[0]["@id"]
    cache["cpi_stats_data_id"] = stats_data_id
    cache["cpi_table_title"] = tables[0].get("STATISTICS_NAME", "")
    _save_cache(cache)
    return stats_data_id


def _parse_time_code(time_code: str) -> date | None:
    """'YYYY00MMMM' -> date(YYYY, MM, 1); returns None for non-monthly
    (e.g. annual-average) rows, identified by a non-01..12 trailing month."""
    if len(time_code) != 10:
        return None
    try:
        year = int(time_code[:4])
        month = int(time_code[-2:])
    except ValueError:
        return None
    if not 1 <= month <= 12:
        return None
    try:
        return date(year, month, 1)
    except ValueError:
        return None


def collect_cpi():
    run_id = log_collector_start("japan_cpi")
    rows_written = 0
    try:
        app_id = _app_id()
        stats_data_id = _discover_cpi_stats_data_id(app_id)

        # One request per cat01 code, not combined. cdTimeFrom proved
        # unreliable live (2026-09-10): it can silently skip several months
        # rather than cleanly filtering from that point forward, and a
        # combined multi-cat01 request risks the headline series (decades of
        # history) consuming the whole `limit` before core-series rows are
        # reached. An unfiltered per-series pull (limit=500, well above the
        # ~14 months this project needs) reliably returned continuous
        # monthly data in testing, so client-side date filtering afterward
        # is the safer approach here.
        cutoff = date.today() - timedelta(days=430)  # >180d to allow YoY comparisons at the window edge
        with db_cursor() as cur:
            for cat_code, indicator in ((CAT01_HEADLINE, "cpi_headline_index"), (CAT01_CORE, "cpi_core_index")):
                resp = get(
                    f"{ESTAT_BASE}/getStatsData",
                    params={
                        "appId": app_id, "statsDataId": stats_data_id,
                        "cdCat01": cat_code, "cdArea": AREA_NATIONWIDE, "cdTab": TAB_INDEX_LEVEL,
                        "limit": 500,
                    },
                )
                data = resp.json()
                stat_data = data.get("GET_STATS_DATA", {}).get("STATISTICAL_DATA", {})
                values = stat_data.get("DATA_INF", {}).get("VALUE", [])
                if isinstance(values, dict):
                    values = [values]

                for v in values:
                    obs_date = _parse_time_code(v.get("@time", ""))
                    val = v.get("$")
                    if obs_date is None or val is None or obs_date < cutoff:
                        continue
                    cur.execute(
                        """INSERT INTO japan_macro_indicators (date, indicator, value, unit, source, release_type)
                           VALUES (?, ?, ?, 'index', 'e-stat.go.jp', 'final')
                           ON CONFLICT(date, indicator, source) DO UPDATE SET value=excluded.value""",
                        (obs_date.isoformat(), indicator, float(val)),
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


if __name__ == "__main__":
    try:
        n = collect_cpi()
        print(f"Japan CPI: wrote/updated {n} rows.")
    except Exception as e:
        print(f"Japan CPI collector FAILED: {e}")
