import csv
from pathlib import Path

from strategy_core import now_ist

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.csv"

COLUMNS = [
    "trade_date",
    "symbol",
    "trading_symbol",
    "direction",
    "quantity",
    "entry_time",
    "entry_price",
    "exit_time",
    "exit_price",
    "target_price",
    "stop_loss_price",
    "exit_reason",
    "gross_pnl",
    "status",
]


def ensure_trade_history_file():
    DATA_DIR.mkdir(exist_ok=True)

    if not TRADE_HISTORY_FILE.exists():
        with TRADE_HISTORY_FILE.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()


def record_closed_trade(state, exit_price, exit_reason):
    ensure_trade_history_file()

    qty = int(float(state.get("quantity") or 0))
    entry_price = float(state.get("entry_price") or 0)
    exit_price = float(exit_price or 0)
    gross_pnl = round((exit_price - entry_price) * qty, 2)

    row = {
        "trade_date": now_ist().strftime("%Y-%m-%d"),
        "symbol": state.get("symbol", ""),
        "trading_symbol": state.get("trading_symbol", ""),
        "direction": state.get("direction", ""),
        "quantity": qty,
        "entry_time": state.get("created_at", ""),
        "entry_price": entry_price,
        "exit_time": now_ist().isoformat(),
        "exit_price": exit_price,
        "target_price": state.get("target_price", ""),
        "stop_loss_price": state.get("stop_loss_price", ""),
        "exit_reason": exit_reason,
        "gross_pnl": gross_pnl,
        "status": "CLOSED",
    }

    with TRADE_HISTORY_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writerow(row)

    return row