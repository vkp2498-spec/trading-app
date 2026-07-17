from collections import deque
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import ast
import csv
import json
import os
import re

import requests


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

ENV_FILE = BASE_DIR / ".env"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.csv"
LOG_FILE = LOG_DIR / "trade_bot.log"
STOCK_SCANNER_STATUS_FILE = DATA_DIR / "stock_scanner_status.json"

SYMBOLS = ["NIFTY", "BANKNIFTY"]
STATE_SLOTS = SYMBOLS + ["STOCK_FUTURE"]

UPSTOX_POSITIONS_URL = (
    "https://api.upstox.com/v2/"
    "portfolio/short-term-positions"
)


def load_env():
    """
    Load values from .env without printing secrets.
    """
    if not ENV_FILE.exists():
        return

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)

        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


def file_status(path: Path) -> dict:
    return {
        "name": path.name,
        "exists": path.exists(),
        "sizeBytes": path.stat().st_size if path.exists() else 0,
    }


def read_last_lines(
    path: Path,
    max_lines: int = 2500,
) -> list[str]:
    """
    Read only the end of a potentially large log file.
    """
    if not path.exists():
        return []

    with path.open("r", errors="ignore") as file:
        return list(deque(file, maxlen=max_lines))


def safe_literal_dict(text: str) -> dict:
    """
    Safely parse dictionary text found in bot logs.

    ast.literal_eval does not execute arbitrary Python code.
    """
    try:
        value = ast.literal_eval(text)

        if isinstance(value, dict):
            return value

        return {}
    except (ValueError, SyntaxError):
        return {}


def extract_between(
    line: str,
    start: str,
    end: str,
) -> str:
    if start not in line:
        return ""

    part = line.split(start, 1)[1]

    if end and end in part:
        part = part.split(end, 1)[0]

    return part.strip()


def empty_symbol_status(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "lastUpdate": None,
        "status": "NO DATA",
        "signal": None,
        "confidence": None,
        "optionScore": None,
        "weightedScore": None,
        "weightedGrade": None,
        "atmOptionFlow": None,
        "reason": None,
        "position": None,
    }


def parse_latest_bot_status() -> dict:
    """
    Parse the latest status of NIFTY and BANKNIFTY from the bot log.

    Returns only dashboard-safe information.
    """
    statuses = {
        symbol: empty_symbol_status(symbol)
        for symbol in SYMBOLS
    }

    last_bot_log = None

    for raw_line in read_last_lines(LOG_FILE):
        line = raw_line.strip()

        timestamp_match = re.match(
            r"^(\d{4}-\d{2}-\d{2} "
            r"\d{2}:\d{2}:\d{2}) \|",
            line,
        )

        if timestamp_match:
            last_bot_log = timestamp_match.group(1)

        for symbol in SYMBOLS:
            if f"| {symbol} " not in line:
                continue

            item = statuses[symbol]

            if timestamp_match:
                item["lastUpdate"] = timestamp_match.group(1)

            signal_match = re.search(
                rf"{symbol} signal: "
                rf"([A-Z]+), "
                rf"confidence=([A-Z]+), "
                rf"score=([-0-9.]+)",
                line,
            )

            if signal_match:
                item["status"] = "SIGNAL CHECKED"
                item["signal"] = signal_match.group(1)
                item["confidence"] = signal_match.group(2)

                try:
                    item["optionScore"] = float(
                        signal_match.group(3)
                    )
                except ValueError:
                    item["optionScore"] = None

            if f"{symbol} no trade:" in line:
                item["status"] = "REJECTED"
                item["reason"] = line.split(
                    f"{symbol} no trade:",
                    1,
                )[1].strip()

            if f"{symbol} ERROR:" in line:
                item["status"] = "ERROR"
                item["reason"] = line.split(
                    f"{symbol} ERROR:",
                    1,
                )[1].strip()

            if f"{symbol} MARKET BUY placed" in line:
                item["status"] = "BOUGHT"

            if f"{symbol} POSITION OPEN:" in line:
                item["status"] = "OPEN"
                item["position"] = line.split(
                    f"{symbol} POSITION OPEN:",
                    1,
                )[1].strip()

            if f"{symbol} open position active:" in line:
                item["status"] = "OPEN"
                item["position"] = line.split(
                    f"{symbol} open position active:",
                    1,
                )[1].strip()

            if f"{symbol} TARGET exit" in line:
                item["status"] = "TARGET HIT"

            if f"{symbol} STOP_LOSS exit" in line:
                item["status"] = "STOP LOSS HIT"

            if f"{symbol} bot squareoff" in line:
                item["status"] = "SQUAREOFF"

            if f"{symbol} analysis:" in line:
                weighted_text = extract_between(
                    line,
                    "weighted=",
                    " llm=",
                )

                weighted = safe_literal_dict(
                    weighted_text
                )

                if weighted:
                    item["weightedScore"] = weighted.get(
                        "score"
                    )
                    item["weightedGrade"] = weighted.get(
                        "grade"
                    )

                flow_text = extract_between(
                    line,
                    "atm_option_flow=",
                    "",
                )

                flow = safe_literal_dict(flow_text)

                if flow:
                    item["atmOptionFlow"] = {
                        "bias": flow.get("bias"),
                        "close": flow.get("close"),
                        "vwap": flow.get("vwap"),
                        "volumeRatio": flow.get(
                            "volume_ratio"
                        ),
                    }

                llm_text = extract_between(
                    line,
                    "llm=",
                    " atm_option_flow=",
                )

                llm = safe_literal_dict(llm_text)

                if llm:
                    item["status"] = (
                        "APPROVED"
                        if llm.get("execute_trade")
                        else "REJECTED"
                    )

                    item["reason"] = llm.get(
                        "reason",
                        item["reason"],
                    )

    return {
        "lastBotLog": last_bot_log,
        "symbols": [
            statuses[symbol]
            for symbol in SYMBOLS
        ],
    }

