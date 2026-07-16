import os
import sys
import json
import gzip
import time as time_module
import socket
import urllib.request
from pathlib import Path
from datetime import datetime, time
from zoneinfo import ZoneInfo
import csv
from copy import deepcopy

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
UPSTOX_CANCEL_ORDER_URL = "https://api-hft.upstox.com/v2/order/cancel"
UPSTOX_MODIFY_ORDER_URL = "https://api-hft.upstox.com/v2/order/modify"
UPSTOX_ORDER_DETAILS_URL = "https://api.upstox.com/v2/order/details"
UPSTOX_POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"
UPSTOX_MARGIN_URL = "https://api.upstox.com/v2/charges/margin"
UPSTOX_FUNDS_URL = "https://api.upstox.com/v2/user/get-funds-and-margin"
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


def option_levels_from_fill(
    entry_price,
    target_percent,
    stop_percent,
    transaction_type="BUY",
):
    entry = float(entry_price)
    if str(transaction_type).upper() == "SELL":
        target = round(max(entry * (1 - float(target_percent) / 100), 0.05), 0)
        stop = round(entry * (1 + float(stop_percent) / 100), 0)
    else:
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
    transaction_type="BUY",
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

    transaction_type = str(transaction_type).upper()
    is_short = transaction_type == "SELL"
    risk = stop - entry if is_short else entry - stop
    if risk <= 0:
        result["reasons"].append("Stop loss does not define positive option-premium risk")
        return result

    option_flow = technicals.get("atm_option_flow", {}) or {}
    completed_candle_close = to_float(option_flow.get("close"), 0)
    if completed_candle_close > 0:
        entry_extension = (
            ((completed_candle_close - entry) / completed_candle_close) * 100
            if is_short
            else ((entry - completed_candle_close) / completed_candle_close) * 100
        )
        result["entry_extension_percent"] = round(entry_extension, 2)
        result["maximum_entry_extension_percent"] = round(max_extension, 2)
        if entry_extension > max_extension:
            result["reasons"].append(
                f"Expected entry is unfavorably extended by {entry_extension:.2f}% versus "
                "the completed ATM option candle; "
                f"maximum is {max_extension:.2f}%"
            )
            return result

    target_candidates = []
    for timeframe_key, label in (("five_min", "5M"), ("fifteen_min", "15M")):
        analysis = technicals.get(timeframe_key, {}) or {}
        technical_target = to_float(analysis.get("option_target_price"), 0)
        target_is_valid = technical_target < entry if is_short else technical_target > entry
        if analysis.get("bias") == direction and target_is_valid:
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

    target_selector = max if is_short else min
    reachable_target, limiting_timeframe = target_selector(
        target_candidates,
        key=lambda item: item[0],
    )
    adjusted_target = max(target, reachable_target) if is_short else min(target, reachable_target)
    reward = entry - adjusted_target if is_short else adjusted_target - entry
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
        raw_quantity = position_quantity(position)
        quantity = abs(raw_quantity)
        ltp = position_ltp(position)
        entry = to_float(state.get("entry_price"))
        if quantity > 0 and ltp is not None and entry > 0:
            if str(state.get("entry_transaction_type") or "BUY").upper() == "SELL":
                total += (entry - float(ltp)) * quantity
            else:
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


def option_type_for(direction, transaction_type):
    if direction == "BULLISH":
        return "CE" if transaction_type == "BUY" else "PE"
    if direction == "BEARISH":
        return "PE" if transaction_type == "BUY" else "CE"
    raise ValueError(f"Unsupported direction: {direction}")


def entry_price_for(atm, direction, transaction_type):
    option_type = option_type_for(direction, transaction_type)
    return to_float(atm.get(f"{option_type}_ltp"), 0)


def normalize_option_flow_for_position(raw_flow, transaction_type):
    flow = dict(raw_flow or {})
    flow["raw_premium_bias"] = flow.get("bias")
    flow["entry_transaction_type"] = transaction_type
    if transaction_type == "SELL":
        close = to_float(flow.get("close"), 0)
        vwap = to_float(flow.get("vwap"), 0)
        slope = to_float(flow.get("vwap_slope"), 0)
        if close > 0 and vwap > 0 and close < vwap and slope <= 0:
            flow["bias"] = "BULLISH"
            flow["confidence"] = "HIGH" if flow.get("volume_confirmed") else "MEDIUM"
        elif close > 0 and vwap > 0 and close > vwap and slope >= 0:
            flow["bias"] = "BEARISH"
            flow["confidence"] = "HIGH" if flow.get("volume_confirmed") else "MEDIUM"
        else:
            flow["bias"] = {
                "BULLISH": "BEARISH",
                "BEARISH": "BULLISH",
            }.get(flow.get("bias"), "NEUTRAL")
        flow["reasons"] = [
            "Short-option expression: falling sold premium supports the position"
        ] + list(flow.get("reasons") or [])
    return flow


