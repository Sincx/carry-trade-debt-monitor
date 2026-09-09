"""Run the macro/rates collectors (Phase 1). Intended cadence: daily.
Treasury intraday snapshots run separately via run_intraday_snapshot.py, 4-5x/trading day.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from collectors import treasury_yields, japan_macro, boj_events, fx_rates


def main():
    print("=== Treasury official close ===")
    try:
        print(treasury_yields.collect_official_close())
    except Exception as e:
        print(f"FAILED: {e}")

    print("=== Japan CPI ===")
    try:
        print(japan_macro.collect_cpi())
    except Exception as e:
        print(f"FAILED: {e}")

    print("=== BOJ schedule + latest decision ===")
    try:
        print(boj_events.collect_upcoming_meetings())
        print(boj_events.collect_latest_decision())
    except Exception as e:
        print(f"FAILED: {e}")

    print("=== FX (USD/JPY + rate differential) ===")
    try:
        print(fx_rates.collect_usdjpy_history())
        print(fx_rates.compute_rate_differential())
    except Exception as e:
        print(f"FAILED: {e}")


if __name__ == "__main__":
    main()
