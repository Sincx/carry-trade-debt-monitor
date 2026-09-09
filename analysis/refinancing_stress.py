"""
Refinancing stress indicator (spec 3.5.3): for each entity and future
maturity year, combine the maturity ladder with the prevailing US 30Y yield
to estimate the cost delta if that year's maturing principal were rolled at
current rates instead of its original weighted-average coupon.

This is a simple, transparent estimate -- not a real refinancing model (it
ignores instrument-specific tenor/credit spread at refinancing) -- and is
labeled as such in the dashboard.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.connection import db_cursor


def compute_refinancing_stress():
    today = date.today().isoformat()
    with db_cursor() as cur:
        cur.execute(
            "SELECT yield FROM us_treasury_yields WHERE tenor='30Y' AND is_official_close=1 ORDER BY timestamp DESC LIMIT 1"
        )
        row = cur.fetchone()
        current_30y = row["yield"] if row else None
        if current_30y is None:
            return 0

        cur.execute(
            """SELECT entity_id,
                      CAST(strftime('%Y', maturity_date) AS INTEGER) AS maturity_year,
                      SUM(principal) AS principal_maturing,
                      SUM(principal * coupon_rate) / NULLIF(SUM(principal), 0) AS weighted_avg_coupon
               FROM debt_instruments
               WHERE status='open' AND maturity_date IS NOT NULL AND principal IS NOT NULL AND coupon_rate IS NOT NULL
               GROUP BY entity_id, maturity_year"""
        )
        rows = cur.fetchall()

        run_date = today
        written = 0
        for r in rows:
            delta_bps = (current_30y - r["weighted_avg_coupon"]) * 100
            annual_cost_delta = r["principal_maturing"] * (delta_bps / 10000)
            cur.execute(
                """INSERT INTO refinancing_stress
                     (run_date, entity_id, maturity_year, principal_maturing,
                      weighted_avg_original_coupon, current_30y_yield,
                      estimated_coupon_delta_bps, estimated_annual_cost_delta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_date, entity_id, maturity_year) DO UPDATE SET
                     principal_maturing=excluded.principal_maturing,
                     weighted_avg_original_coupon=excluded.weighted_avg_original_coupon,
                     current_30y_yield=excluded.current_30y_yield,
                     estimated_coupon_delta_bps=excluded.estimated_coupon_delta_bps,
                     estimated_annual_cost_delta=excluded.estimated_annual_cost_delta""",
                (run_date, r["entity_id"], r["maturity_year"], r["principal_maturing"],
                 r["weighted_avg_coupon"], current_30y, delta_bps, annual_cost_delta),
            )
            written += 1
        return written


if __name__ == "__main__":
    n = compute_refinancing_stress()
    print(f"Refinancing stress: wrote/updated {n} entity/year rows.")