IST = ZoneInfo("Asia/Kolkata")


def safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def empty_trade_performance() -> dict:
    return {
        "today": {
            "closedTrades": 0,
            "closedPnL": 0.0,
            "winRate": 0.0,
            "symbolPnL": {
                symbol: 0.0
                for symbol in SYMBOLS
            },
            "categoryPnL": {
                "optionSell": 0.0,
                "optionBuy": 0.0,
                "stockFutures": 0.0,
                "overall": 0.0,
            },
        },
        "cumulative": {
            "totalTrades": 0,
            "totalPnL": 0.0,
            "winRate": 0.0,
            "averageProfitPerWinningTrade": 0.0,
            "averageLossPerLosingTrade": 0.0,
            "symbolPnL": {
                symbol: 0.0
                for symbol in SYMBOLS
            },
            "categoryPerformance": category_performance(
                []
            ),
        },
        "equityCurve": [],
        "recentTrades": [],
    }


def calculate_win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0

    winning_trades = sum(
        1
        for trade in trades
        if trade["grossPnL"] > 0
    )

    return round(
        winning_trades / len(trades) * 100,
        1,
    )


def average_trade_results(trades: list[dict]) -> tuple[float, float]:
    profits = [trade["grossPnL"] for trade in trades if trade["grossPnL"] > 0]
    losses = [abs(trade["grossPnL"]) for trade in trades if trade["grossPnL"] < 0]
    return (
        round(sum(profits) / len(profits), 2) if profits else 0.0,
        round(sum(losses) / len(losses), 2) if losses else 0.0,
    )


def symbol_pnl(trades: list[dict]) -> dict:
    totals = {
        symbol: 0.0
        for symbol in SYMBOLS
    }

    for trade in trades:
        symbol = trade["symbol"]

        if symbol not in totals:
            totals[symbol] = 0.0

        totals[symbol] += trade["grossPnL"]

    return {
        symbol: round(value, 2)
        for symbol, value in totals.items()
    }


def trade_category(trade: dict) -> str:
    instrument_class = str(
        trade.get("instrumentClass") or ""
    ).upper()
    position_side = str(
        trade.get("positionSide") or ""
    ).upper()
    transaction_type = str(
        trade.get("transactionType") or "BUY"
    ).upper()

    if "FUT" in instrument_class or "FUT" in position_side:
        return "STOCK_FUTURES"

    if transaction_type == "SELL":
        return "OPTION_SELL"

    return "OPTION_BUY"


def category_performance(
    trades: list[dict],
) -> list[dict]:
    groups = [
        ("NIFTY", "OPTION_SELL"),
        ("NIFTY", "OPTION_BUY"),
        ("BANKNIFTY", "OPTION_SELL"),
        ("BANKNIFTY", "OPTION_BUY"),
        ("ALL", "STOCK_FUTURES"),
    ]

    summaries = []

    for symbol, category in groups:
        matching = [
            trade
            for trade in trades
            if str(
                trade.get("underlyingSymbol")
                or trade.get("symbol")
                or ""
            ).upper()
            == symbol
            and trade_category(trade) == category
        ]

        if category == "STOCK_FUTURES":
            matching = [
                trade for trade in trades
                if trade_category(trade) == category
            ]

        summaries.append(
            {
                "symbol": symbol,
                "category": category,
                "tradeCount": len(matching),
                "winRate": calculate_win_rate(
                    matching
                ),
                "cumulativePnL": round(
                    sum(
                        trade["grossPnL"]
                        for trade in matching
                    ),
                    2,
                ),
            }
        )

    return summaries


