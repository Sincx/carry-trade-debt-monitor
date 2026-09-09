"""
FastAPI dashboard (spec 3.6, Section 8).

Reads only from SQLite -- never recomputes correlation/refinancing-stress
live (those are pre-computed by the nightly analysis job and read from
correlation_results / refinancing_stress, per Section 8's design principle).
"""
import sys
from pathlib import Path
from datetime import date, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.connection import get_connection

APP_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Carry Trade / US 30Y / AI Infra Debt Monitor")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def q(sql, params=()):
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def q_one(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# Data assembly
# --------------------------------------------------------------------------

def get_current_levels():
    us30y_close = q_one(
        "SELECT timestamp, yield FROM us_treasury_yields WHERE tenor='30Y' AND is_official_close=1 ORDER BY timestamp DESC LIMIT 1"
    )
    us30y_intraday = q_one(
        "SELECT timestamp, yield FROM us_treasury_yields WHERE tenor='30Y' AND is_official_close=0 ORDER BY timestamp DESC LIMIT 1"
    )
    boj = q_one(
        "SELECT date, rate_after, statement_tone FROM boj_events WHERE event_type='rate_decision' ORDER BY date DESC LIMIT 1"
    )
    usdjpy = q_one("SELECT timestamp, rate FROM fx_rates WHERE pair='USDJPY' ORDER BY timestamp DESC LIMIT 1")
    cpi_rows = q(
        "SELECT date, value FROM japan_macro_indicators WHERE indicator='cpi_headline_index' ORDER BY date DESC LIMIT 13"
    )
    cpi_yoy = None
    if len(cpi_rows) >= 13:
        cpi_yoy = round((cpi_rows[0]["value"] / cpi_rows[12]["value"] - 1) * 100, 2)
    next_meeting = q_one(
        "SELECT date FROM boj_events WHERE is_upcoming=1 AND date >= ? ORDER BY date ASC LIMIT 1",
        (date.today().isoformat(),),
    )
    return {
        "us30y_close": us30y_close, "us30y_intraday": us30y_intraday,
        "boj": boj, "usdjpy": usdjpy, "cpi_yoy": cpi_yoy,
        "next_boj_meeting": next_meeting,
    }


def get_trend_series():
    cutoff = (date.today() - timedelta(days=180)).isoformat()
    return {
        "us30y": q(
            "SELECT timestamp as d, yield as v FROM us_treasury_yields WHERE tenor='30Y' AND is_official_close=1 AND timestamp>=? ORDER BY timestamp",
            (cutoff,),
        ),
        "usdjpy": q(
            "SELECT timestamp as d, rate as v FROM fx_rates WHERE pair='USDJPY' AND timestamp>=? ORDER BY timestamp",
            (cutoff,),
        ),
        "cpi": q(
            "SELECT date as d, value as v FROM japan_macro_indicators WHERE indicator='cpi_headline_index' AND date>=? ORDER BY date",
            (cutoff,),
        ),
        "boj_rate": q(
            "SELECT date as d, rate_after as v FROM boj_events WHERE event_type='rate_decision' AND date>=? ORDER BY date",
            (cutoff,),
        ),
    }


def get_correlation_panel():
    rows = q(
        """SELECT * FROM correlation_results
           WHERE run_date = (SELECT MAX(run_date) FROM correlation_results)
           ORDER BY series_a, series_b, window_days"""
    )
    return rows


def get_maturity_ladder():
    rows = q(
        """SELECT entity_id, CAST(strftime('%Y', maturity_date) AS INTEGER) AS maturity_year, SUM(principal) AS total_principal
           FROM debt_instruments
           WHERE status='open' AND maturity_date IS NOT NULL
           GROUP BY entity_id, maturity_year
           ORDER BY maturity_year"""
    )
    years = sorted(set(r["maturity_year"] for r in rows))
    entities = sorted(set(r["entity_id"] for r in rows))
    by_year_total = {y: 0 for y in years}
    for r in rows:
        by_year_total[r["maturity_year"]] += r["total_principal"] or 0
    return {"rows": rows, "years": years, "entities": entities, "by_year_total": by_year_total}


def get_activity_feed(limit=40):
    return q("SELECT * FROM alerts_log ORDER BY timestamp DESC LIMIT ?", (limit,))


def get_entities():
    return q("SELECT * FROM entities ORDER BY category, name")


def get_financial_summary():
    """Debt outstanding, near-term maturity concentration, and capex/OCF
    ratio per entity -- pre-computed by analysis/entity_financial_summary.py.
    Sorted by capex/OCF ratio descending: the research model behind this
    panel (2026-09-10) found that ratio the single clearest differentiator
    of debt-market rate exposure across the tracked universe."""
    return q(
        """SELECT s.*, e.name, e.category FROM entity_financial_summary s
           JOIN entities e ON e.entity_id = s.entity_id
           WHERE s.run_date = (SELECT MAX(run_date) FROM entity_financial_summary)
           ORDER BY s.capex_ocf_ratio DESC NULLS LAST"""
    )


def get_collector_health():
    return q(
        """SELECT collector_name, status, started_at, finished_at, rows_written, error_message
           FROM collector_runs
           WHERE id IN (SELECT MAX(id) FROM collector_runs GROUP BY collector_name)
           ORDER BY collector_name"""
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "levels": get_current_levels(),
        "trends": get_trend_series(),
        "correlations": get_correlation_panel(),
        "ladder": get_maturity_ladder(),
        "financial_summary": get_financial_summary(),
        "activity": get_activity_feed(),
        "entities": get_entities(),
        "collector_health": get_collector_health(),
    })


@app.get("/entity/{entity_id}", response_class=HTMLResponse)
def entity_detail(request: Request, entity_id: str):
    entity = q_one("SELECT * FROM entities WHERE entity_id=?", (entity_id,))
    debt = q(
        "SELECT * FROM debt_instruments WHERE entity_id=? ORDER BY status, maturity_date",
        (entity_id,),
    )
    cashflow = q(
        "SELECT * FROM cashflow_statements WHERE entity_id=? ORDER BY period_end DESC LIMIT 8",
        (entity_id,),
    )
    reported = q(
        "SELECT * FROM reported_events WHERE entity_id=? ORDER BY event_date DESC LIMIT 30",
        (entity_id,),
    )
    refi = q(
        """SELECT * FROM refinancing_stress
           WHERE entity_id=? AND run_date=(SELECT MAX(run_date) FROM refinancing_stress WHERE entity_id=?)
           ORDER BY maturity_year""",
        (entity_id, entity_id),
    )
    summary = q_one(
        """SELECT * FROM entity_financial_summary
           WHERE entity_id=? AND run_date=(SELECT MAX(run_date) FROM entity_financial_summary WHERE entity_id=?)""",
        (entity_id, entity_id),
    )
    # Full-fiscal-year-only series for the cash flow trend chart -- 10-Q rows
    # in this schema are YTD-cumulative, not discrete quarters, so mixing
    # them into one chronological line would visually fake a trend.
    cashflow_trend = q(
        """SELECT period_end, operating_cash_flow, capex, free_cash_flow FROM cashflow_statements
           WHERE entity_id=? AND fiscal_period_type='10-K' ORDER BY period_end""",
        (entity_id,),
    )
    return templates.TemplateResponse(request, "entity.html", {
        "entity": entity, "debt": debt,
        "cashflow": cashflow, "reported": reported, "refi": refi,
        "summary": summary, "cashflow_trend": cashflow_trend,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.app:app", host="127.0.0.1", port=8420, reload=False)
