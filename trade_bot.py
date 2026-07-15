import os
import sys
import json
import gzip
import time as time_module
import socket
from unittest import result
import urllib.request
from pathlib import Path
from datetime import datetime, time
from zoneinfo import ZoneInfo
import csv

from analysis_journal import record_analysis
from institutional_flow import (
    get_institutional_footprint,
    neutral_institutional_footprint,
)
from llm_decision import get_llm_decision
from market_technicals import (
    get_technical_analysis,
    convert_index_levels_to_option_premium,
    get_option_volume_vwap_analysis,
)

from option_chain_trend import get_option_chain_trend, record_option_chain_snapshot
from signal_score import weighted_alignment_score

import requests
import urllib3.util.connection as urllib3_cn

from strategy_core import get_index_recommendation, now_ist, option_chain_signal
from trade_journal import record_closed_trade

from whatsapp_alerts import send_trade_closed_alert
from apns_push import send_trade_closed_notification, send_trade_entered_notification


def send_apple_closed_trade_alert(journal_row):
    """Never allow a notification failure to interrupt trading cleanup."""
    try:
        result = send_trade_closed_notification(journal_row)
        log(f"Apple trade-close notification result: {result}")
    except Exception as error:
        log(f"Apple trade-close notification failed: {error}")


def send_apple_trade_entered_alert(position_state):
    """Never allow a notification failure to interrupt position tracking."""
    try:
        result = send_trade_entered_notification(position_state)
        log(f"Apple trade-entry notification result: {result}")
    except Exception as error:
        log(f"Apple trade-entry notification failed: {error}")


def allowed_gai_family():
    return socket.AF_INET


urllib3_cn.allowed_gai_family = allowed_gai_family

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
INSTRUMENT_CACHE = BASE_DIR / "upstox_complete.json.gz"
TRADE_COUNT_FILE = BASE_DIR / "daily_trade_count.json"
TRADE_HISTORY_FILE = BASE_DIR / "data" / "trade_history.csv"

SYMBOLS = ["NIFTY", "BANKNIFTY"]

# Used only when the corresponding environment variable is not set.
DEFAULT_LOT_MULTIPLIERS = {
    "NIFTY": 1,
    "BANKNIFTY": 1,
}

MIN_SCORE_BY_SYMBOL = {
    "NIFTY": 70,
    "BANKNIFTY": 65,
}

LLM_RESCUE_SCORE = 50

DEFAULT_NORMAL_TARGET_PERCENT = 10.0
DEFAULT_NORMAL_STOP_PERCENT = 7.5
DEFAULT_CAUTIOUS_TARGET_PERCENT = 6.0
DEFAULT_CAUTIOUS_STOP_PERCENT = 5.0
DEFAULT_MIN_TECHNICAL_REWARD_RISK = 1.0
DEFAULT_MAX_ENTRY_EXTENSION_PERCENT = 1.5
DEFAULT_RISK_SLOTS_PER_DAY = 3
DEFAULT_MIN_REENTRY_MINUTES = 30

SYMBOL_CONFIG = {
    "NIFTY": {
        "underlying_candidates": ["NIFTY"],
    },
    "BANKNIFTY": {
        "underlying_candidates": ["BANKNIFTY", "NIFTY BANK"],
    },
}

UPSTOX_PLACE_ORDER_URL = "https://api-hft.upstox.com/v2/order/place"
UPSTOX_ORDER_DETAILS_URL = "https://api.upstox.com/v2/order/details"
UPSTOX_POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"
UPSTOX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"


def state_file(symbol):
    return BASE_DIR / f"trade_state_{symbol}.json"


def reentry_guard_file(symbol):
    return BASE_DIR / f"reentry_guard_{symbol}.json"


def load_env():
    if not ENV_FILE.exists():
        return

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log(msg):
    print(f"{now_ist().strftime('%Y-%m-%d %H:%M:%S')} | {msg}", flush=True)


def to_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def to_int(value, default=0):
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def configured_percent(env_key, default):
    value = to_float(os.getenv(env_key), default)
    if value <= 0 or value >= 100:
        raise RuntimeError(f"{env_key} must be greater than 0 and less than 100")
    return value


def risk_percentages(cautious=False):
    if cautious:
        return (
            configured_percent("CAUTIOUS_TARGET_PERCENT", DEFAULT_CAUTIOUS_TARGET_PERCENT),
            configured_percent("CAUTIOUS_STOP_PERCENT", DEFAULT_CAUTIOUS_STOP_PERCENT),
        )

    return (
        configured_percent("NORMAL_TARGET_PERCENT", DEFAULT_NORMAL_TARGET_PERCENT),
        configured_percent("NORMAL_STOP_PERCENT", DEFAULT_NORMAL_STOP_PERCENT),
    )


def option_levels_from_fill(entry_price, target_percent, stop_percent):
    entry = float(entry_price)
    target = round(entry * (1 + float(target_percent) / 100), 0)
    stop = round(max(entry * (1 - float(stop_percent) / 100), 0), 0)
    return target, stop


def configured_non_negative_float(env_key, default):
    value = to_float(os.getenv(env_key), default)
    if value < 0:
        raise RuntimeError(f"{env_key} must be greater than or equal to 0")
    return value