def short_structure_allowed(direction, technicals, raw_flow, weighted_score):
    if os.getenv("ALLOW_NAKED_OPTION_SELLING", "false").lower() != "true":
        return False, "ALLOW_NAKED_OPTION_SELLING is not true"

    minimum_score = configured_non_negative_float("SHORT_MIN_WEIGHTED_SCORE", 80.0)
    if float(weighted_score.get("score") or 0) < minimum_score:
        return False, f"short structure score is below {minimum_score:.1f}"

    fifteen = technicals.get("fifteen_min", {}) or {}
    five = technicals.get("five_min", {}) or {}
    institutional = technicals.get("institutional_flow", {}) or {}
    if fifteen.get("bias") != direction or five.get("bias") != direction:
        return False, "15M and 5M must both align for a naked option sell"
    if (
        institutional.get("bias") == opposite_direction(direction)
        and institutional.get("confidence") in {"MEDIUM", "HIGH"}
    ):
        return False, "institutional footprint conflicts with the short structure"

    close = to_float(raw_flow.get("close"), 0)
    vwap = to_float(raw_flow.get("vwap"), 0)
    slope = to_float(raw_flow.get("vwap_slope"), 0)
    volume_ratio = to_float(raw_flow.get("volume_ratio"), 0)
    minimum_volume = configured_non_negative_float("SHORT_MIN_VOLUME_RATIO", 0.8)
    if close <= 0 or vwap <= 0 or close >= vwap or slope > 0:
        return False, "the option proposed for selling is not weakening below a flat/down VWAP"
    if volume_ratio < minimum_volume:
        return False, f"sold-option volume ratio is below {minimum_volume:.2f}"
    return True, "sold option is weakening below VWAP with aligned 15M/5M direction"

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

    max_lots = max(to_int(os.getenv("MAX_LOTS_PER_ENTRY"), 1), 1)
    return min(lots, max_lots)


def order_quantity_for(
    symbol,
    instrument,
    entry_price=None,
    stop_loss_price=None,
    transaction_type="BUY",
):
    lot_size = int(instrument["lot_size"])
    configured_lots = lot_multiplier_for(symbol)
    risk_budget = max_risk_per_trade(symbol)

    if risk_budget <= 0 or entry_price is None or stop_loss_price is None:
        return lot_size * configured_lots

    risk_per_unit = (
        float(stop_loss_price) - float(entry_price)
        if str(transaction_type).upper() == "SELL"
        else float(entry_price) - float(stop_loss_price)
    )
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


def place_stop_market_order(instrument, transaction_type, quantity, trigger_price):
    payload = {
        "quantity": int(quantity),
        "product": "I",
        "validity": "DAY",
        "price": 0,
        "tag": "index_bot_stop",
        "instrument_token": instrument["instrument_key"],
        "order_type": "SL-M",
        "transaction_type": transaction_type,
        "disclosed_quantity": 0,
        "trigger_price": round(float(trigger_price), 1),
        "is_amo": False,
        "market_protection": -1,
    }
    result = upstox_request("POST", UPSTOX_PLACE_ORDER_URL, json=payload)
    return result, payload


def cancel_order(order_id):
    return upstox_request(
        "DELETE",
        UPSTOX_CANCEL_ORDER_URL,
        params={"order_id": order_id},
    )


def modify_stop_order(order_id, quantity, trigger_price):
    payload = {
        "quantity": int(quantity),
        "validity": "DAY",
        "price": 0,
        "order_id": order_id,
        "order_type": "SL-M",
        "disclosed_quantity": 0,
        "trigger_price": round(float(trigger_price), 1),
        "market_protection": -1,
    }
    return upstox_request("PUT", UPSTOX_MODIFY_ORDER_URL, json=payload)


def estimate_order_margin(instrument, transaction_type, quantity, price=None):
    item = {
        "instrument_key": instrument["instrument_key"],
        "quantity": int(quantity),
        "transaction_type": str(transaction_type).upper(),
        "product": "I",
    }
    if price and float(price) > 0:
        item["price"] = float(price)
    result = upstox_request(
        "POST",
        UPSTOX_MARGIN_URL,
        json={"instruments": [item]},
    )
    data = result.get("data", {}) or {}
    return float(data.get("final_margin") or data.get("required_margin") or 0)


def available_equity_margin():
    result = upstox_request("GET", UPSTOX_FUNDS_URL, params={"segment": "SEC"})
    data = result.get("data", {}) or {}
    equity = data.get("equity", data) if isinstance(data, dict) else {}
    return float(
        equity.get("available_margin")
        or equity.get("available_margin_for_trading")
        or equity.get("net")
        or 0
    )


def validate_short_margin(instrument, quantity, price):
    required = estimate_order_margin(instrument, "SELL", quantity, price)
    available = available_equity_margin()
    buffer_percent = configured_non_negative_float("SHORT_MARGIN_BUFFER_PERCENT", 20.0)
    if buffer_percent >= 100:
        raise RuntimeError("SHORT_MARGIN_BUFFER_PERCENT must be less than 100")
    usable = available * (1 - buffer_percent / 100)
    return {
        "allowed": required > 0 and required <= usable,
        "required_margin": round(required, 2),
        "available_margin": round(available, 2),
        "usable_margin_after_buffer": round(usable, 2),
        "buffer_percent": round(buffer_percent, 2),
    }


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
    return find_matching_position_for_side(instrument_key, "BUY")


def find_matching_position_for_side(instrument_key, entry_transaction_type="BUY"):
    for pos in get_open_positions():
        pos_key = pos.get("instrument_token") or pos.get("instrument_key")
        qty = position_quantity(pos)

        expected_sign = -1 if str(entry_transaction_type).upper() == "SELL" else 1
        if pos_key == instrument_key and qty * expected_sign > 0:
            return pos

    return None


