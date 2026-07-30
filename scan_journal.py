import csv
from pathlib import Path

from strategy_core import now_ist


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SCAN_FILE = DATA_DIR / "scan_decisions.csv"

COLUMNS = [
    "timestamp",
    "symbol",
    "score",
    "action",
]


def _ensure_file():
    DATA_DIR.mkdir(exist_ok=True)
    if not SCAN_FILE.exists():
        with SCAN_FILE.open("w", newline="") as handle:
            csv.DictWriter(handle, fieldnames=COLUMNS).writeheader()


def record_scan_decision(symbol, score, action, timestamp=None):
    _ensure_file()
    row = {
        "timestamp": (timestamp or now_ist()).isoformat(),
        "symbol": str(symbol or "").upper(),
        "score": score,
        "action": str(action or "").lower(),
    }
    with SCAN_FILE.open("a", newline="") as handle:
        csv.DictWriter(handle, fieldnames=COLUMNS).writerow(row)
    return row