def today_category_pnl(
    trades: list[dict],
) -> dict:
    option_sell = sum(
        trade["grossPnL"]
        for trade in trades
        if trade_category(trade) == "OPTION_SELL"
    )
    option_buy = sum(
        trade["grossPnL"]
        for trade in trades
        if trade_category(trade) == "OPTION_BUY"
    )
    stock_futures = sum(
        trade["grossPnL"]
        for trade in trades
        if trade_category(trade) == "STOCK_FUTURES"
    )

    return {
        "optionSell": round(option_sell, 2),
        "optionBuy": round(option_buy, 2),
        "stockFutures": round(stock_futures, 2),
        "overall": round(
            option_sell
            + option_buy
            + stock_futures,
            2,
        ),
    }


def normalize_trade(row: dict) -> dict:
    return {
        "tradeDate": row.get("trade_date", ""),
        "symbol": row.get("symbol", ""),
        "underlyingSymbol": row.get("underlying_symbol", row.get("symbol", "")),
        "instrumentClass": row.get("instrument_class", "INDEX_OPTION"),
        "tradingSymbol": row.get(
            "trading_symbol",
            "",
        ),
        "direction": row.get("direction", ""),
        "transactionType": str(
            row.get("transaction_type") or "BUY"
        ).upper(),
        "positionSide": row.get(
            "position_side",
            "",
        ),
        "quantity": safe_int(row.get("quantity")),
        "entryTime": row.get("entry_time", ""),
        "entryPrice": safe_float(
            row.get("entry_price")
        ),
        "exitTime": row.get("exit_time", ""),
        "exitPrice": safe_float(
            row.get("exit_price")
        ),
        "targetPrice": safe_float(
            row.get("target_price")
        ),
        "stopLossPrice": safe_float(
            row.get("stop_loss_price")
        ),
        "exitReason": row.get("exit_reason", ""),
        "grossPnL": safe_float(
            row.get("gross_pnl")
        ),
        "status": row.get("status", "CLOSED"),
    }


def read_trade_history() -> list[dict]:
    if not TRADE_HISTORY_FILE.exists():
        return []

    try:
        with TRADE_HISTORY_FILE.open(
            "r",
            newline="",
            errors="ignore",
        ) as file:
            reader = csv.DictReader(file)

            return [
                normalize_trade(row)
                for row in reader
                if row
            ]
    except (OSError, csv.Error):
        return []


def build_equity_curve(
    trades: list[dict],
) -> list[dict]:
    daily_totals = {}

    for trade in trades:
        trade_date = trade["tradeDate"]

        if not trade_date:
            continue

        daily_totals.setdefault(
            trade_date,
            0.0,
        )

        daily_totals[trade_date] += (
            trade["grossPnL"]
        )

    cumulative_pnl = 0.0
    points = []

    for trade_date in sorted(daily_totals):
        daily_pnl = round(
            daily_totals[trade_date],
            2,
        )

        cumulative_pnl += daily_pnl

        points.append(
            {
                "date": trade_date,
                "dailyPnL": daily_pnl,
                "cumulativePnL": round(
                    cumulative_pnl,
                    2,
                ),
            }
        )

    return points


def build_trade_performance() -> dict:
    trades = read_trade_history()

    if not trades:
        return empty_trade_performance()

    today_text = datetime.now(
        IST
    ).strftime("%Y-%m-%d")

    today_trades = [
        trade
        for trade in trades
        if trade["tradeDate"] == today_text
    ]

    recent_trades = sorted(
        trades,
        key=lambda trade: trade["exitTime"],
        reverse=True,
    )[:20]
    average_profit, average_loss = average_trade_results(trades)

    return {
        "today": {
            "closedTrades": len(today_trades),
            "closedPnL": round(
                sum(
                    trade["grossPnL"]
                    for trade in today_trades
                ),
                2,
            ),
            "winRate": calculate_win_rate(
                today_trades
            ),
            "symbolPnL": symbol_pnl(
                today_trades
            ),
            "categoryPnL": today_category_pnl(
                today_trades
            ),
        },
        "cumulative": {
            "totalTrades": len(trades),
            "totalPnL": round(
                sum(
                    trade["grossPnL"]
                    for trade in trades
                ),
                2,
            ),
            "winRate": calculate_win_rate(
                trades
            ),
            "averageProfitPerWinningTrade": average_profit,
            "averageLossPerLosingTrade": average_loss,
            "symbolPnL": symbol_pnl(trades),
            "categoryPerformance": category_performance(
                trades
            ),
        },
        "equityCurve": build_equity_curve(
            trades
        ),
        "recentTrades": recent_trades,
    }

