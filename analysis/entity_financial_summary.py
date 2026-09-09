"""
Per-entity debt outstanding, near-term maturity concentration, and CAPEX/
cash-flow trend summary (spec extension, 2026-09-10 -- see wiki research
model "Hyperscaler & Neocloud Debt Maturity, CAPEX Trajectory & US Treasury
Rate-Exposure Ranking" this was built from).

Two distinct exposure channels, kept separate rather than blended into one
opaque score (a deliberate choice carried over from that research -- see its
Section D9 recommendation #5): near-term debt that must be refinanced
(Channel A), and ongoing capex that already exceeds operating cash flow,
meaning new debt/equity must be raised regardless of what maturities look
like (Channel B).

CAPEX projection honesty note: verified YoY capex growth across this
tracked universe runs 55-160%+ (a step-change catch-up to AI-infrastructure
demand, not a stable trend). Naively extrapolating that forward compounds
into obviously-wrong multi-year figures. This module still computes a
single-year trend-implied projection (some viewers will want *a* number),
but flags projection_reliable=0 whenever YoY growth exceeds
UNRELIABLE_GROWTH_THRESHOLD, and the dashboard must surface that flag
prominently rather than presenting the number as if it were guidance.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.connection import db_cursor

NEAR_TERM_YEARS = 4  # matches the research model's 2026-2029-style window, rolled forward from today
# % YoY -- above this, don't present the projection as informative. Set low
# (not e.g. 100%) deliberately: the 2026-09-10 research model found that even
# the *slowest* capex grower in this tracked universe (Amazon, +59% YoY)
# produces an internally-inconsistent extrapolated figure two years out --
# a normal mature-company capex/revenue relationship grows in the
# high-single/low-double digits, so anything well above that reflects a
# step-change buildout phase, not a trend safe to project forward.
UNRELIABLE_GROWTH_THRESHOLD = 40.0


def _debt_summary(cur, entity_id: str) -> dict:
    cur.execute(
        """SELECT SUM(principal) AS total, SUM(principal * coupon_rate) / NULLIF(SUM(principal), 0) AS wavg_coupon
           FROM debt_instruments WHERE entity_id=? AND status='open'""",
        (entity_id,),
    )
    row = cur.fetchone()
    total = row["total"] or 0
    wavg_coupon = row["wavg_coupon"]
    is_itemized = 1

    if not total:
        # No itemized instruments -- could mean "genuinely no debt" (e.g.
        # Palantir) or "debt exists but this filer's note format isn't
        # itemized yet" (e.g. CoreWeave, 7.3). Fall back to the XBRL
        # aggregate snapshot so the two cases aren't shown identically.
        cur.execute("SELECT noncurrent, current FROM entity_debt_aggregate WHERE entity_id=?", (entity_id,))
        agg_row = cur.fetchone()
        if agg_row and ((agg_row["noncurrent"] or 0) + (agg_row["current"] or 0)) > 0:
            total = (agg_row["noncurrent"] or 0) + (agg_row["current"] or 0)
            wavg_coupon = None  # not derivable from an aggregate balance-sheet figure
            is_itemized = 0

    near_term = None
    near_term_pct = None
    if is_itemized and total:
        near_term_end = (date.today().replace(day=1) + timedelta(days=365 * NEAR_TERM_YEARS)).isoformat()
        cur.execute(
            """SELECT SUM(principal) AS near_term FROM debt_instruments
               WHERE entity_id=? AND status='open' AND maturity_date IS NOT NULL AND maturity_date <= ?""",
            (entity_id, near_term_end),
        )
        near_term = cur.fetchone()["near_term"] or 0
        near_term_pct = (near_term / total * 100) if total else None

    return {
        "total_debt_outstanding": total,
        "weighted_avg_coupon": wavg_coupon,
        "near_term_maturity_amount": near_term,
        "near_term_maturity_pct": near_term_pct,
        "is_itemized": is_itemized,
    }


def _capex_summary(cur, entity_id: str) -> dict:
    # Only 10-K (full fiscal year) rows -- 10-Q figures in this schema are
    # fiscal-year-to-date cumulative, not discrete quarters (confirmed
    # against live SEC XBRL data), so mixing them into a YoY comparison
    # would silently compare unlike periods.
    cur.execute(
        """SELECT period_end, capex, operating_cash_flow, free_cash_flow FROM cashflow_statements
           WHERE entity_id=? AND fiscal_period_type='10-K' ORDER BY period_end DESC LIMIT 2""",
        (entity_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return {
            "latest_fy_period_end": None, "latest_fy_capex": None, "latest_fy_ocf": None, "latest_fy_fcf": None,
            "prior_fy_capex": None, "capex_yoy_growth_pct": None, "capex_ocf_ratio": None,
            "projected_next_fy_capex": None, "projection_reliable": 1,
        }

    latest = rows[0]
    prior = rows[1] if len(rows) > 1 else None

    capex_ocf_ratio = None
    if latest["capex"] is not None and latest["operating_cash_flow"]:
        capex_ocf_ratio = latest["capex"] / latest["operating_cash_flow"] * 100

    growth_pct = None
    projected = None
    reliable = 1
    if prior and prior["capex"] and latest["capex"] is not None:
        growth_pct = (latest["capex"] / prior["capex"] - 1) * 100
        projected = latest["capex"] * (1 + growth_pct / 100)
        reliable = 0 if abs(growth_pct) > UNRELIABLE_GROWTH_THRESHOLD else 1

    return {
        "latest_fy_period_end": latest["period_end"],
        "latest_fy_capex": latest["capex"],
        "latest_fy_ocf": latest["operating_cash_flow"],
        "latest_fy_fcf": latest["free_cash_flow"],
        "prior_fy_capex": prior["capex"] if prior else None,
        "capex_yoy_growth_pct": growth_pct,
        "capex_ocf_ratio": capex_ocf_ratio,
        "projected_next_fy_capex": projected,
        "projection_reliable": reliable,
    }


def compute_entity_financial_summary() -> int:
    today = date.today().isoformat()
    written = 0
    with db_cursor() as cur:
        cur.execute("SELECT entity_id FROM entities WHERE data_tier='structured'")
        entity_ids = [r["entity_id"] for r in cur.fetchall()]

        for entity_id in entity_ids:
            debt = _debt_summary(cur, entity_id)
            capex = _capex_summary(cur, entity_id)
            if not debt["total_debt_outstanding"] and not capex["latest_fy_capex"]:
                continue  # nothing to summarize for this entity yet

            cur.execute(
                """INSERT INTO entity_financial_summary
                     (run_date, entity_id, total_debt_outstanding, weighted_avg_coupon,
                      near_term_maturity_amount, near_term_maturity_pct,
                      latest_fy_period_end, latest_fy_capex, latest_fy_ocf, latest_fy_fcf,
                      prior_fy_capex, capex_yoy_growth_pct, capex_ocf_ratio,
                      projected_next_fy_capex, projection_reliable, is_itemized)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_date, entity_id) DO UPDATE SET
                     total_debt_outstanding=excluded.total_debt_outstanding,
                     weighted_avg_coupon=excluded.weighted_avg_coupon,
                     near_term_maturity_amount=excluded.near_term_maturity_amount,
                     near_term_maturity_pct=excluded.near_term_maturity_pct,
                     latest_fy_period_end=excluded.latest_fy_period_end,
                     latest_fy_capex=excluded.latest_fy_capex,
                     latest_fy_ocf=excluded.latest_fy_ocf,
                     latest_fy_fcf=excluded.latest_fy_fcf,
                     prior_fy_capex=excluded.prior_fy_capex,
                     capex_yoy_growth_pct=excluded.capex_yoy_growth_pct,
                     capex_ocf_ratio=excluded.capex_ocf_ratio,
                     projected_next_fy_capex=excluded.projected_next_fy_capex,
                     projection_reliable=excluded.projection_reliable,
                     is_itemized=excluded.is_itemized""",
                (today, entity_id, debt["total_debt_outstanding"], debt["weighted_avg_coupon"],
                 debt["near_term_maturity_amount"], debt["near_term_maturity_pct"],
                 capex["latest_fy_period_end"], capex["latest_fy_capex"], capex["latest_fy_ocf"], capex["latest_fy_fcf"],
                 capex["prior_fy_capex"], capex["capex_yoy_growth_pct"], capex["capex_ocf_ratio"],
                 capex["projected_next_fy_capex"], capex["projection_reliable"], debt["is_itemized"]),
            )
            written += 1
    return written


if __name__ == "__main__":
    n = compute_entity_financial_summary()
    print(f"Entity financial summary: wrote/updated {n} rows.")
