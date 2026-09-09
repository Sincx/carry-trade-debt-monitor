"""Single US 30Y intraday snapshot. Schedule 4-5x on trading days (e.g. 9:35, 11:00, 13:00, 15:30, 16:15 ET)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors import treasury_yields
from alerts import engine as alert_engine

if __name__ == "__main__":
    try:
        y = treasury_yields.collect_intraday_snapshot()
        print(f"30Y intraday snapshot: {y}%")
    except Exception as e:
        print(f"FAILED: {e}")
    print(alert_engine.check_treasury_yield_moves())
