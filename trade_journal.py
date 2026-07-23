import csv
from pathlib import Path

from strategy_core import now_ist

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.csv"

COLUMNS = [
    "trade_date",
    "symbol",
    "underlying_symbol",
    "instrument_class",
    "trading_symbol",
    "direction",
    "transaction_type",
    "position_side",
    "quantity",
    "entry_time",
    "entry_price",
    "exit_time",
    "exit_price",
    "target_price",
    "stop_loss_price",
    "original_stop_loss_price",
    "profit_protection_stage",
    "profit_booking_price",
    "exit_reason",
    "gross_pnl",
    "score",
    "trade_sequence",
    "prior_trade_symbol",
    "prior_trade_outcome",
    "prior_trade_pnl",
    "risk_per_trade_limit",
    "remaining_index_risk_budget",
    "planned_risk",
    "status",
]


def ensure_trade_history_file():
    DATA_DIR.mkdir(exist_ok=True)

    if not TRADE_HISTORY_FILE.exists():
        with TRADE_HISTORY_FILE.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
        return

    with TRADE_HISTORY_FILE.open("r", newline="") as f:
        reader = csv.DictReader(f)
        existing_fields = reader.fieldnames or []
        rows = list(reader)

    if existing_fields != COLUMNS:
        migrated = [{column: row.get(column, "") for column in COLUMNS} for row in rows]
        temporary = TRADE_HISTORY_FILE.with_suffix(".tmp")
        with temporary.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(migrated)
        temporary.replace(TRADE_HISTORY_FILE)


def record_closed_trade(state, exit_price, exit_reason):
    ensure_trade_history_file()

    qty = int(float(state.get("quantity") or 0))
    entry_price = float(state.get("entry_price") or 0)
    exit_price = float(exit_price or 0)
    transaction_type = str(state.get("entry_transaction_type") or "BUY").upper()
    pnl_per_unit = (
        entry_price - exit_price
        if transaction_type == "SELL"
        else exit_price - entry_price
    )
    gross_pnl = round(pnl_per_unit * qty, 2)

    row = {
        "trade_date": now_ist().strftime("%Y-%m-%d"),
        "symbol": state.get("symbol", ""),
        "underlying_symbol": state.get("underlying_symbol", state.get("symbol", "")),
        "instrument_class": state.get("instrument_class", "INDEX_OPTION"),
        "trading_symbol": state.get("trading_symbol", ""),
        "direction": state.get("direction", ""),
        "transaction_type": transaction_type,
        "position_side": state.get(
            "position_side",
            "SHORT_OPTION" if transaction_type == "SELL" else "LONG_OPTION",
        ),
        "quantity": qty,
        "entry_time": state.get("created_at", ""),
        "entry_price": entry_price,
        "exit_time": now_ist().isoformat(),
        "exit_price": exit_price,
        "target_price": state.get("target_price", ""),
        "stop_loss_price": state.get("stop_loss_price", ""),
        "original_stop_loss_price": state.get(
            "original_stop_loss_price", state.get("stop_loss_price", "")
        ),
        "profit_protection_stage": state.get("profit_protection_stage", 0),
        "profit_booking_price": state.get("profit_booking_price", ""),
        "exit_reason": exit_reason,
        "gross_pnl": gross_pnl,
        "score": state.get("score", ""),
        "trade_sequence": state.get("trade_sequence", ""),
        "prior_trade_symbol": state.get("prior_trade_symbol", ""),
        "prior_trade_outcome": state.get("prior_trade_outcome", ""),
        "prior_trade_pnl": state.get("prior_trade_pnl", ""),
        "risk_per_trade_limit": state.get("risk_per_trade_limit", ""),
        "remaining_index_risk_budget": state.get("remaining_index_risk_budget", ""),
        "planned_risk": state.get("planned_risk", ""),
        "status": "CLOSED",
    }

    with TRADE_HISTORY_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writerow(row)

    return row