def evaluate_trade_feasibility(
    direction,
    entry_price,
    proposed_target,
    stop_loss,
    technicals,
):
    entry = float(entry_price)
    target = float(proposed_target)
    stop = float(stop_loss)
    min_rr = configured_non_negative_float(
        "MIN_TECHNICAL_REWARD_RISK",
        DEFAULT_MIN_TECHNICAL_REWARD_RISK,
    )
    max_extension = configured_non_negative_float(
        "MAX_ENTRY_EXTENSION_PERCENT",
        DEFAULT_MAX_ENTRY_EXTENSION_PERCENT,
    )

    result = {
        "allowed": False,
        "direction": direction,
        "entry_price": round(entry, 2),
        "proposed_target_price": round(target, 2),
        "stop_loss_price": round(stop, 2),
        "minimum_reward_risk": round(min_rr, 2),
        "reasons": [],
    }

    risk = entry - stop
    if risk <= 0:
        result["reasons"].append("Stop loss does not define positive option-premium risk")
        return result

    option_flow = technicals.get("atm_option_flow", {}) or {}
    completed_candle_close = to_float(option_flow.get("close"), 0)
    if completed_candle_close > 0:
        entry_extension = ((entry - completed_candle_close) / completed_candle_close) * 100
        result["entry_extension_percent"] = round(entry_extension, 2)
        result["maximum_entry_extension_percent"] = round(max_extension, 2)
        if entry_extension > max_extension:
            result["reasons"].append(
                f"Expected entry is {entry_extension:.2f}% above the completed ATM option candle; "
                f"maximum is {max_extension:.2f}%"
            )
            return result

    target_candidates = []
    for timeframe_key, label in (("five_min", "5M"), ("fifteen_min", "15M")):
        analysis = technicals.get(timeframe_key, {}) or {}
        technical_target = to_float(analysis.get("option_target_price"), 0)
        if analysis.get("bias") == direction and technical_target > entry:
            target_candidates.append((technical_target, label))

    result["technical_target_candidates"] = [
        {"timeframe": label, "target_price": round(value, 2)}
        for value, label in target_candidates
    ]
    if not target_candidates:
        result["reasons"].append(
            "No aligned 5M or 15M option-premium target is available"
        )
        return result

    reachable_target, limiting_timeframe = min(target_candidates, key=lambda item: item[0])
    adjusted_target = min(target, reachable_target)
    reward = adjusted_target - entry
    reward_risk = reward / risk if risk > 0 else 0

    result.update(
        {
            "limiting_timeframe": limiting_timeframe,
            "reachable_target_price": round(reachable_target, 2),
            "adjusted_target_price": round(adjusted_target, 2),
            "technical_reward": round(reward, 2),
            "option_risk": round(risk, 2),
            "technical_reward_risk": round(reward_risk, 2),
            "technical_headroom_percent": round((reward / entry) * 100, 2),
        }
    )

    if reward <= 0 or reward_risk < min_rr:
        result["reasons"].append(
            f"Technical reward/risk {reward_risk:.2f} is below required {min_rr:.2f}; "
            f"{limiting_timeframe} target={reachable_target:.2f}"
        )
        return result

    result["allowed"] = True
    result["reasons"].append(
        f"Technical reward/risk {reward_risk:.2f} passes {min_rr:.2f}; "
        f"target capped by {limiting_timeframe} at {adjusted_target:.2f}"
    )
    return result


def read_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def read_state(symbol):
    return read_json(state_file(symbol), {})


def write_state(symbol, state):
    write_json(state_file(symbol), state)


def clear_state(symbol):
    write_state(symbol, {})


def read_reentry_guard(symbol):
    guard = read_json(reentry_guard_file(symbol), {})
    if guard.get("date") != now_ist().strftime("%Y-%m-%d"):
        return {}
    return guard


def write_reentry_guard(symbol, guard):
    write_json(reentry_guard_file(symbol), guard)


def clear_reentry_guard(symbol):
    write_reentry_guard(symbol, {})


def register_losing_exit_guard(symbol, state, journal_row, exit_reason):
    if exit_reason not in {"STOP_LOSS", "SENTIMENT_EXIT"}:
        return
    if to_float(journal_row.get("gross_pnl")) >= 0:
        return

    try:
        write_reentry_guard(
            symbol,
            {
                "date": now_ist().strftime("%Y-%m-%d"),
                "blocked_direction": state.get("direction"),
                "trading_symbol": state.get("trading_symbol"),
                "stopped_at": now_ist().isoformat(),
                "exit_reason": exit_reason,
                "reset_seen": False,
            },
        )
        log(
            f"{symbol} re-entry guard armed after losing {exit_reason}: "
            f"direction={state.get('direction')}; signal reset required"
        )
    except Exception as error:
        log(f"{symbol} could not persist re-entry guard: {error}")


def observe_signal_reset(symbol, direction):
    guard = read_reentry_guard(symbol)
    blocked_direction = guard.get("blocked_direction")
    if not blocked_direction or guard.get("reset_seen"):
        return

    if direction != blocked_direction:
        guard["reset_seen"] = True
        guard["reset_at"] = now_ist().isoformat()
        guard["reset_direction"] = direction
        write_reentry_guard(symbol, guard)
        log(
            f"{symbol} re-entry guard reset observed: "
            f"blocked_direction={blocked_direction} new_signal={direction}"
        )


def reentry_block_reason(symbol, direction):
    guard = read_reentry_guard(symbol)
    if guard.get("blocked_direction") != direction:
        return ""

    if not guard.get("reset_seen"):
        return (
            f"same-direction re-entry blocked after {guard.get('exit_reason')} at "
            f"{guard.get('stopped_at')}; wait for a neutral/opposite signal reset"
        )

    cooldown_minutes = configured_non_negative_float(
        "MIN_REENTRY_MINUTES",
        DEFAULT_MIN_REENTRY_MINUTES,
    )
    try:
        stopped_at = datetime.fromisoformat(guard.get("stopped_at"))
        elapsed_minutes = (now_ist() - stopped_at).total_seconds() / 60
    except Exception:
        elapsed_minutes = 0

    if elapsed_minutes < cooldown_minutes:
        return (
            f"same-direction re-entry cooldown active after {guard.get('exit_reason')}; "
            f"elapsed={elapsed_minutes:.1f}m required={cooldown_minutes:.1f}m"
        )
    return ""

def today_realized_pnl():
    today = now_ist().strftime("%Y-%m-%d")

    if not TRADE_HISTORY_FILE.exists():
        return 0.0

    total = 0.0

    try:
        with TRADE_HISTORY_FILE.open("r", newline="") as f:
            reader = csv.DictReader(f)

            for row in reader:
                if str(row.get("trade_date")) != today:
                    continue

                total += to_float(row.get("gross_pnl"))

    except Exception as e:
        log(f"Could not read today's realized P&L: {e}")
        return 0.0

    return round(total, 2)


def bot_unrealized_pnl():
    states = [read_state(symbol) for symbol in SYMBOLS]
    tracked = {
        state.get("instrument_key"): state
        for state in states
        if state.get("instrument_key") and state.get("status") in {"POSITION_OPEN", "EXIT_PENDING"}
    }
    if not tracked:
        return 0.0

    total = 0.0
    for position in get_open_positions():
        instrument_key = position.get("instrument_token") or position.get("instrument_key")
        state = tracked.get(instrument_key)
        if not state:
            continue
        quantity = max(position_quantity(position), 0)
        ltp = position_ltp(position)
        entry = to_float(state.get("entry_price"))
        if quantity > 0 and ltp is not None and entry > 0:
            total += (float(ltp) - entry) * quantity

    return round(total, 2)


