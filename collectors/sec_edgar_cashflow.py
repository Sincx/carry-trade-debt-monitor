"""
Public-company cash flow collector via SEC EDGAR XBRL company facts (3.4, 4).

Structured tier only (STRUCTURED_ENTITY_IDS). Pulls operating cash flow,
capex, and cash & equivalents per reporting period, computes free cash flow,
and keeps the trailing CASHFLOW_HISTORY_QUARTERS worth of periods (7.7).
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import ENTITIES, CASHFLOW_HISTORY_QUARTERS
from config.xbrl_tags import CASHFLOW_CONCEPTS
from collectors.sec_edgar_common import fetch_company_facts, resolve_tag
from collectors.http_utils import BlockedDomainError
from db.connection import db_cursor, log_collector_start, log_collector_end


def _index_by_period(values: list) -> dict:
    """Map period end date -> value, preferring 10-K/10-Q duration-appropriate entries."""
    out = {}
    for v in values:
        end = v.get("end")
        form = v.get("form", "")
        if not end or form not in ("10-K", "10-Q"):
            continue
        # Prefer the entry filed most recently for a given period end (handles restatements).
        prev = out.get(end)
        if prev is None or v.get("filed", "") > prev.get("filed", ""):
            out[end] = v
    return out


def collect_entity_cashflow(entity: dict) -> int:
    cik = entity["cik"]
    entity_id = entity["entity_id"]
    facts = fetch_company_facts(cik)

    op_tag, op_values = resolve_tag(facts, CASHFLOW_CONCEPTS["operating_cash_flow"])
    capex_tag, capex_values = resolve_tag(facts, CASHFLOW_CONCEPTS["capex"])
    cash_tag, cash_values = resolve_tag(facts, CASHFLOW_CONCEPTS["cash_and_equivalents"])

    op_by_period = _index_by_period(op_values)
    capex_by_period = _index_by_period(capex_values)
    cash_by_period = _index_by_period(cash_values)

    all_periods = sorted(set(op_by_period) | set(capex_by_period) | set(cash_by_period), reverse=True)
    cutoff = date.today() - timedelta(days=CASHFLOW_HISTORY_QUARTERS * 95)  # ~quarter length, with slack

    rows_written = 0
    with db_cursor() as cur:
        for period_end in all_periods:
            try:
                if date.fromisoformat(period_end) < cutoff:
                    continue
            except ValueError:
                continue

            op = op_by_period.get(period_end, {})
            capex = capex_by_period.get(period_end, {})
            cash = cash_by_period.get(period_end, {})

            op_val = op.get("val")
            capex_val = capex.get("val")
            cash_val = cash.get("val")
            fcf = (op_val - capex_val) if (op_val is not None and capex_val is not None) else None

            form = op.get("form") or capex.get("form") or cash.get("form")
            accn = op.get("accn") or capex.get("accn") or cash.get("accn")
            source_url = (
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K"
                if not accn else
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/"
            )

            cur.execute(
                """INSERT INTO cashflow_statements
                     (entity_id, period_end, fiscal_period_type, operating_cash_flow, capex,
                      free_cash_flow, cash_and_equivalents, source_filing_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id, period_end) DO UPDATE SET
                     operating_cash_flow=excluded.operating_cash_flow, capex=excluded.capex,
                     free_cash_flow=excluded.free_cash_flow, cash_and_equivalents=excluded.cash_and_equivalents,
                     source_filing_url=excluded.source_filing_url""",
                (entity_id, period_end, form, op_val, capex_val, fcf, cash_val, source_url),
            )
            rows_written += 1

    return rows_written


def collect_all():
    run_id = log_collector_start("sec_edgar_cashflow")
    total = 0
    errors = []
    for entity in ENTITIES:
        if entity["data_tier"] != "structured" or not entity.get("cik"):
            continue
        try:
            n = collect_entity_cashflow(entity)
            total += n
            print(f"  {entity['entity_id']}: {n} periods")
        except BlockedDomainError as e:
            errors.append(f"{entity['entity_id']}: {e}")
            print(f"  {entity['entity_id']}: BLOCKED -- {e}")
        except Exception as e:
            errors.append(f"{entity['entity_id']}: {type(e).__name__}: {e}")
            print(f"  {entity['entity_id']}: FAILED -- {e}")

    status = "success" if not errors else ("partial" if total else "failed")
    log_collector_end(run_id, status, total, "; ".join(errors) if errors else None)
    return total


if __name__ == "__main__":
    n = collect_all()
    print(f"Cash flow collector: wrote/updated {n} rows total.")