def position_ltp(position):
    for key in ["last_price", "ltp", "close_price"]:
        value = position.get(key)
        if value is not None:
            return float(value)
    return None


def position_avg_price(position, entry_transaction_type="BUY"):
    keys = ["average_price"]
    if str(entry_transaction_type).upper() == "SELL":
        keys.extend(["sell_price", "day_sell_price"])
    else:
        keys.extend(["buy_price", "day_buy_price"])
    for key in keys:
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
    entry_transaction_type="BUY",
    protective_stop_order_id=None,
):
    entry_transaction_type = str(entry_transaction_type).upper()
    if target_percent is not None and stop_percent is not None:
        percent_target, percent_stop = option_levels_from_fill(
            entry_price,
            target_percent,
            stop_percent,
            transaction_type=entry_transaction_type,
        )
        target_is_valid = (
            float(target_price) < float(entry_price)
            if entry_transaction_type == "SELL" and target_price is not None
            else target_price is not None and float(target_price) > float(entry_price)
        )
        if target_is_valid:
            target_price = (
                max(percent_target, round(float(target_price), 0))
                if entry_transaction_type == "SELL"
                else min(percent_target, round(float(target_price), 0))
            )
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
                transaction_type=entry_transaction_type,
            )
        else:
            target_price = round(float(target_price), 0)
            stop_loss_price = round(float(stop_loss_price), 0)

    state = {
        "date": now_ist().strftime("%Y-%m-%d"),
        "symbol": symbol,
        "entry_order_id": order_id,
        "buy_order_id": order_id if entry_transaction_type == "BUY" else None,
        "entry_transaction_type": entry_transaction_type,
        "exit_transaction_type": "BUY" if entry_transaction_type == "SELL" else "SELL",
        "position_side": "SHORT_OPTION" if entry_transaction_type == "SELL" else "LONG_OPTION",
        "protective_stop_order_id": protective_stop_order_id,
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
        "lowest_ltp": round(float(entry_price), 2),
        "trailing_stop_active": False,
        "trailing_stop_reason": "",
    }

    write_state(symbol, state)

    log(
        f"{symbol} POSITION OPEN: symbol={instrument['trading_symbol']} "
        f"action={entry_transaction_type} qty={quantity} lots={state['lot_multiplier']} entry={state['entry_price']} "
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
    exit_transaction = state.get("exit_transaction_type") or (
        "BUY" if str(state.get("entry_transaction_type") or "BUY").upper() == "SELL" else "SELL"
    )
    log(f"{symbol} pending {exit_transaction} check: order_id={exit_order_id} status={status}")

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
        log(f"{symbol} exit {exit_transaction} was {status}; position state retained for retry.")
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

def arm_short_protective_stop(symbol, state):
    instrument = {
        "instrument_key": state["instrument_key"],
        "trading_symbol": state.get("trading_symbol"),
    }
    result, payload = place_stop_market_order(
        instrument,
        "BUY",
        int(state["quantity"]),
        float(state["stop_loss_price"]),
    )
    order_id = result.get("data", {}).get("order_id")
    if not order_id:
        raise RuntimeError(f"protective BUY stop returned no order_id: {result}")
    details = wait_for_order_complete(order_id, attempts=1, delay_seconds=0)
    if order_is_rejected(details):
        raise RuntimeError(
            f"protective BUY stop was {order_status(details)}: {details}"
        )
    state["protective_stop_order_id"] = order_id
    write_state(symbol, state)
    log(f"{symbol} broker protective BUY stop armed: order_id={order_id} payload={payload}")
    return state


def protective_stop_filled(symbol, state):
    order_id = state.get("protective_stop_order_id")
    if not order_id:
        return False
    details = get_order_details(order_id)
    if order_is_complete(details):
        complete_exit(symbol, state, details, state.get("stop_loss_price"), "STOP_LOSS")
        return True
    return False


def cancel_protective_stop(symbol, state):
    order_id = state.get("protective_stop_order_id")
    if not order_id:
        return False
    if protective_stop_filled(symbol, state):
        return True
    try:
        cancel_order(order_id)
    except Exception:
        if protective_stop_filled(symbol, state):
            return True
        raise
    state.pop("protective_stop_order_id", None)
    write_state(symbol, state)
    log(f"{symbol} protective stop cancelled before active exit: order_id={order_id}")
    return False


def handle_existing_state(symbol, state):
    instrument_key = state.get("instrument_key")
    if not instrument_key:
        return False

    entry_transaction = str(state.get("entry_transaction_type") or "BUY").upper()
    exit_transaction = "BUY" if entry_transaction == "SELL" else "SELL"

    if entry_transaction == "SELL" and protective_stop_filled(symbol, state):
        return True

    if state.get("status") == "EXIT_PENDING":
        if monitor_pending_exit(symbol, state):
            return True
        state = read_state(symbol)

    position = find_matching_position_for_side(instrument_key, entry_transaction)
    if position:
        ltp = position_ltp(position)
        qty = abs(position_quantity(position))
        if entry_transaction == "SELL" and not state.get("protective_stop_order_id"):
            try:
                state = arm_short_protective_stop(symbol, state)
            except Exception as error:
                log(f"{symbol} CRITICAL: short has no broker stop; flattening now: {error}")
                instrument = {"instrument_key": instrument_key, "trading_symbol": state.get("trading_symbol")}
                result, payload = place_market_order(instrument, "BUY", qty)
                order_id = result.get("data", {}).get("order_id")
                details = wait_for_order_complete(order_id) if order_id else {}
                complete_exit(symbol, state, details, ltp, "PROTECTION_FAILURE", result, payload)
                return True
        state = apply_trailing_stop(symbol, state, ltp)
        target_price = float(state.get("target_price"))
        stop_loss_price = float(state.get("stop_loss_price"))
        is_short = entry_transaction == "SELL"

        log(
            f"{symbol} open {state.get('position_side', 'LONG_OPTION')} active: "
            f"{state.get('trading_symbol')} qty={qty} ltp={ltp} "
            f"entry={state.get('entry_price')} target={target_price} stop_loss={stop_loss_price}"
        )

        sentiment_exit, sentiment_reason = should_exit_on_sentiment_change(symbol, state, ltp)
        target_hit = ltp is not None and (ltp <= target_price if is_short else ltp >= target_price)
        stop_hit = ltp is not None and (ltp >= stop_loss_price if is_short else ltp <= stop_loss_price)
        if ltp is not None and (target_hit or stop_hit or sentiment_exit):
            if sentiment_exit:
                exit_reason = "SENTIMENT_EXIT"
                log(f"{symbol} sentiment exit triggered: {sentiment_reason}")
            else:
                exit_reason = "TARGET" if target_hit else "STOP_LOSS"

            if is_short and cancel_protective_stop(symbol, state):
                return True

            instrument = {"instrument_key": instrument_key, "trading_symbol": state.get("trading_symbol")}
            result, payload = place_market_order(instrument, exit_transaction, qty)
            exit_order_id = result.get("data", {}).get("order_id")
            if not exit_order_id:
                raise RuntimeError(f"{symbol} {exit_transaction} exit returned no order_id: {result}")
            exit_details = wait_for_order_complete(exit_order_id)
            if order_is_complete(exit_details):
                complete_exit(symbol, state, exit_details, ltp, exit_reason, result, payload)
            else:
                state["status"] = "EXIT_PENDING"
                state["exit_order_id"] = exit_order_id
                state["exit_reason"] = exit_reason
                state["exit_fallback_price"] = ltp
                write_state(symbol, state)
                log(f"{symbol} {exit_transaction} exit pending: order_id={exit_order_id}")
        return True

    entry_order_id = state.get("entry_order_id") or state.get("buy_order_id")
    pending_status = f"{entry_transaction}_PLACED_NOT_COMPLETE"
    if entry_order_id and state.get("status") in {pending_status, "BUY_PLACED_NOT_COMPLETE"}:
        details = wait_for_order_complete(entry_order_id, attempts=1, delay_seconds=0)
        status = order_status(details)
        log(f"{symbol} pending {entry_transaction} check: order_id={entry_order_id} status={status}")
        if order_is_rejected(details):
            clear_state(symbol)
            return True
        if order_is_complete(details):
            position = find_matching_position_for_side(instrument_key, entry_transaction)
            entry_price = position_avg_price(position, entry_transaction) if position else None
            entry_price = entry_price or to_float(details.get("average_price")) or to_float(details.get("price"))
            if not entry_price:
                log(f"{symbol} {entry_transaction} complete but entry price not found. Keeping state.")
                return True
            quantity = int(state.get("quantity") or abs(position_quantity(position)) or 0)
            instrument = {
                "instrument_key": instrument_key,
                "trading_symbol": state.get("trading_symbol"),
                "lot_size": int(state.get("lot_size") or quantity),
            }
            save_open_position_state(
                symbol, entry_order_id, instrument, state.get("direction"),
                state.get("confidence"), state.get("score"), entry_price, quantity,
                state.get("target_price"), state.get("stop_loss_price"),
                state.get("target_percent"), state.get("stop_percent"),
                entry_transaction_type=entry_transaction,
            )
            state = read_state(symbol)
            if entry_transaction == "SELL":
                try:
                    arm_short_protective_stop(symbol, state)
                except Exception as error:
                    log(f"{symbol} CRITICAL: delayed short fill has no broker stop; flattening: {error}")
                    result, payload = place_market_order(instrument, "BUY", quantity)
                    exit_order_id = result.get("data", {}).get("order_id")
                    exit_details = wait_for_order_complete(exit_order_id) if exit_order_id else {}
                    complete_exit(
                        symbol,
                        read_state(symbol),
                        exit_details,
                        entry_price,
                        "PROTECTION_FAILURE",
                        result,
                        payload,
                    )
                    return True
            clear_reentry_guard(symbol)
            return True
        return True

    if entry_transaction == "SELL" and state.get("protective_stop_order_id"):
        if protective_stop_filled(symbol, state):
            return True
        cancel_protective_stop(symbol, state)
    log(f"{symbol} state exists but no matching open position found. Clearing stale state.")
    clear_state(symbol)
    return False

def apply_trailing_stop(symbol, state, ltp):
    if ltp is None:
        return state

    entry_price = float(state.get("entry_price") or 0)
    target_price = float(state.get("target_price") or 0)
    current_stop = float(state.get("stop_loss_price") or 0)
    is_short = str(state.get("entry_transaction_type") or "BUY").upper() == "SELL"
    current_best = float(
        state.get("lowest_ltp" if is_short else "highest_ltp") or entry_price
    )

    if entry_price <= 0 or (target_price >= entry_price if is_short else target_price <= entry_price):
        return state

    ltp = float(ltp)
    best_ltp = min(current_best, ltp) if is_short else max(current_best, ltp)
    target_gap = entry_price - target_price if is_short else target_price - entry_price
    profit_from_entry = entry_price - best_ltp if is_short else best_ltp - entry_price
    target_progress = profit_from_entry / target_gap if target_gap > 0 else 0

    new_stop = current_stop
    reason = None

    # Once trade reaches 25% of expected target, reduce risk.
    if target_progress >= 0.25:
        candidate = round(entry_price * (1.01 if is_short else 0.99), 0)
        new_stop = min(new_stop, candidate) if is_short else max(new_stop, candidate)
        reason = "Trail activated: 25% target progress, risk reduced"

    # Once trade reaches 40% of expected target, move to breakeven.
    if target_progress >= 0.40:
        new_stop = min(new_stop, round(entry_price, 0)) if is_short else max(new_stop, round(entry_price, 0))
        reason = "Trail tightened: 40% target progress, stop moved to breakeven"

    # Once trade reaches 60% of expected target, lock 30% of expected profit.
    if target_progress >= 0.60:
        candidate = round(entry_price + (-target_gap if is_short else target_gap) * 0.30, 0)
        new_stop = min(new_stop, candidate) if is_short else max(new_stop, candidate)
        reason = "Trail tightened: 60% target progress, locked 30% of expected profit"

    # Once trade reaches 75% of expected target, lock 50% of expected profit.
    if target_progress >= 0.75:
        candidate = round(entry_price + (-target_gap if is_short else target_gap) * 0.50, 0)
        new_stop = min(new_stop, candidate) if is_short else max(new_stop, candidate)
        reason = "Trail tightened: 75% target progress, locked 50% of expected profit"

    # Once trade reaches 90% of expected target, lock 70% of expected profit.
    if target_progress >= 0.90:
        candidate = round(entry_price + (-target_gap if is_short else target_gap) * 0.70, 0)
        new_stop = min(new_stop, candidate) if is_short else max(new_stop, candidate)
        reason = "Trail tightened: 90% target progress, locked 70% of expected profit"

    state_changed = False

    best_improved = best_ltp < current_best if is_short else best_ltp > current_best
    if best_improved:
        state["lowest_ltp" if is_short else "highest_ltp"] = round(best_ltp, 2)
        state_changed = True

    stop_improved = new_stop < current_stop if is_short else new_stop > current_stop
    if stop_improved:
        state["stop_loss_price"] = round(new_stop, 0)
        state["trailing_stop_active"] = True
        state["trailing_stop_reason"] = reason
        state_changed = True

        log(
            f"{symbol} trailing stop updated: entry={entry_price} "
            f"ltp={ltp} best={best_ltp} target={target_price} "
            f"progress={round(target_progress * 100, 1)}% "
            f"old_stop={current_stop} new_stop={new_stop} reason={reason}"
        )

    if state_changed:
        write_state(symbol, state)
        if stop_improved and is_short and state.get("protective_stop_order_id"):
            try:
                modify_stop_order(
                    state["protective_stop_order_id"],
                    int(state["quantity"]),
                    float(state["stop_loss_price"]),
                )
                log(f"{symbol} broker protective stop modified to {state['stop_loss_price']}")
            except Exception as error:
                log(f"{symbol} protective stop modification failed; original broker stop remains: {error}")

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

    is_short = str(state.get("entry_transaction_type") or "BUY").upper() == "SELL"
    if entry_price <= 0 or (target_price >= entry_price if is_short else target_price <= entry_price):
        return False, ""

    # Give the trade some time to breathe after entry.
    if minutes_since_created(state) < 5:
        return False, ""

    # If already near target, let target/trailing-stop logic handle it.
    target_progress = (
        (entry_price - float(ltp)) / (entry_price - target_price)
        if is_short
        else (float(ltp) - entry_price) / (target_price - entry_price)
    )
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

        entry_transaction = str(state.get("entry_transaction_type") or "BUY").upper()
        position = find_matching_position_for_side(state["instrument_key"], entry_transaction)

        if position:
            qty = abs(position_quantity(position))
            instrument = {
                "instrument_key": state["instrument_key"],
                "trading_symbol": state.get("trading_symbol"),
            }

            try:
                if entry_transaction == "SELL" and cancel_protective_stop(symbol, state):
                    continue
                exit_transaction = "BUY" if entry_transaction == "SELL" else "SELL"
                result, payload = place_market_order(instrument, exit_transaction, qty)
                sell_order_id = result.get("data", {}).get("order_id")
                if not sell_order_id:
                    raise RuntimeError(f"{symbol} squareoff {exit_transaction} returned no order_id: {result}")

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
                        f"{symbol} squareoff {exit_transaction} pending; state retained. "
                        f"order_id={sell_order_id} status={order_status(sell_details)}"
                    )
            except Exception as e:
                log(f"{symbol} bot squareoff failed: {e}")
        else:
            if entry_transaction == "SELL" and state.get("protective_stop_order_id"):
                try:
                    if protective_stop_filled(symbol, state):
                        continue
                    cancel_protective_stop(symbol, state)
                except Exception as error:
                    log(f"{symbol} could not clear protective order during squareoff: {error}")
                    continue
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