def daily_profit_target():
    return to_float(os.getenv("DAILY_PROFIT_TARGET"), 0)


def after_profit_target_mode():
    return os.getenv("AFTER_PROFIT_TARGET_MODE", "paper").strip().lower()


def daily_profit_target_reached():
    target = daily_profit_target()

    if target <= 0:
        return False

    return today_realized_pnl() >= target


def daily_max_loss():
    return to_float(os.getenv("DAILY_MAX_LOSS"), 0)


def max_risk_per_trade(symbol):
    symbol_value = to_float(os.getenv(f"{symbol}_MAX_RISK_PER_TRADE"), 0)
    if symbol_value > 0:
        return symbol_value

    global_value = to_float(os.getenv("MAX_RISK_PER_TRADE"), 0)
    if global_value > 0:
        return global_value

    max_loss = daily_max_loss()
    slots = max(to_int(os.getenv("RISK_SLOTS_PER_DAY"), DEFAULT_RISK_SLOTS_PER_DAY), 1)
    return max_loss / slots if max_loss > 0 else 0


def after_max_loss_mode():
    return os.getenv("AFTER_MAX_LOSS_MODE", "paper").strip().lower()


def daily_max_loss_reached():
    max_loss = daily_max_loss()

    if max_loss <= 0:
        return False

    try:
        risk_pnl = today_realized_pnl() + bot_unrealized_pnl()
    except Exception as error:
        log(f"Could not include unrealized bot P&L in max-loss check: {error}")
        risk_pnl = today_realized_pnl()

    return risk_pnl <= -abs(max_loss)


def risk_limit_mode():
    if daily_profit_target_reached():
        return "profit_target", after_profit_target_mode()

    if daily_max_loss_reached():
        return "max_loss", after_max_loss_mode()

    return None, None


def read_trade_count():
    today = now_ist().strftime("%Y-%m-%d")
    data = read_json(TRADE_COUNT_FILE, {"date": today, "counts": {}})

    if data.get("date") != today:
        return {"date": today, "counts": {}}

    if "counts" not in data:
        data["counts"] = {}

    return data


def write_trade_count(data):
    write_json(TRADE_COUNT_FILE, data)


def trade_count_for(symbol):
    data = read_trade_count()
    return int(data.get("counts", {}).get(symbol, 0))


def increment_trade_count(symbol):
    data = read_trade_count()
    counts = data.setdefault("counts", {})
    counts[symbol] = int(counts.get(symbol, 0)) + 1
    write_trade_count(data)
    return counts[symbol]


def upstox_headers():
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN not set in .env")

    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def upstox_request(method, url, **kwargs):
    response = requests.request(method, url, headers=upstox_headers(), timeout=30, **kwargs)

    if response.status_code >= 300:
        raise RuntimeError(f"Upstox API failed {response.status_code}: {response.text[:500]}")

    return response.json()

def opposite_direction(direction):
    return "BEARISH" if direction == "BULLISH" else "BULLISH"

def lot_multiplier_for(symbol):
    default_lots = int(DEFAULT_LOT_MULTIPLIERS.get(symbol, 1))
    env_key = f"{symbol}_LOTS"
    raw_value = os.getenv(env_key)

    if raw_value is None or not raw_value.strip():
        return default_lots

    try:
        lots = int(raw_value.strip())
    except ValueError as error:
        raise RuntimeError(
            f"{env_key} must be a whole number greater than or equal to 1"
        ) from error

    if lots < 1:
        raise RuntimeError(
            f"{env_key} must be greater than or equal to 1; received {lots}"
        )

    return lots


