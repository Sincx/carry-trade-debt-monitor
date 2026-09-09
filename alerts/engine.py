"""
Threshold-based alerting engine (spec 3.7).

Evaluates newly ingested data against configurable thresholds and writes to
alerts_log. Dashboard-only delivery for v1 (delivered_channel stays
'dashboard'); a future push channel (ntfy, Slack) would only need to read
this same table and dispatch, per the spec's explicit separation of
detection from delivery.

Correlation regime-shift alerts are written directly by analysis/correlation.py
at detection time (they're a byproduct of that computation); this module
covers the other three alert types plus collector-failure alerts.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import ALERT_THRESHOLDS
from db.connection import db_cursor


def _log_alert(alert_type: str, message: str, severity: str, entity_id: str | None = None):
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO alerts_log (alert_type, entity_id_nullable, message, severity)
               VALUES (?, ?, ?, ?)""",
            (alert_type, entity_id, message, severity),
        )


def check_treasury_yield_moves() -> int:
    alerts_raised = 0
    with db_cursor() as cur:
        # Intraday move: latest snapshot vs prior snapshot (any source), same day.
        cur.execute(
            """SELECT timestamp, yield FROM us_treasury_yields
               WHERE tenor='30Y' ORDER BY timestamp DESC LIMIT 2"""
        )
        rows = cur.fetchall()
        if len(rows) == 2:
            move_bps = abs(rows[0]["yield"] - rows[1]["yield"]) * 100
            if move_bps >= ALERT_THRESHOLDS["treasury_30y_intraday_bps"]:
                _log_alert("yield_move",
                            f"US 30Y moved {move_bps:.1f}bps ({rows[1]['yield']:.3f}% -> {rows[0]['yield']:.3f}%) "
                            f"between {rows[1]['timestamp']} and {rows[0]['timestamp']}",
                            "warning")
                alerts_raised += 1

        # Weekly move: latest official close vs official close ~7 days prior.
        cur.execute(
            """SELECT timestamp, yield FROM us_treasury_yields
               WHERE tenor='30Y' AND is_official_close=1 ORDER BY timestamp DESC LIMIT 1"""
        )
        latest = cur.fetchone()
        if latest:
            week_ago = (date.fromisoformat(latest["timestamp"][:10]) - timedelta(days=7)).isoformat()
            cur.execute(
                """SELECT timestamp, yield FROM us_treasury_yields
                   WHERE tenor='30Y' AND is_official_close=1 AND timestamp <= ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (week_ago,),
            )
            prior = cur.fetchone()
            if prior:
                move_bps = abs(latest["yield"] - prior["yield"]) * 100
                if move_bps >= ALERT_THRESHOLDS["treasury_30y_weekly_bps"]:
                    _log_alert("yield_move",
                                f"US 30Y moved {move_bps:.1f}bps week-over-week "
                                f"({prior['yield']:.3f}% on {prior['timestamp']} -> {latest['yield']:.3f}% on {latest['timestamp']})",
                                "critical")
                    alerts_raised += 1
    return alerts_raised


def check_boj_decision() -> int:
    alerts_raised = 0
    with db_cursor() as cur:
        cur.execute(
            """SELECT date, rate_before, rate_after, statement_tone FROM boj_events
               WHERE event_type='rate_decision' ORDER BY date DESC LIMIT 1"""
        )
        latest = cur.fetchone()
        if not latest:
            return 0
        changed = latest["rate_before"] is not None and latest["rate_after"] != latest["rate_before"]

        # Already alerted for this exact decision?
        cur.execute(
            "SELECT id FROM alerts_log WHERE alert_type='boj_decision' AND message LIKE ?",
            (f"%{latest['date']}%",),
        )
        if cur.fetchone():
            return 0

        if changed and ALERT_THRESHOLDS["boj_alert_on_any_change"]:
            _log_alert("boj_decision",
                        f"BOJ changed policy rate on {latest['date']}: {latest['rate_before']}% -> {latest['rate_after']}% "
                        f"(tone: {latest['statement_tone']})",
                        "critical")
            alerts_raised += 1
        elif not changed and ALERT_THRESHOLDS["boj_alert_on_hold_streak_break"]:
            cur.execute(
                """SELECT COUNT(*) as n FROM boj_events
                   WHERE event_type='rate_decision' AND rate_before=rate_after AND date < ?""",
                (latest["date"],),
            )
            hold_streak = cur.fetchone()["n"]
            if hold_streak >= 2:  # a hold following an established hold streak is lower-signal; still logged, info severity
                _log_alert("boj_decision",
                            f"BOJ held rate at {latest['rate_after']}% on {latest['date']} "
                            f"(tone: {latest['statement_tone']}) -- {hold_streak} consecutive holds",
                            "info")
                alerts_raised += 1
    return alerts_raised


def check_new_issuance() -> int:
    alerts_raised = 0
    cutoff = (date.today() - timedelta(days=ALERT_THRESHOLDS["new_issuance_lookback_days"])).isoformat()
    with db_cursor() as cur:
        cur.execute(
            """SELECT instrument_id, entity_id, principal, coupon_rate, maturity_date, first_seen_date
               FROM debt_instruments WHERE first_seen_date >= ?""",
            (cutoff,),
        )
        for row in cur.fetchall():
            cur.execute(
                "SELECT id FROM alerts_log WHERE alert_type='new_issuance' AND message LIKE ?",
                (f"%instrument_id={row['instrument_id']}%",),
            )
            if cur.fetchone():
                continue
            _log_alert("new_issuance",
                        f"{row['entity_id']} new debt instrument detected "
                        f"(instrument_id={row['instrument_id']}): ${row['principal']:,.0f} @ {row['coupon_rate']}% "
                        f"due {row['maturity_date']}",
                        "warning", entity_id=row["entity_id"])
            alerts_raised += 1
    return alerts_raised


def check_collector_health() -> int:
    """Surface recent collector failures as info/warning alerts so scrape
    breakage (7.2) is visible in the activity feed, not just a silent log row."""
    alerts_raised = 0
    with db_cursor() as cur:
        cur.execute(
            """SELECT id, collector_name, error_message, started_at FROM collector_runs
               WHERE status='failed' AND started_at >= datetime('now', '-1 day')
               ORDER BY started_at DESC"""
        )
        for row in cur.fetchall():
            cur.execute(
                "SELECT id FROM alerts_log WHERE alert_type='collector_failure' AND message LIKE ?",
                (f"%run_id={row['id']}%",),
            )
            if cur.fetchone():
                continue
            _log_alert("collector_failure",
                        f"Collector '{row['collector_name']}' failed at {row['started_at']} "
                        f"(run_id={row['id']}): {row['error_message']}",
                        "warning")
            alerts_raised += 1
    return alerts_raised


def run_all() -> dict:
    return {
        "yield_moves": check_treasury_yield_moves(),
        "boj_decision": check_boj_decision(),
        "new_issuance": check_new_issuance(),
        "collector_health": check_collector_health(),
    }


if __name__ == "__main__":
    results = run_all()
    print(results)
