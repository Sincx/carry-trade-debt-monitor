"""
Correlation & linkage analysis engine (spec 3.5, 7.1).

Two distinct analysis modes, deliberately kept separate rather than forced
into one framework, because they have very different statistical character:

  1. Continuous daily-frequency pairs (USD/JPY, rate differential vs US 30Y
     yield): real daily sample sizes, safe to run rolling Pearson/Spearman
     and lag cross-correlation on.
  2. Event-driven pairs (BOJ rate decisions, Japan CPI releases vs US 30Y
     yield reaction): these happen every 6-7 weeks / monthly, so forward-
     filling to daily frequency to inflate N would misrepresent statistical
     power (7.1's core warning). Instead we correlate the event "surprise"
     against the subsequent yield move across only the actual events in the
     window, and always report that (small) sample_size honestly.

All output is written to correlation_results (auditable), never silently
recomputed only in-memory for the dashboard.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import CORRELATION_WINDOWS, ALERT_THRESHOLDS
from db.connection import db_cursor, get_connection


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_daily_series(indicator_query: str, params: tuple, date_col: str, value_col: str) -> pd.Series:
    conn = get_connection()
    try:
        df = pd.read_sql_query(indicator_query, conn, params=params)
    finally:
        conn.close()
    if df.empty:
        return pd.Series(dtype=float)
    df[date_col] = pd.to_datetime(df[date_col])
    s = df.set_index(date_col)[value_col].sort_index()
    return s[~s.index.duplicated(keep="last")]


def load_us_30y_daily() -> pd.Series:
    return _load_daily_series(
        "SELECT timestamp as d, yield as v FROM us_treasury_yields WHERE tenor='30Y' AND is_official_close=1",
        (), "d", "v",
    )


def load_usdjpy_daily() -> pd.Series:
    return _load_daily_series(
        "SELECT timestamp as d, rate as v FROM fx_rates WHERE pair='USDJPY'",
        (), "d", "v",
    )


def load_rate_differential_daily() -> pd.Series:
    return _load_daily_series(
        "SELECT date as d, differential as v FROM rate_differentials",
        (), "d", "v",
    )


def load_boj_events() -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            "SELECT date, rate_before, rate_after FROM boj_events "
            "WHERE event_type='rate_decision' AND rate_before IS NOT NULL AND rate_after IS NOT NULL "
            "ORDER BY date", conn,
        )
    finally:
        conn.close()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df["surprise"] = df["rate_after"] - df["rate_before"]
    return df


def load_japan_cpi_events() -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            "SELECT date, value FROM japan_macro_indicators WHERE indicator='cpi_headline_index' ORDER BY date",
            conn,
        )
    finally:
        conn.close()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df["yoy_change"] = df["value"].pct_change(12) * 100  # % YoY, assumes monthly cadence
    return df


# ---------------------------------------------------------------------------
# Continuous-pair rolling correlation + lead-lag + Granger (mode 1)
# ---------------------------------------------------------------------------

def rolling_correlation(series_a: pd.Series, series_b: pd.Series, window_days: int, method="pearson"):
    """Correlate day-over-day CHANGES (not levels) of two aligned series over
    the most recent window_days observations. Returns (corr, p_value, n)."""
    df = pd.concat([series_a.diff(), series_b.diff()], axis=1, join="inner").dropna()
    df.columns = ["a", "b"]
    df = df.tail(window_days)
    n = len(df)
    if n < 5:
        return None, None, n
    if method == "pearson":
        corr, p = pearsonr(df["a"], df["b"])
    else:
        corr, p = spearmanr(df["a"], df["b"])
    return float(corr), float(p), n


def lead_lag_correlation(series_a: pd.Series, series_b: pd.Series, window_days: int, max_lag=10):
    """Test whether series_a leads series_b: shift series_a forward by lag
    days and find the lag with the strongest Pearson correlation of changes.
    Positive lag => series_a's move at t predicts series_b's move at t+lag."""
    df = pd.concat([series_a.diff(), series_b.diff()], axis=1, join="inner").dropna()
    df.columns = ["a", "b"]
    df = df.tail(window_days + max_lag)
    best = {"lag": 0, "corr": None, "n": 0}
    for lag in range(-max_lag, max_lag + 1):
        shifted = pd.concat([df["a"].shift(lag), df["b"]], axis=1).dropna()
        if len(shifted) < 5:
            continue
        corr, _ = pearsonr(shifted.iloc[:, 0], shifted.iloc[:, 1])
        if best["corr"] is None or abs(corr) > abs(best["corr"]):
            best = {"lag": lag, "corr": float(corr), "n": len(shifted)}
    return best


def granger_causality_test(series_a: pd.Series, series_b: pd.Series, window_days: int, max_lag=5):
    """Exploratory only (per 3.5): does series_a Granger-cause series_b?
    Returns the minimum p-value across tested lags, or None if too few obs."""
    try:
        from statsmodels.tsa.stattools import grangercausalitytests
    except ImportError:
        return None, None

    df = pd.concat([series_b.diff(), series_a.diff()], axis=1, join="inner").dropna()  # [effect, cause] order
    df = df.tail(window_days)
    if len(df) < max_lag * 3 + 5:
        return None, len(df)
    try:
        results = grangercausalitytests(df.values, maxlag=max_lag, verbose=False)
        p_values = [results[lag][0]["ssr_ftest"][1] for lag in results]
        return float(min(p_values)), len(df)
    except Exception:
        return None, len(df)


# ---------------------------------------------------------------------------
# Event-driven correlation (mode 2) -- BOJ decisions / Japan CPI vs 30Y reaction
# ---------------------------------------------------------------------------

def event_reaction_correlation(events_df: pd.DataFrame, surprise_col: str, us30y: pd.Series, reaction_window_days=5):
    """For each event, measure the US 30Y yield change over the following
    reaction_window_days trading days, then Pearson-correlate that reaction
    against the event's surprise magnitude. Sample size = number of events,
    reported honestly rather than inflated by forward-filling."""
    if events_df.empty or us30y.empty:
        return None, None, 0

    reactions, surprises = [], []
    idx = us30y.index
    for _, row in events_df.iterrows():
        event_date = row["date"]
        after = idx[idx >= event_date]
        before = idx[idx <= event_date]
        if len(after) == 0 or len(before) == 0:
            continue
        start_date = before[-1]
        end_candidates = after[after <= event_date + timedelta(days=reaction_window_days * 2)]
        if len(end_candidates) == 0:
            continue
        end_pos = min(reaction_window_days, len(end_candidates) - 1)
        end_date = end_candidates[end_pos]
        reaction = us30y.loc[end_date] - us30y.loc[start_date]
        reactions.append(reaction)
        surprises.append(row[surprise_col])

    n = len(reactions)
    if n < 4:
        return None, None, n
    corr, p = pearsonr(surprises, reactions)
    return float(corr), float(p), n


# ---------------------------------------------------------------------------
# Persistence + regime-shift flagging
# ---------------------------------------------------------------------------

def _previous_correlation(series_a: str, series_b: str, window_days: int, method: str):
    with db_cursor() as cur:
        cur.execute(
            """SELECT correlation_value FROM correlation_results
               WHERE series_a=? AND series_b=? AND window_days=? AND method=?
               ORDER BY run_date DESC LIMIT 1""",
            (series_a, series_b, window_days, method),
        )
        row = cur.fetchone()
        return row["correlation_value"] if row else None


def store_result(series_a, series_b, window_days, method, corr, lag_days, sample_size, p_value, notes=""):
    prev = _previous_correlation(series_a, series_b, window_days, method)
    regime_shift = 0
    if corr is not None and prev is not None and abs(corr - prev) >= ALERT_THRESHOLDS["correlation_regime_shift_delta"]:
        regime_shift = 1

    today = date.today().isoformat()
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO correlation_results
                 (run_date, series_a, series_b, window_days, method, correlation_value,
                  lag_days, sample_size, p_value, regime_shift_flag, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (today, series_a, series_b, window_days, method, corr, lag_days, sample_size, p_value, regime_shift, notes),
        )
    if regime_shift:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO alerts_log (alert_type, message, severity)
                   VALUES ('correlation_regime_shift', ?, 'warning')""",
                (f"{series_a} vs {series_b} ({window_days}d {method}) shifted from {prev:.2f} to {corr:.2f}",),
            )
    return regime_shift