def state_file(symbol: str) -> Path:
    return BASE_DIR / f"trade_state_{symbol}.json"


def read_json_file(
    path: Path,
    default=None,
):
    if default is None:
        default = {}

    if not path.exists():
        return default

    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def upstox_headers():
    token = os.getenv("UPSTOX_ACCESS_TOKEN")

    if not token:
        return None

    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def fetch_upstox_positions() -> tuple[list, str | None]:
    """
    Read current positions from Upstox.

    No order-placement endpoint is used.
    Error responses are deliberately sanitized.
    """
    headers = upstox_headers()

    if not headers:
        return [], "UPSTOX_ACCESS_TOKEN is not configured"

    try:
        response = requests.get(
            UPSTOX_POSITIONS_URL,
            headers=headers,
            timeout=12,
        )

        if response.status_code >= 300:
            return (
                [],
                "Upstox positions request failed "
                f"with status {response.status_code}",
            )

        payload = response.json()

        positions = payload.get("data", [])

        if not isinstance(positions, list):
            return [], "Unexpected Upstox response format"

        return positions, None

    except requests.Timeout:
        return [], "Upstox request timed out"

    except requests.RequestException:
        return [], "Unable to contact Upstox"

    except ValueError:
        return [], "Upstox returned invalid JSON"


def broker_position_quantity(position: dict) -> int:
    for key in ["quantity", "net_quantity"]:
        value = position.get(key)

        if value is not None:
            return safe_int(value)

    buy_quantity = safe_float(
        position.get("day_buy_quantity")
    )

    sell_quantity = safe_float(
        position.get("day_sell_quantity")
    )

    return int(buy_quantity - sell_quantity)


def broker_position_ltp(
    position: dict,
) -> float | None:
    for key in [
        "last_price",
        "ltp",
        "close_price",
    ]:
        value = position.get(key)

        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue

    return None


def broker_average_price(
    position: dict,
    entry_transaction_type: str = "BUY",
) -> float | None:
    side_specific_keys = (
        ["sell_price", "day_sell_price"]
        if str(entry_transaction_type).upper() == "SELL"
        else ["buy_price", "day_buy_price"]
    )

    for key in ["average_price", *side_specific_keys, "avg_price"]:
        value = position.get(key)

        if value is not None:
            price = safe_float(value)

            if price > 0:
                return price

    return None


def find_broker_position(
    positions: list[dict],
    instrument_key: str,
) -> dict | None:
    for position in positions:
        position_key = (
            position.get("instrument_token")
            or position.get("instrument_key")
        )

        if position_key == instrument_key:
            return position

    return None


def calculate_target_progress(
    entry_price: float,
    last_price: float | None,
    target_price: float,
    entry_transaction_type: str = "BUY",
) -> float | None:
    is_short = str(entry_transaction_type).upper() == "SELL"
    if (
        last_price is None
        or entry_price <= 0
        or target_price <= 0
        or (is_short and target_price >= entry_price)
        or (not is_short and target_price <= entry_price)
    ):
        return None

    achieved = entry_price - last_price if is_short else last_price - entry_price
    planned = entry_price - target_price if is_short else target_price - entry_price
    progress = achieved / planned * 100

    return round(progress, 1)