def build_trade_candidate(symbol, rec, base_technicals, institutional, option_trend, transaction_type):
    direction = rec["direction"]
    atm = rec["atm"]
    option_type = option_type_for(direction, transaction_type)
    entry_price = entry_price_for(atm, direction, transaction_type)
    if entry_price <= 0:
        return None, "missing option premium"

    instrument = find_index_option_instrument(
        symbol,
        atm["expiry"],
        atm["strike"],
        option_type,
    )
    raw_flow = get_option_volume_vwap_analysis(
        instrument["instrument_key"],
        side_label=instrument["trading_symbol"],
    )
    technicals = deepcopy(base_technicals)
    technicals["raw_atm_option_flow"] = raw_flow
    technicals["atm_option_flow"] = normalize_option_flow_for_position(
        raw_flow,
        transaction_type,
    )
    technicals["institutional_flow"] = institutional

    for timeframe in ("two_hour", "fifteen_min", "five_min"):
        technicals[timeframe] = convert_index_levels_to_option_premium(
            technicals.get(timeframe, {}),
            option_side=option_type,
            option_entry_price=entry_price,
            delta=0.5,
            transaction_type=transaction_type,
        )

    option_summary = {
        "bias": direction,
        "confidence": rec["confidence"],
        "score": rec["score"],
        "strike": atm["strike"],
        "expiry": atm["expiry"],
        "entry_price": round(entry_price, 2),
        "reasons": rec.get("reasons", []),
        "trade_action": f"{transaction_type}_OPTION",
        "transaction_type": transaction_type,
        "option_type": option_type,
        "trading_symbol": instrument["trading_symbol"],
        "option_chain_trend": option_trend,
    }
    weighted = weighted_alignment_score(option_summary, technicals, option_trend)
    option_summary["weighted_alignment"] = weighted

    if transaction_type == "SELL":
        allowed, reason = short_structure_allowed(direction, technicals, raw_flow, weighted)
        if not allowed:
            return {
                "allowed": False,
                "reason": reason,
                "transaction_type": transaction_type,
                "instrument": instrument,
                "technicals": technicals,
                "option_summary": option_summary,
                "weighted": weighted,
            }, None

    score_value = float(weighted.get("score") or 0)
    minimum = MIN_SCORE_BY_SYMBOL.get(symbol, 65)
    if weighted.get("grade") == "SKIP" or (
        weighted.get("grade") == "CAUTIOUS_TRADE" and score_value < minimum
    ):
        return {
            "allowed": False,
            "reason": f"weighted score {score_value:.1f} does not qualify",
            "transaction_type": transaction_type,
            "instrument": instrument,
            "technicals": technicals,
            "option_summary": option_summary,
            "weighted": weighted,
        }, None

    cautious = weighted.get("grade") == "CAUTIOUS_TRADE"
    target_percent, stop_percent = risk_percentages(cautious=cautious)
    target, stop = option_levels_from_fill(
        entry_price,
        target_percent,
        stop_percent,
        transaction_type=transaction_type,
    )
    option_summary.update(
        {
            "target_price": target,
            "stop_loss_price": stop,
            "cautious_trade": cautious,
        }
    )
    feasibility = evaluate_trade_feasibility(
        direction,
        entry_price,
        target,
        stop,
        technicals,
        transaction_type=transaction_type,
    )
    technicals["trade_feasibility"] = feasibility
    option_summary["trade_feasibility"] = feasibility
    if not feasibility.get("allowed"):
        return {
            "allowed": False,
            "reason": "entry feasibility rejected: " + "; ".join(feasibility.get("reasons", [])),
            "transaction_type": transaction_type,
            "instrument": instrument,
            "technicals": technicals,
            "option_summary": option_summary,
            "weighted": weighted,
        }, None

    option_summary["target_price"] = float(feasibility["adjusted_target_price"])
    return {
        "allowed": True,
        "reason": "qualified",
        "transaction_type": transaction_type,
        "instrument": instrument,
        "entry_price": entry_price,
        "target_price": float(feasibility["adjusted_target_price"]),
        "stop_loss_price": float(stop),
        "target_percent": target_percent,
        "stop_percent": stop_percent,
        "technicals": technicals,
        "option_summary": option_summary,
        "weighted": weighted,
    }, None


