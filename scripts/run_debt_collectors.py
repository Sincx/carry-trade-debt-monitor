"""Run the SEC EDGAR debt + cash flow collectors (Phase 2). Intended cadence: daily."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors import sec_edgar_debt, sec_edgar_cashflow, reported_events_feed


def main():
    print("=== SEC EDGAR debt ===")
    sec_edgar_debt.collect_all()
    print("=== SEC EDGAR cash flow ===")
    sec_edgar_cashflow.collect_all()
    print("=== Reported events (neocloud / AI-lab press feed) ===")
    reported_events_feed.collect_all()


if __name__ == "__main__":
    main()