def build_live_positions() -> dict:
    broker_positions, upstox_error = (
        fetch_upstox_positions()
    )

    dashboard_positions = []

    for symbol in STATE_SLOTS:
        state = read_json_file(
            state_file(symbol)
        )

        instrument_key = state.get(
            "instrument_key"
        )

        if not state or not instrument_key:
            continue

        broker_position = find_broker_position(
            broker_positions,
            instrument_key,
        )

        quantity = safe_int(
            state.get("quantity")
        )
        entry_transaction_type = str(
            state.get("entry_transaction_type")
            or "BUY"
        ).upper()
        is_short = entry_transaction_type == "SELL"

        broker_quantity = 0
        entry_price = safe_float(
            state.get("entry_price")
        )

        last_price = None

        if broker_position:
            broker_quantity = (
                broker_position_quantity(
                    broker_position
                )
            )

            last_price = broker_position_ltp(
                broker_position
            )

            broker_entry = broker_average_price(
                broker_position,
                entry_transaction_type,
            )

            if broker_entry is not None:
                entry_price = broker_entry

        target_price = safe_float(
            state.get("target_price")
        )

        stop_loss_price = safe_float(
            state.get("stop_loss_price")
        )

        highest_price = safe_float(
            state.get("lowest_ltp")
            if is_short
            else state.get("highest_ltp"),
            entry_price,
        )

        live_pnl = None
        risk_to_stop = None
        reward_left = None

        if (
            last_price is not None
            and entry_price > 0
        ):
            live_pnl = round(
                (
                    entry_price - last_price
                    if is_short
                    else last_price - entry_price
                )
                * quantity,
                2,
            )

        if (
            last_price is not None
            and stop_loss_price > 0
        ):
            risk_to_stop = round(
                max(
                    stop_loss_price - last_price
                    if is_short
                    else last_price - stop_loss_price,
                    0,
                ) * quantity,
                2,
            )

        if (
            last_price is not None
            and target_price > 0
        ):
            reward_left = round(
                max(
                    last_price - target_price
                    if is_short
                    else target_price - last_price,
                    0,
                ) * quantity,
                2,
            )

        dashboard_positions.append(
            {
                "symbol": symbol,
                "underlyingSymbol": state.get("underlying_symbol", symbol),
                "instrumentClass": state.get("instrument_class", "INDEX_OPTION"),
                "tradingSymbol": state.get(
                    "trading_symbol",
                    "",
                ),
                "direction": state.get(
                    "direction",
                    "BUY",
                ),
                "transactionType": entry_transaction_type,
                "positionSide": state.get(
                    "position_side",
                    "SHORT_OPTION" if is_short else "LONG_OPTION",
                ),
                "status": state.get(
                    "status",
                    "OPEN",
                ),
                "quantity": quantity,
                "brokerQuantity": (
                    broker_quantity
                ),
                "entryPrice": entry_price,
                "lastPrice": last_price,
                "targetPrice": target_price,
                "stopLossPrice": (
                    stop_loss_price
                ),
                "highestPrice": highest_price,
                "livePnL": live_pnl,
                "targetProgress": (
                    calculate_target_progress(
                        entry_price,
                        last_price,
                        target_price,
                        entry_transaction_type,
                    )
                ),
                "riskToStop": risk_to_stop,
                "rewardLeft": reward_left,
                "trailingStopActive": bool(
                    state.get(
                        "trailing_stop_active",
                        False,
                    )
                ),
                "trailingStopReason": (
                    state.get(
                        "trailing_stop_reason",
                        "",
                    )
                ),
                "createdAt": state.get(
                    "created_at",
                    "",
                ),
            }
        )

    total_live_pnl = round(
        sum(
            position["livePnL"] or 0
            for position in dashboard_positions
        ),
        2,
    )

    return {
        "upstoxStatus": (
            "healthy"
            if upstox_error is None
            else "check"
        ),
        "error": upstox_error,
        "openTradeCount": len(
            dashboard_positions
        ),
        "totalLivePnL": total_live_pnl,
        "positions": dashboard_positions,
    }

def build_health_snapshot() -> dict:
    load_env()

    bot_status = parse_latest_bot_status()

    trade_performance = build_trade_performance()
    live_positions = build_live_positions()
    stock_scanner = read_json_file(
        STOCK_SCANNER_STATUS_FILE,
        {
            "enabled": os.getenv("ENABLE_STOCK_FUTURES_SCANNER", "false").lower() == "true",
            "status": "NO DATA",
            "message": "The stock-futures scanner has not run yet",
        },
    )

    return {
        "status": "ok",
        "service": "HK Trading Dashboard Data",
        "baseDirectory": str(BASE_DIR),
        "files": {
            "tradeHistory": file_status(
                TRADE_HISTORY_FILE
            ),
            "botLog": file_status(LOG_FILE),
            "stockScannerStatus": file_status(STOCK_SCANNER_STATUS_FILE),
            "environmentFilePresent": ENV_FILE.exists(),
        },
        "configuration": {
            "upstoxTokenPresent": bool(
                os.getenv("UPSTOX_ACCESS_TOKEN")
            )
        },
        "bot": bot_status,
        "performance": trade_performance,
        "live": live_positions,
        "stockFuturesScanner": stock_scanner,
    }


if __name__ == "__main__":
    snapshot = build_health_snapshot()

    print(
        json.dumps(
            snapshot,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