def select_trade_candidate(candidates):
    qualified = [candidate for candidate in candidates if candidate and candidate.get("allowed")]
    if not qualified:
        return None
    buy = next((item for item in qualified if item["transaction_type"] == "BUY"), None)
    sell = next((item for item in qualified if item["transaction_type"] == "SELL"), None)
    if not sell:
        return buy
    if not buy:
        return sell
    advantage = configured_non_negative_float("SHORT_SCORE_ADVANTAGE", 5.0)
    sell_score = float(sell["weighted"].get("score") or 0)
    buy_score = float(buy["weighted"].get("score") or 0)
    return sell if sell_score >= buy_score + advantage else buy


def evaluate_symbol_buy_or_sell(symbol):
    risk_reason, risk_mode = risk_limit_mode()
    if risk_mode == "stop":
        log(f"{symbol} no trade: daily risk limit reached ({risk_reason})")
        return False

    rec = get_index_recommendation(symbol)
    record_option_chain_snapshot(symbol, rec)
    direction = rec["direction"]
    confidence = rec["confidence"]
    score = rec["score"]
    atm = rec["atm"]
    log(
        f"{symbol} signal: {direction}, confidence={confidence}, score={score}, "
        f"strike={atm['strike']}, expiry={atm['expiry']}"
    )
    observe_signal_reset(symbol, direction)
    if direction not in {"BULLISH", "BEARISH"} or confidence != "HIGH" or abs(score) < 4:
        collect_institutional_footprint(symbol, rec)
        log(f"{symbol} no trade: signal is not directional HIGH confidence.")
        return False
    blocked_reason = reentry_block_reason(symbol, direction)
    if blocked_reason:
        log(f"{symbol} no trade: {blocked_reason}")
        return False

    try:
        base_technicals = get_technical_analysis(symbol)
    except Exception as error:
        base_technicals = {
            "two_hour": {"bias": "NEUTRAL", "confidence": "LOW", "reasons": [str(error)]},
            "fifteen_min": {"bias": "NEUTRAL", "confidence": "LOW", "reasons": [str(error)]},
            "five_min": {"bias": "NEUTRAL", "confidence": "LOW", "reasons": [str(error)]},
        }
        log(f"{symbol} technical analysis failed: {error}")

    institutional = collect_institutional_footprint(symbol, rec)
    option_trend = get_option_chain_trend(symbol, direction, expiry=atm.get("expiry"))
    candidates = []
    for transaction_type in ("BUY", "SELL"):
        try:
            candidate, _ = build_trade_candidate(
                symbol,
                rec,
                base_technicals,
                institutional,
                option_trend,
                transaction_type,
            )
            if candidate:
                candidates.append(candidate)
                log(
                    f"{symbol} {transaction_type} candidate: allowed={candidate.get('allowed')} "
                    f"score={candidate.get('weighted', {}).get('score')} reason={candidate.get('reason')} "
                    f"contract={candidate.get('instrument', {}).get('trading_symbol')}"
                )
        except Exception as error:
            log(f"{symbol} {transaction_type} candidate unavailable: {error}")

    preferred = select_trade_candidate(candidates)
    if not preferred:
        best = max(candidates, key=lambda item: float(item.get("weighted", {}).get("score") or 0), default=None)
        if best:
            decision = {
                "execute_trade": False,
                "decision": "NO_TRADE",
                "confidence": "HIGH",
                "target_price": None,
                "stop_loss_price": None,
                "reason": best.get("reason"),
            }
            record_analysis(symbol, best["option_summary"], best["technicals"], decision)
        log(f"{symbol} no trade: neither BUY nor SELL structure passed deterministic gates.")
        return False

    qualified = [item for item in candidates if item and item.get("allowed")]
    ordered = [preferred] + [item for item in qualified if item is not preferred]
    live = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
    check_short_margin = live or (
        os.getenv("CHECK_SHORT_MARGIN_IN_DRY_RUN", "false").lower() == "true"
    )

    for chosen in ordered:
        transaction_type = chosen["transaction_type"]
        instrument = chosen["instrument"]
        quantity = order_quantity_for(
            symbol,
            instrument,
            chosen["entry_price"],
            chosen["stop_loss_price"],
            transaction_type=transaction_type,
        )
        if quantity <= 0:
            log(
                f"{symbol} {transaction_type} structure skipped: one lot exceeds "
                "the configured per-trade risk budget."
            )
            continue

        if transaction_type == "SELL" and check_short_margin:
            try:
                margin = validate_short_margin(
                    instrument,
                    quantity,
                    chosen["entry_price"],
                )
            except Exception as error:
                log(
                    f"{symbol} SELL structure skipped: margin validation failed: {error}; "
                    "considering the alternate qualified structure."
                )
                continue
            chosen["short_margin_check"] = margin
            log(f"{symbol} SELL candidate margin check: {margin}")
            if not margin.get("allowed"):
                log(
                    f"{symbol} SELL structure skipped: insufficient buffered margin; "
                    "considering the alternate qualified structure."
                )
                continue

        option_summary = chosen["option_summary"]
        technicals = chosen["technicals"]
        llm_decision = get_llm_decision(symbol, option_summary, technicals)
        record_analysis(symbol, option_summary, technicals, llm_decision)
        if not llm_decision.get("execute_trade") or llm_decision.get("decision") != direction:
            log(
                f"{symbol} {transaction_type} structure rejected by LLM/rules: "
                f"{llm_decision.get('reason')}; considering alternate structure."
            )
            continue

        chosen.update(
            {
                "symbol": symbol,
                "direction": direction,
                "confidence": confidence,
                "signal_score": score,
                "risk_reason": risk_reason,
                "risk_mode": risk_mode,
                "llm_decision": llm_decision,
            }
        )
        return chosen

    log(f"{symbol} no trade: every qualified BUY/SELL structure was rejected.")
    return False


