import csv
import json
from pathlib import Path

from strategy_core import now_ist

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ANALYSIS_FILE = DATA_DIR / "analysis_history.csv"

COLUMNS = [
    "timestamp",
    "symbol",
    "option_chain_bias",
    "option_chain_confidence",
    "four_hour_bias",
    "four_hour_confidence",
    "fifteen_min_bias",
    "fifteen_min_confidence",
    "llm_execute_trade",
    "llm_confidence",
    "llm_target_price",
    "llm_stop_loss_price",
    "llm_reason",
    "raw_json",
]


def ensure_file():
    DATA_DIR.mkdir(exist_ok=True)
    if not ANALYSIS_FILE.exists():
        with ANALYSIS_FILE.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=COLUMNS).writeheader()


def record_analysis(symbol, option_summary, technicals, llm_decision):
    ensure_file()

    row = {
        "timestamp": now_ist().isoformat(),
        "symbol": symbol,
        "option_chain_bias": option_summary.get("bias"),
        "option_chain_confidence": option_summary.get("confidence"),
        "four_hour_bias": technicals.get("four_hour", {}).get("bias"),
        "four_hour_confidence": technicals.get("four_hour", {}).get("confidence"),
        "fifteen_min_bias": technicals.get("fifteen_min", {}).get("bias"),
        "fifteen_min_confidence": technicals.get("fifteen_min", {}).get("confidence"),
        "llm_execute_trade": llm_decision.get("execute_trade"),
        "llm_confidence": llm_decision.get("confidence"),
        "llm_target_price": llm_decision.get("target_price"),
        "llm_stop_loss_price": llm_decision.get("stop_loss_price"),
        "llm_reason": llm_decision.get("reason"),
        "raw_json": json.dumps(
            {
                "option_summary": option_summary,
                "technicals": technicals,
                "llm_decision": llm_decision,
            },
            sort_keys=True,
        ),
    }

    with ANALYSIS_FILE.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writerow(row)

    return row