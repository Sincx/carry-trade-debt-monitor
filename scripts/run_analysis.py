"""
Nightly analysis + alerting job (Section 8, Phase 3/4).
Recomputes correlation/refinancing-stress and evaluates alert thresholds.
Run this AFTER the day's collectors so it sees fresh data.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.correlation import run_full_analysis
from analysis.refinancing_stress import compute_refinancing_stress
from analysis.entity_financial_summary import compute_entity_financial_summary
from alerts.engine import run_all as run_alerts


def main():
    print("=== Correlation analysis ===")
    results = run_full_analysis()
    print(f"{len(results)} correlation series computed.")

    print("=== Refinancing stress ===")
    n = compute_refinancing_stress()
    print(f"{n} entity/year refinancing-stress rows.")

    print("=== Entity financial summary (debt/capex/cashflow) ===")
    n = compute_entity_financial_summary()
    print(f"{n} entity financial summary rows.")

    print("=== Alert evaluation ===")
    print(run_alerts())


if __name__ == "__main__":
    main()