def execute_selected_candidate(chosen):
    symbol = chosen["symbol"]
    direction = chosen["direction"]
    confidence = chosen["confidence"]
    score = chosen["signal_score"]
    risk_reason = chosen.get("risk_reason")
    risk_mode = chosen.get("risk_mode")
    transaction_type = chosen["transaction_type"]
    instrument = chosen["instrument"]
    entry_price = chosen["entry_price"]
    target = chosen["target_price"]
    stop = chosen["stop_loss_price"]
    quantity = order_quantity_for(
        symbol,
        instrument,
        entry_price,
        stop,
        transaction_type=transaction_type,
    )
    if quantity <= 0:
        log(f"{symbol} no trade: one lot exceeds the configured per-trade risk budget.")
        return False

    live = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
    if transaction_type == "SELL" and live:
        margin = validate_short_margin(instrument, quantity, entry_price)
        log(f"{symbol} short margin check: {margin}")
        if not margin["allowed"]:
            log(f"{symbol} no trade: insufficient buffered margin for naked option sell.")
            return False

    log(
        f"{symbol} selected {transaction_type}: {instrument['trading_symbol']} qty={quantity} "
        f"entry={entry_price} target={target} stop={stop} live={live}"
    )
    if risk_mode == "paper":
        log(
            f"{symbol} PAPER ONLY after daily risk limit: reason={risk_reason} "
            f"would_{transaction_type.lower()}={instrument['trading_symbol']} qty={quantity} "
            f"entry={entry_price} target={target} stop_loss={stop}"
        )
        return True
    if not live:
        log(f"{symbol} DRY RUN ONLY: would {transaction_type} one lot.")
        return True

    result, payload = place_market_order(instrument, transaction_type, quantity)
    order_id = result.get("data", {}).get("order_id")
    if not order_id:
        raise RuntimeError(f"{symbol} {transaction_type} returned no order_id: {result}")
    increment_trade_count(symbol)
    log(f"{symbol} MARKET {transaction_type} placed: order_id={order_id} payload={payload}")
    details = wait_for_order_complete(order_id)
    if not order_is_complete(details):
        write_state(symbol, {
            "date": now_ist().strftime("%Y-%m-%d"),
            "symbol": symbol,
            "entry_order_id": order_id,
            "entry_transaction_type": transaction_type,
            "instrument_key": instrument["instrument_key"],
            "trading_symbol": instrument["trading_symbol"],
            "quantity": int(quantity),
            "lot_size": int(instrument["lot_size"]),
            "lot_multiplier": 1,
            "target_price": target,
            "stop_loss_price": stop,
            "target_percent": chosen["target_percent"],
            "stop_percent": chosen["stop_percent"],
            "direction": direction,
            "confidence": confidence,
            "score": score,
            "status": f"{transaction_type}_PLACED_NOT_COMPLETE",
            "created_at": now_ist().isoformat(),
        })
        return True

    position = find_matching_position_for_side(instrument["instrument_key"], transaction_type)
    fill = position_avg_price(position, transaction_type) if position else None
    fill = fill or to_float(details.get("average_price")) or entry_price
    save_open_position_state(
        symbol, order_id, instrument, direction, confidence, score, fill, quantity,
        target, stop, chosen["target_percent"], chosen["stop_percent"],
        entry_transaction_type=transaction_type,
    )
    if transaction_type == "SELL":
        try:
            arm_short_protective_stop(symbol, read_state(symbol))
        except Exception as error:
            log(f"{symbol} CRITICAL: protective stop failed; flattening short immediately: {error}")
            emergency, emergency_payload = place_market_order(instrument, "BUY", quantity)
            emergency_id = emergency.get("data", {}).get("order_id")
            emergency_details = wait_for_order_complete(emergency_id) if emergency_id else {}
            complete_exit(
                symbol,
                read_state(symbol),
                emergency_details,
                fill,
                "PROTECTION_FAILURE",
                emergency,
                emergency_payload,
            )
            return True
    clear_reentry_guard(symbol)
    return True