def order_quantity_for(symbol, instrument, entry_price=None, stop_loss_price=None):
    lot_size = int(instrument["lot_size"])
    configured_lots = lot_multiplier_for(symbol)
    risk_budget = max_risk_per_trade(symbol)

    if risk_budget <= 0 or entry_price is None or stop_loss_price is None:
        return lot_size * configured_lots

    risk_per_unit = float(entry_price) - float(stop_loss_price)
    if risk_per_unit <= 0:
        return 0

    risk_per_lot = risk_per_unit * lot_size
    affordable_lots = int(risk_budget // risk_per_lot)
    actual_lots = min(configured_lots, affordable_lots)
    return lot_size * max(actual_lots, 0)


def place_market_order(instrument, transaction_type, quantity):
    payload = {
        "quantity": int(quantity),
        "product": "I",
        "validity": "DAY",
        "price": 0,
        "tag": "index_bot",
        "instrument_token": instrument["instrument_key"],
        "order_type": "MARKET",
        "transaction_type": transaction_type,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
        "market_protection": -1,
    }

    result = upstox_request("POST", UPSTOX_PLACE_ORDER_URL, json=payload)
    return result, payload


def get_order_details(order_id):
    result = upstox_request("GET", UPSTOX_ORDER_DETAILS_URL, params={"order_id": order_id})
    data = result.get("data", {})

    if isinstance(data, list):
        return data[0] if data else {}

    return data or {}


def wait_for_order_complete(order_id, attempts=5, delay_seconds=2):
    latest = {}

    for _ in range(attempts):
        latest = get_order_details(order_id)
        status = str(latest.get("status", "")).lower()

        if status in {"complete", "completed", "traded", "rejected", "cancelled", "canceled"}:
            return latest

        time_module.sleep(delay_seconds)

    return latest


def get_open_positions():
    result = upstox_request("GET", UPSTOX_POSITIONS_URL)
    return result.get("data", []) or []


def position_quantity(position):
    if position.get("quantity") is not None:
        return to_int(position.get("quantity"))

    if position.get("net_quantity") is not None:
        return to_int(position.get("net_quantity"))

    buy_qty = to_int(position.get("day_buy_quantity"))
    sell_qty = to_int(position.get("day_sell_quantity"))
    return buy_qty - sell_qty


def find_matching_position(instrument_key):
    for pos in get_open_positions():
        pos_key = pos.get("instrument_token") or pos.get("instrument_key")
        qty = position_quantity(pos)

        if pos_key == instrument_key and qty > 0:
            return pos

    return None


def position_ltp(position):
    for key in ["last_price", "ltp", "close_price"]:
        value = position.get(key)
        if value is not None:
            return float(value)
    return None


def position_avg_price(position):
    for key in ["average_price", "buy_price", "day_buy_price"]:
        value = position.get(key)
        if value is not None:
            return float(value)
    return None


def ensure_instruments_file():
    if INSTRUMENT_CACHE.exists():
        return

    log("Downloading Upstox instrument file...")
    urllib.request.urlretrieve(UPSTOX_INSTRUMENTS_URL, INSTRUMENT_CACHE)


def parse_expiry(expiry_text):
    text = str(expiry_text).strip()

    for fmt in ["%Y-%m-%d", "%d %b", "%d %b %Y"]:
        try:
            if fmt == "%d %b":
                dt = datetime.strptime(f"{text} {now_ist().year}", "%d %b %Y").date()
                if dt < now_ist().date():
                    dt = datetime.strptime(f"{text} {now_ist().year + 1}", "%d %b %Y").date()
                return dt

            return datetime.strptime(text, fmt).date()
        except Exception:
            continue

    raise RuntimeError(f"Could not parse expiry: {expiry_text}")


def find_index_option_instrument(symbol, expiry_text, strike, option_type):
    ensure_instruments_file()

    wanted_expiry = parse_expiry(expiry_text)
    wanted_strike = float(strike)
    underlying_candidates = set(SYMBOL_CONFIG[symbol]["underlying_candidates"])

    with gzip.open(INSTRUMENT_CACHE, "rt", encoding="utf-8") as f:
        instruments = json.load(f)

    matches = []

    for item in instruments:
        if item.get("segment") != "NSE_FO":
            continue
        if item.get("underlying_symbol") not in underlying_candidates:
            continue
        if item.get("instrument_type") != option_type:
            continue
        if float(item.get("strike_price", -1)) != wanted_strike:
            continue

        expiry_raw = item.get("expiry")

        if isinstance(expiry_raw, int):
            expiry_date = datetime.fromtimestamp(expiry_raw / 1000, IST).date()
        else:
            try:
                expiry_date = parse_expiry(expiry_raw)
            except Exception:
                expiry_date = None

        if expiry_date == wanted_expiry:
            matches.append(item)

    if not matches:
        raise RuntimeError(f"No Upstox instrument found for {symbol} {int(strike)} {option_type} {expiry_text}")

    return sorted(matches, key=lambda x: x.get("lot_size", 0))[0]


def market_window_ok():
    now = now_ist().time()
    return time(9, 20) <= now <= time(15, 15)


def save_open_position_state(
    symbol,
    order_id,
    instrument,
    direction,
    confidence,
    score,
    entry_price,
    quantity,
    target_price=None,
    stop_loss_price=None,
    target_percent=None,
    stop_percent=None,
):
    if target_percent is not None and stop_percent is not None:
        percent_target, percent_stop = option_levels_from_fill(
            entry_price,
            target_percent,
            stop_percent,
        )
        if target_price is not None and float(target_price) > float(entry_price):
            target_price = min(percent_target, round(float(target_price), 0))
        else:
            target_price = percent_target
        stop_loss_price = percent_stop
    else:
        if target_price is None or stop_loss_price is None:
            target_percent, stop_percent = risk_percentages(cautious=False)
            target_price, stop_loss_price = option_levels_from_fill(
                entry_price,
                target_percent,
                stop_percent,
            )
        else:
            target_price = round(float(target_price), 0)
            stop_loss_price = round(float(stop_loss_price), 0)

    state = {
        "date": now_ist().strftime("%Y-%m-%d"),
        "symbol": symbol,
        "buy_order_id": order_id,
        "instrument_key": instrument["instrument_key"],
        "trading_symbol": instrument["trading_symbol"],
        "quantity": int(quantity),
        "lot_size": int(instrument["lot_size"]),
        "lot_multiplier": max(int(quantity) // int(instrument["lot_size"]), 1),
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "entry_price": round(float(entry_price), 2),
        "target_price": target_price,
        "stop_loss_price": stop_loss_price,
        "target_percent": target_percent,
        "stop_percent": stop_percent,
        "status": "POSITION_OPEN",
        "created_at": now_ist().isoformat(),
        "highest_ltp": round(float(entry_price), 2),
        "trailing_stop_active": False,
        "trailing_stop_reason": "",
    }

    write_state(symbol, state)

    log(
        f"{symbol} POSITION OPEN: symbol={instrument['trading_symbol']} "
        f"qty={quantity} lots={state['lot_multiplier']} entry={state['entry_price']} "
        f"target={target_price} stop_loss={stop_loss_price}"
    )
    send_apple_trade_entered_alert(state)


def order_status(order_details):
    return str((order_details or {}).get("status", "")).strip().lower()


def order_is_complete(order_details):
    return order_status(order_details) in {"complete", "completed", "traded"}


def order_is_rejected(order_details):
    return order_status(order_details) in {"rejected", "cancelled", "canceled"}


def complete_exit(symbol, state, order_details, fallback_price, exit_reason, result=None, payload=None):
    exit_price = (
        to_float((order_details or {}).get("average_price"))
        or to_float((order_details or {}).get("price"))
        or to_float(fallback_price)
    )
    if exit_price <= 0:
        raise RuntimeError(f"{symbol} exit completed but no valid fill price was returned")

    journal_row = record_closed_trade(state, exit_price, exit_reason)
    register_losing_exit_guard(symbol, state, journal_row, exit_reason)
    send_trade_closed_alert(journal_row)
    send_apple_closed_trade_alert(journal_row)
    log(
        f"{symbol} {exit_reason} exit confirmed COMPLETE: result={result} "
        f"payload={payload} journal={journal_row}"
    )
    clear_state(symbol)
    return journal_row


def monitor_pending_exit(symbol, state):
    exit_order_id = state.get("exit_order_id")
    if not exit_order_id:
        state["status"] = "POSITION_OPEN"
        write_state(symbol, state)
        return False

    details = wait_for_order_complete(exit_order_id, attempts=1, delay_seconds=0)
    status = order_status(details)
    log(f"{symbol} pending SELL check: order_id={exit_order_id} status={status}")

    if order_is_complete(details):
        complete_exit(
            symbol,
            state,
            details,
            state.get("exit_fallback_price"),
            state.get("exit_reason") or "EXIT",
        )
        return True

    if order_is_rejected(details):
        state["status"] = "POSITION_OPEN"
        state.pop("exit_order_id", None)
        state.pop("exit_reason", None)
        state.pop("exit_fallback_price", None)
        write_state(symbol, state)
        log(f"{symbol} SELL was {status}; position state retained for retry.")
        return False

    return True

def run_position_monitor():
    for symbol in SYMBOLS:
        try:
            state = read_state(symbol)

            if not state:
                log(f"{symbol} monitor: no open bot state.")
                continue

            handle_existing_state(symbol, state)

        except Exception as e:
            log(f"{symbol} monitor ERROR: {e}")

def handle_existing_state(symbol, state):
    instrument_key = state.get("instrument_key")
    if not instrument_key:
        return False

    if state.get("status") == "EXIT_PENDING":
        if monitor_pending_exit(symbol, state):
            return True
        state = read_state(symbol)

    position = find_matching_position(instrument_key)

    if position:
        ltp = position_ltp(position)
        qty = position_quantity(position)
        state = apply_trailing_stop(symbol, state, ltp)
        target_price = float(state.get("target_price"))
        stop_loss_price = float(state.get("stop_loss_price"))

        log(
            f"{symbol} open position active: {state.get('trading_symbol')} "
            f"qty={qty} ltp={ltp} entry={state.get('entry_price')} "
            f"target={target_price} stop_loss={stop_loss_price}"
        )

        sentiment_exit, sentiment_reason = should_exit_on_sentiment_change(symbol, state, ltp)

        if ltp is not None and (ltp >= target_price or ltp <= stop_loss_price or sentiment_exit):
            if sentiment_exit:
                exit_reason = "SENTIMENT_EXIT"
                log(f"{symbol} sentiment exit triggered: {sentiment_reason}")
            else:
                exit_reason = "TARGET" if ltp >= target_price else "STOP_LOSS"
            instrument = {
                "instrument_key": instrument_key,
                "trading_symbol": state.get("trading_symbol"),
            }

            result, payload = place_market_order(instrument, "SELL", qty)
            sell_order_id = result.get("data", {}).get("order_id")
            if not sell_order_id:
                raise RuntimeError(f"{symbol} SELL placed but no order_id returned: {result}")

            sell_details = wait_for_order_complete(sell_order_id)
            if order_is_complete(sell_details):
                complete_exit(symbol, state, sell_details, ltp, exit_reason, result, payload)
            else:
                state["status"] = "EXIT_PENDING"
                state["exit_order_id"] = sell_order_id
                state["exit_reason"] = exit_reason
                state["exit_fallback_price"] = ltp
                write_state(symbol, state)
                log(
                    f"{symbol} SELL not complete; state retained as EXIT_PENDING. "
                    f"order_id={sell_order_id} status={order_status(sell_details)}"
                )

        return True

    buy_order_id = state.get("buy_order_id")

    if buy_order_id and state.get("status") == "BUY_PLACED_NOT_COMPLETE":
        details = wait_for_order_complete(buy_order_id, attempts=1, delay_seconds=0)
        status = str(details.get("status", "")).lower()

        log(f"{symbol} pending BUY check: order_id={buy_order_id} status={status}")

        if status in {"rejected", "cancelled", "canceled"}:
            log(f"{symbol} pending BUY rejected/cancelled. Clearing state.")
            clear_state(symbol)
            return True

        if status in {"complete", "completed", "traded"}:
            position = find_matching_position(instrument_key)
            entry_price = position_avg_price(position) if position else None
            entry_price = entry_price or to_float(details.get("average_price")) or to_float(details.get("price"))

            if not entry_price:
                log(f"{symbol} BUY complete but entry price not found. Keeping state.")
                return True

            quantity = int(state.get("quantity") or position_quantity(position) or 0)

            instrument = {
                "instrument_key": instrument_key,
                "trading_symbol": state.get("trading_symbol"),
                "lot_size": int(state.get("lot_size") or quantity),
            }

            save_open_position_state(
                symbol=symbol,
                order_id=buy_order_id,
                instrument=instrument,
                direction=state.get("direction"),
                confidence=state.get("confidence"),
                score=state.get("score"),
                entry_price=entry_price,
                quantity=quantity,
                target_price=state.get("target_price"),
                stop_loss_price=state.get("stop_loss_price"),
                target_percent=state.get("target_percent"),
                stop_percent=state.get("stop_percent"),
            )
            clear_reentry_guard(symbol)
            return True

        log(f"{symbol} BUY order still pending. No new order.")
        return True

    log(f"{symbol} state exists but no matching open position found. Clearing stale state.")
    clear_state(symbol)
    return False

def apply_trailing_stop(symbol, state, ltp):
    if ltp is None:
        return state

    entry_price = float(state.get("entry_price") or 0)
    target_price = float(state.get("target_price") or 0)
    current_stop = float(state.get("stop_loss_price") or 0)
    current_high = float(state.get("highest_ltp") or entry_price)

    if entry_price <= 0 or target_price <= entry_price:
        return state

    ltp = float(ltp)
    highest_ltp = max(current_high, ltp)

    target_gap = target_price - entry_price
    profit_from_entry = highest_ltp - entry_price
    target_progress = profit_from_entry / target_gap if target_gap > 0 else 0

    new_stop = current_stop
    reason = None

    # Once trade reaches 25% of expected target, reduce risk.
    if target_progress >= 0.25:
        new_stop = max(new_stop, round(entry_price * 0.99, 0))
        reason = "Trail activated: 25% target progress, risk reduced"

    # Once trade reaches 40% of expected target, move to breakeven.
    if target_progress >= 0.40:
        new_stop = max(new_stop, round(entry_price, 0))
        reason = "Trail tightened: 40% target progress, stop moved to breakeven"

    # Once trade reaches 60% of expected target, lock 30% of expected profit.
    if target_progress >= 0.60:
        new_stop = max(new_stop, round(entry_price + target_gap * 0.30, 0))
        reason = "Trail tightened: 60% target progress, locked 30% of expected profit"

    # Once trade reaches 75% of expected target, lock 50% of expected profit.
    if target_progress >= 0.75:
        new_stop = max(new_stop, round(entry_price + target_gap * 0.50, 0))
        reason = "Trail tightened: 75% target progress, locked 50% of expected profit"

    # Once trade reaches 90% of expected target, lock 70% of expected profit.
    if target_progress >= 0.90:
        new_stop = max(new_stop, round(entry_price + target_gap * 0.70, 0))
        reason = "Trail tightened: 90% target progress, locked 70% of expected profit"

    state_changed = False

    if highest_ltp > current_high:
        state["highest_ltp"] = round(highest_ltp, 2)
        state_changed = True

    if new_stop > current_stop:
        state["stop_loss_price"] = round(new_stop, 0)
        state["trailing_stop_active"] = True
        state["trailing_stop_reason"] = reason
        state_changed = True

        log(
            f"{symbol} trailing stop updated: entry={entry_price} "
            f"ltp={ltp} highest={highest_ltp} target={target_price} "
            f"progress={round(target_progress * 100, 1)}% "
            f"old_stop={current_stop} new_stop={new_stop} reason={reason}"
        )

    if state_changed:
        write_state(symbol, state)

    return state

def minutes_since_created(state):
    try:
        created_at = datetime.fromisoformat(state.get("created_at"))
        return (now_ist() - created_at).total_seconds() / 60
    except Exception:
        return 999


def should_exit_on_sentiment_change(symbol, state, ltp):
    if ltp is None:
        return False, ""

    direction = state.get("direction")
    entry_price = float(state.get("entry_price") or 0)
    target_price = float(state.get("target_price") or 0)

    if direction not in {"BULLISH", "BEARISH"}:
        return False, ""

    if entry_price <= 0 or target_price <= entry_price:
        return False, ""

    # Give the trade some time to breathe after entry.
    if minutes_since_created(state) < 5:
        return False, ""

    # If already near target, let target/trailing-stop logic handle it.
    target_progress = (float(ltp) - entry_price) / (target_price - entry_price)
    if target_progress >= 0.70:
        return False, ""

    try:
        result = get_index_recommendation(symbol)

        if isinstance(result, dict):
            df_atm = result.get("df_atm") or result.get("atm") or result.get("atm_df")
        else:
            df_atm = result[0]

        if df_atm is None:
            return False, "sentiment check skipped: no ATM data"

        if isinstance(df_atm, dict):
            atm = df_atm
        elif hasattr(df_atm, "empty"):
            if df_atm.empty:
                return False, "sentiment check skipped: empty ATM data"
            atm = df_atm.iloc[0]
        else:
            return False, f"sentiment check skipped: unsupported ATM type {type(df_atm)}"

        new_direction, new_confidence, new_score, new_reasons = option_chain_signal(atm)

        if direction == "BULLISH":
            opposite_is_strong = new_direction == "BEARISH" and new_confidence == "HIGH" and new_score <= -4
        else:
            opposite_is_strong = new_direction == "BULLISH" and new_confidence == "HIGH" and new_score >= 4

        if opposite_is_strong:
            return True, (
                f"sentiment invalidated: trade_direction={direction}, "
                f"new_direction={new_direction}, confidence={new_confidence}, "
                f"score={new_score}, reasons={new_reasons}"
            )

        return False, ""

    except Exception as e:
        log(f"{symbol} sentiment exit check failed: {e}")
        return False, ""

def run_squareoff():
    for symbol in SYMBOLS:
        state = read_state(symbol)

        if not state.get("instrument_key"):
            log(f"{symbol} no bot state found for squareoff.")
            continue

        if state.get("status") == "EXIT_PENDING":
            monitor_pending_exit(symbol, state)
            continue

        position = find_matching_position(state["instrument_key"])

        if position:
            qty = position_quantity(position)
            instrument = {
                "instrument_key": state["instrument_key"],
                "trading_symbol": state.get("trading_symbol"),
            }

            try:
                result, payload = place_market_order(instrument, "SELL", qty)
                sell_order_id = result.get("data", {}).get("order_id")
                if not sell_order_id:
                    raise RuntimeError(f"{symbol} squareoff SELL returned no order_id: {result}")

                fallback_price = position_ltp(position)
                sell_details = wait_for_order_complete(sell_order_id)
                if order_is_complete(sell_details):
                    complete_exit(
                        symbol,
                        state,
                        sell_details,
                        fallback_price,
                        "SQUAREOFF",
                        result,
                        payload,
                    )
                else:
                    state["status"] = "EXIT_PENDING"
                    state["exit_order_id"] = sell_order_id
                    state["exit_reason"] = "SQUAREOFF"
                    state["exit_fallback_price"] = fallback_price
                    write_state(symbol, state)
                    log(
                        f"{symbol} squareoff SELL pending; state retained. "
                        f"order_id={sell_order_id} status={order_status(sell_details)}"
                    )
            except Exception as e:
                log(f"{symbol} bot squareoff failed: {e}")
        else:
            log(f"{symbol} no matching bot position found for squareoff.")
            clear_state(symbol)

def cautious_override_allowed(direction, weighted_score, technicals):
    score_value = float(weighted_score.get("score") or 0)
    fifteen = technicals.get("fifteen_min", {}) or {}
    five = technicals.get("five_min", {}) or {}
    atm_flow = technicals.get("atm_option_flow", {}) or {}
    institutional = technicals.get("institutional_flow", {}) or {}

    atm_close = float(atm_flow.get("close") or 0)
    atm_vwap = float(atm_flow.get("vwap") or 999999)

    return (
        score_value >= LLM_RESCUE_SCORE
        and fifteen.get("bias") != opposite_direction(direction)
        and five.get("bias") != opposite_direction(direction)
        and atm_flow.get("bias") in {"BULLISH", "NEUTRAL"}
        and atm_close >= atm_vwap
        and not (
            institutional.get("confidence") == "HIGH"
            and institutional.get("bias") in {"BULLISH", "BEARISH"}
            and institutional.get("bias") != direction
        )
    )


def collect_institutional_footprint(symbol, recommendation, atm_option_flow=None):
    try:
        footprint = get_institutional_footprint(
            symbol,
            recommendation,
            atm_option_flow or {},
        )
        log(
            f"{symbol} institutional footprint: bias={footprint.get('bias')} "
            f"confidence={footprint.get('confidence')} score={footprint.get('score')} "
            f"reasons={footprint.get('reasons')}"
        )
        return footprint
    except Exception as error:
        log(f"{symbol} institutional footprint unavailable: {error}")
        return neutral_institutional_footprint(str(error))


def process_symbol(symbol):
    state = read_state(symbol)

    if state and handle_existing_state(symbol, state):
        return

    risk_reason, risk_mode = risk_limit_mode()

    if risk_mode == "stop":
        log(
            f"{symbol} no trade: daily risk limit reached. "
            f"reason={risk_reason} today_pnl={today_realized_pnl()} "
            f"profit_target={daily_profit_target()} max_loss={daily_max_loss()} mode=stop"
        )
        return

    rec = get_index_recommendation(symbol)
    record_option_chain_snapshot(symbol, rec)
    direction = rec["direction"]
    confidence = rec["confidence"]
    score = rec["score"]
    atm = rec["atm"]
    prices = rec["prices"]

    log(f"{symbol} signal: {direction}, confidence={confidence}, score={score}, strike={atm['strike']}, expiry={atm['expiry']}")
    observe_signal_reset(symbol, direction)

    if direction not in {"BULLISH", "BEARISH"}:
        collect_institutional_footprint(symbol, rec)
        log(f"{symbol} no trade: neutral signal.")
        return

    if confidence != "HIGH" or abs(score) < 4:
        collect_institutional_footprint(symbol, rec)
        log(f"{symbol} no trade: signal is not strong HIGH confidence.")
        return

    blocked_reason = reentry_block_reason(symbol, direction)
    if blocked_reason:
        log(f"{symbol} no trade: {blocked_reason}")
        return

    option_type = "CE" if direction == "BULLISH" else "PE"
    expected_entry_price = prices.get("entry_price")

    if not expected_entry_price:
        log(f"{symbol} no trade: missing expected entry price.")
        return

    instrument = find_index_option_instrument(symbol, atm["expiry"], atm["strike"], option_type)

    live = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"

    selected_target_percent, selected_stop_percent = risk_percentages(cautious=False)
    expected_target, expected_stop_loss = option_levels_from_fill(
        expected_entry_price,
        selected_target_percent,
        selected_stop_percent,
    )

    option_summary = {
        "bias": direction,
        "confidence": confidence,
        "score": score,
        "strike": atm["strike"],
        "expiry": atm["expiry"],
        "entry_price": round(float(expected_entry_price), 2),
        "target_price": expected_target,
        "stop_loss_price": expected_stop_loss,
        "reasons": rec.get("reasons", []),
    }

    try:
        technicals = get_technical_analysis(symbol)
        option_side = "CE" if direction == "BULLISH" else "PE"

        technicals["two_hour"] = convert_index_levels_to_option_premium(
            technicals.get("two_hour", {}),
            option_side=option_side,
            option_entry_price=expected_entry_price,
            delta=0.5,
        )

        technicals["fifteen_min"] = convert_index_levels_to_option_premium(
            technicals.get("fifteen_min", {}),
            option_side=option_side,
            option_entry_price=expected_entry_price,
            delta=0.5,
        )

        technicals["five_min"] = convert_index_levels_to_option_premium(
            technicals.get("five_min", {}),
            option_side=option_side,
            option_entry_price=expected_entry_price,
            delta=0.5,
        )
    except Exception as e:
        log(f"{symbol} technical analysis failed: {e}")
        technicals = {
            "two_hour": {"bias": "NEUTRAL", "confidence": "LOW", "reasons": [str(e)]},
            "fifteen_min": {"bias": "NEUTRAL", "confidence": "LOW", "reasons": [str(e)]},
        }

    atm_option_flow = get_option_volume_vwap_analysis(
        instrument["instrument_key"],
        side_label=instrument["trading_symbol"],
    )
    technicals["atm_option_flow"] = atm_option_flow
    technicals["institutional_flow"] = collect_institutional_footprint(
        symbol,
        rec,
        atm_option_flow,
    )

    option_trend = get_option_chain_trend(symbol, direction, expiry=atm.get("expiry"))
    weighted_score = weighted_alignment_score(option_summary, technicals, option_trend)

    option_summary["option_chain_trend"] = option_trend
    option_summary["weighted_alignment"] = weighted_score

    cautious_override = False

    score_value = float(weighted_score.get("score") or 0)
    symbol_min_score = MIN_SCORE_BY_SYMBOL.get(symbol, 65)

    if weighted_score["grade"] == "SKIP":
        if score_value >= LLM_RESCUE_SCORE and cautious_override_allowed(direction, weighted_score, technicals):
            cautious_override = True
            selected_target_percent, selected_stop_percent = risk_percentages(cautious=True)
            expected_target, expected_stop_loss = option_levels_from_fill(
                expected_entry_price,
                selected_target_percent,
                selected_stop_percent,
            )

            option_summary["target_price"] = expected_target
            option_summary["stop_loss_price"] = expected_stop_loss
            option_summary["cautious_override"] = True
            option_summary["cautious_override_reason"] = (
                "Weighted score was SKIP, but short-term filters and ATM option VWAP allowed cautious trade."
            )

            log(
                f"{symbol} cautious override allowed before LLM: "
                f"score={weighted_score.get('score')} target={expected_target} "
                f"stop_loss={expected_stop_loss}"
            )
        else:
            llm_decision = {
                "execute_trade": False,
                "decision": "NO_TRADE",
                "confidence": "LOW",
                "target_price": None,
                "stop_loss_price": None,
                "reason": f"Weighted score too low before LLM: {weighted_score}",
            }
            record_analysis(symbol, option_summary, technicals, llm_decision)
            log(f"{symbol} no trade: weighted score too low before LLM: {weighted_score}")
            return
    if weighted_score["grade"] == "CAUTIOUS_TRADE":
        selected_target_percent, selected_stop_percent = risk_percentages(cautious=True)
        expected_target, expected_stop_loss = option_levels_from_fill(
            expected_entry_price,
            selected_target_percent,
            selected_stop_percent,
        )
        option_summary["target_price"] = expected_target
        option_summary["stop_loss_price"] = expected_stop_loss
        option_summary["cautious_trade"] = True

    if weighted_score["grade"] == "CAUTIOUS_TRADE" and score_value < symbol_min_score:
        llm_decision = {
            "execute_trade": False,
            "decision": "NO_TRADE",
            "confidence": "LOW",
            "target_price": None,
            "stop_loss_price": None,
            "reason": f"{symbol} score {score_value} is below symbol minimum {symbol_min_score}",
        }
        record_analysis(symbol, option_summary, technicals, llm_decision)
        log(f"{symbol} no trade: score below symbol minimum. score={score_value}, required={symbol_min_score}")
        return


    feasibility = evaluate_trade_feasibility(
        direction=direction,
        entry_price=expected_entry_price,
        proposed_target=expected_target,
        stop_loss=expected_stop_loss,
        technicals=technicals,
    )
    technicals["trade_feasibility"] = feasibility
    option_summary["trade_feasibility"] = feasibility

    if not feasibility.get("allowed"):
        llm_decision = {
            "execute_trade": False,
            "decision": "NO_TRADE",
            "confidence": "HIGH",
            "target_price": None,
            "stop_loss_price": None,
            "reason": "Entry feasibility rejected: " + "; ".join(feasibility.get("reasons", [])),
        }
        record_analysis(symbol, option_summary, technicals, llm_decision)
        log(f"{symbol} no trade: {llm_decision['reason']} feasibility={feasibility}")
        return

    expected_target = float(feasibility["adjusted_target_price"])
    option_summary["target_price"] = expected_target

    llm_decision = get_llm_decision(symbol, option_summary, technicals)
    record_analysis(symbol, option_summary, technicals, llm_decision)

    log(
        f"{symbol} analysis: option={option_summary} "
        f"2h={technicals.get('two_hour')} "
        f"15m={technicals.get('fifteen_min')} "
        f"5m={technicals.get('five_min')} "
        f"weighted={weighted_score} "
        f"llm={llm_decision} "
        f"atm_option_flow={technicals.get('atm_option_flow')} "
        f"institutional_flow={technicals.get('institutional_flow')} "
    )

    if not llm_decision.get("execute_trade"):
        log(f"{symbol} no trade: LLM/rule decision rejected trade. reason={llm_decision.get('reason')}")
        return

    if llm_decision.get("decision") != direction:
        log(
            f"{symbol} no trade: LLM decision {llm_decision.get('decision')} "
            f"does not match option-chain direction {direction}."
        )
        return

    llm_target = llm_decision.get("target_price")
    llm_stop_loss = llm_decision.get("stop_loss_price")
    if llm_target is not None or llm_stop_loss is not None:
        log(
            f"{symbol} LLM price output is advisory only; deterministic risk levels retained. "
            f"llm_target={llm_target} llm_stop={llm_stop_loss} "
            f"target={expected_target} stop={expected_stop_loss}"
        )

    if expected_target <= float(expected_entry_price) or expected_stop_loss >= float(expected_entry_price):
        log(
            f"{symbol} no trade: invalid target/stop from LLM. "
            f"entry={expected_entry_price} target={expected_target} stop={expected_stop_loss}"
        )
        return


    order_quantity = order_quantity_for(
        symbol,
        instrument,
        entry_price=expected_entry_price,
        stop_loss_price=expected_stop_loss,
    )
    if order_quantity <= 0:
        risk_per_lot = (
            (float(expected_entry_price) - float(expected_stop_loss))
            * int(instrument["lot_size"])
        )
        log(
            f"{symbol} no trade: one lot risks approximately {risk_per_lot:.2f}, "
            f"above per-trade budget {max_risk_per_trade(symbol):.2f}"
        )
        return

    actual_lots = order_quantity // int(instrument["lot_size"])

    log(
        f"{symbol} prepared MARKET BUY: {instrument['trading_symbol']} qty={order_quantity} "
        f"lots={actual_lots}/{lot_multiplier_for(symbol)} risk_budget={max_risk_per_trade(symbol):.2f} "
        f"expected_entry={expected_entry_price} expected_target={expected_target} "
        f"expected_stop_loss={expected_stop_loss} live={live}"
    )

    risk_reason, risk_mode = risk_limit_mode()

    if risk_mode == "paper":
        log(
            f"{symbol} PAPER ONLY after daily risk limit: "
            f"reason={risk_reason} today_pnl={today_realized_pnl()} "
            f"profit_target={daily_profit_target()} max_loss={daily_max_loss()} "
            f"would_buy={instrument['trading_symbol']} qty={order_quantity} "
            f"entry={expected_entry_price} target={expected_target} stop_loss={expected_stop_loss}"
        )
        return

    if not live:
        log(f"{symbol} DRY RUN ONLY. Set ENABLE_LIVE_TRADING=true in .env to place real orders.")
        return
    

    result, payload = place_market_order(
        instrument=instrument,
        transaction_type="BUY",
        quantity=order_quantity,
    )

    order_id = result.get("data", {}).get("order_id")
    if not order_id:
        raise RuntimeError(f"{symbol} market BUY placed but no order_id returned: {result}")

    count = increment_trade_count(symbol)
    log(f"{symbol} daily trade count updated: {count}")
    log(f"{symbol} MARKET BUY placed: order_id={order_id} payload={payload}")

    order_details = wait_for_order_complete(order_id)
    order_status = str(order_details.get("status", "")).lower()

    if order_status not in {"complete", "completed", "traded"}:
        write_state(symbol, {
            "date": now_ist().strftime("%Y-%m-%d"),
            "symbol": symbol,
            "buy_order_id": order_id,
            "instrument_key": instrument["instrument_key"],
            "trading_symbol": instrument["trading_symbol"],
            "quantity": int(order_quantity),
            "lot_size": int(instrument["lot_size"]),
            "lot_multiplier": actual_lots,
            "target_price": expected_target,
            "stop_loss_price": expected_stop_loss,
            "target_percent": selected_target_percent,
            "stop_percent": selected_stop_percent,
            "direction": direction,
            "confidence": confidence,
            "score": score,
            "status": "BUY_PLACED_NOT_COMPLETE",
            "created_at": now_ist().isoformat(),
        })
        log(f"{symbol} BUY order not complete yet. status={order_status}. Saved state.")
        return

    position = find_matching_position(instrument["instrument_key"])
    entry_price = position_avg_price(position) if position else None
    entry_price = entry_price or to_float(order_details.get("average_price"))
    entry_price = entry_price or expected_entry_price

    save_open_position_state(
        symbol=symbol,
        order_id=order_id,
        instrument=instrument,
        direction=direction,
        confidence=confidence,
        score=score,
        entry_price=entry_price,
        quantity=order_quantity,
        target_price=expected_target,
        stop_loss_price=expected_stop_loss,
        target_percent=selected_target_percent,
        stop_percent=selected_stop_percent,
    )
    clear_reentry_guard(symbol)


def run_signal_check():
    if not market_window_ok():
        log("Outside trading window. No action.")
        return

    for symbol in SYMBOLS:
        try:
            process_symbol(symbol)
        except Exception as e:
            log(f"{symbol} ERROR: {e}")


def main():
    load_env()

    if "--squareoff" in sys.argv:
        run_squareoff()
    elif "--monitor" in sys.argv:
        run_position_monitor()
    else:
        run_signal_check()


if __name__ == "__main__":
    main()
