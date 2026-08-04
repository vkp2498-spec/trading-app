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
    "score_version",
    "action",
]


def _ensure_file():
    DATA_DIR.mkdir(exist_ok=True)
    if not SCAN_FILE.exists():
        with SCAN_FILE.open("w", newline="") as handle:
            csv.DictWriter(handle, fieldnames=COLUMNS).writeheader()
        return
    with SCAN_FILE.open("r", newline="") as handle:
        reader = csv.DictReader(handle)
        existing_fields = reader.fieldnames or []
        rows = list(reader)
    if existing_fields != COLUMNS:
        migrated = [{column: row.get(column, "") for column in COLUMNS} for row in rows]
        temporary = SCAN_FILE.with_suffix(".tmp")
        with temporary.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(migrated)
        temporary.replace(SCAN_FILE)


def record_scan_decision(symbol, score, action, timestamp=None, score_version=None):
    _ensure_file()
    row = {
        "timestamp": (timestamp or now_ist()).isoformat(),
        "symbol": str(symbol or "").upper(),
        "score": score,
        "score_version": str(score_version or ""),
        "action": str(action or "").lower(),
    }
    with SCAN_FILE.open("a", newline="") as handle:
        csv.DictWriter(handle, fieldnames=COLUMNS).writerow(row)
    return row