def run_signal_check():
    if not market_window_ok():
        log("Outside trading window. No action.")
        return

    for symbol in SYMBOLS:
        state = read_state(symbol)
        if state:
            try:
                handle_existing_state(symbol, state)
            except Exception as error:
                log(f"{symbol} existing-position check ERROR: {error}")

    active = [symbol for symbol in SYMBOLS if read_state(symbol).get("instrument_key")]
    if active:
        log(f"Global one-position rule: active bot position exists in {active[0]}; no new index entry.")
        return

    try:
        untracked_index_positions = [
            position
            for position in get_open_positions()
            if position_quantity(position) != 0
            and any(
                name in str(position.get("trading_symbol") or position.get("tradingsymbol") or "").upper()
                for name in SYMBOLS
            )
        ]
        if untracked_index_positions:
            log("Global one-position rule: an untracked NIFTY/BANKNIFTY broker position exists; no bot entry.")
            return
    except Exception as error:
        log(f"Global broker-position precheck failed; no new entry for safety: {error}")
        return

    qualified = []
    for symbol in SYMBOLS:
        try:
            candidate = evaluate_symbol_buy_or_sell(symbol)
            if candidate:
                qualified.append(candidate)
        except Exception as e:
            log(f"{symbol} ERROR: {e}")

    if not qualified:
        log("Global selection: no qualified NIFTY or BANKNIFTY BUY/SELL structure.")
        return

    chosen = max(
        qualified,
        key=lambda item: (
            float(item.get("weighted", {}).get("score") or 0),
            1 if item.get("transaction_type") == "BUY" else 0,
        ),
    )
    choices = [
        (item["symbol"], item["transaction_type"], item.get("weighted", {}).get("score"))
        for item in qualified
    ]
    log(
        f"Global selection: {chosen['symbol']} {chosen['transaction_type']} chosen at "
        f"score={chosen.get('weighted', {}).get('score')} from {choices}"
    )
    execute_selected_candidate(chosen)


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