def run_full_analysis():
    us30y = load_us_30y_daily()
    usdjpy = load_usdjpy_daily()
    rate_diff = load_rate_differential_daily()
    boj_events = load_boj_events()
    cpi = load_japan_cpi_events()

    summary = []

    for window in CORRELATION_WINDOWS:
        for name, series in (("usdjpy", usdjpy), ("us_jp_rate_differential", rate_diff)):
            if series.empty or us30y.empty:
                continue
            for method in ("pearson", "spearman"):
                corr, p, n = rolling_correlation(series, us30y, window, method)
                store_result(name, "us_30y_yield", window, method, corr, 0, n, p)
                summary.append((name, "us_30y_yield", window, method, corr, n))

            if window == max(CORRELATION_WINDOWS):
                best = lead_lag_correlation(series, us30y, window)
                store_result(name, "us_30y_yield", window, "lead_lag_pearson",
                             best["corr"], best["lag"], best["n"], None,
                             notes=f"best lag={best['lag']}d (positive = {name} leads)")

                g_p, g_n = granger_causality_test(series, us30y, window)
                store_result(name, "us_30y_yield", window, "granger_exploratory",
                             None, 0, g_n or 0, g_p, notes="EXPLORATORY diagnostic only, per 3.5")

    if not boj_events.empty and not us30y.empty:
        corr, p, n = event_reaction_correlation(boj_events, "surprise", us30y)
        store_result("boj_rate_surprise", "us_30y_yield_reaction", 0, "pearson_event", corr, 0, n, p,
                      notes="Event-driven: N = number of BOJ decisions, not trading days. Low-frequency series -- see 7.1.")

    if not cpi.empty and not us30y.empty:
        cpi_events = cpi.dropna(subset=["yoy_change"]).rename(columns={"yoy_change": "surprise"})
        corr, p, n = event_reaction_correlation(cpi_events, "surprise", us30y)
        store_result("japan_cpi_yoy", "us_30y_yield_reaction", 0, "pearson_event", corr, 0, n, p,
                      notes="Event-driven: N = number of CPI releases, not trading days. Low-frequency series -- see 7.1.")

    return summary


if __name__ == "__main__":
    results = run_full_analysis()
    for r in results:
        print(r)
    print(f"Wrote {len(results)}+ correlation_results rows.")
