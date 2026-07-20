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
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None

from analysis_journal import record_analysis
from banknifty_breadth import get_banknifty_breadth
from institutional_flow import (
    get_institutional_footprint,
    neutral_institutional_footprint,
)
from market_technicals import (
    get_technical_analysis,
    convert_index_levels_to_option_premium,
    get_option_volume_vwap_analysis,
)
from stock_option_scanner import scan_stock_option_candidates
from stock_futures_scanner import scan_stock_futures, write_scanner_status

from option_chain_trend import get_option_chain_trend, record_option_chain_snapshot
from signal_score import banknifty_neutral_chain_direction, weighted_alignment_score

import requests
import urllib3.util.connection as urllib3_cn

from strategy_core import get_index_recommendation, now_ist, option_chain_signal
from strategy_core import option_contract_quality
from trade_journal import record_closed_trade
from trading_config import active_value
from upstox_streams import read_market_cache, read_portfolio_cache, write_stream_instruments

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
STOCK_SCANNER_STATUS_FILE = BASE_DIR / "data" / "stock_scanner_status.json"

SYMBOLS = ["NIFTY", "BANKNIFTY"]
STOCK_FUTURE_STATE = "STOCK_FUTURE"
STOCK_OPTION_STATE = "STOCK_OPTION"
BOT_STATE_SLOTS = SYMBOLS + [STOCK_OPTION_STATE, STOCK_FUTURE_STATE]

# Used only when the corresponding environment variable is not set.
DEFAULT_LOT_MULTIPLIERS = {
    "NIFTY": 1,
    "BANKNIFTY": 1,
}

MIN_SCORE_BY_SYMBOL = {
    "NIFTY": 70,
    "BANKNIFTY": 65,
}
DEFAULT_CAUTIOUS_OVERRIDE_SCORE = 50

DEFAULT_NORMAL_TARGET_PERCENT = 10.0
DEFAULT_NORMAL_STOP_PERCENT = 7.5
DEFAULT_CAUTIOUS_TARGET_PERCENT = 6.0
DEFAULT_CAUTIOUS_STOP_PERCENT = 5.0
DEFAULT_INDEX_EXIT_POINTS = {
    "NIFTY": {"target": 30.0, "stop": 30.0},
    "BANKNIFTY": {"target": 90.0, "stop": 90.0},
}
DEFAULT_OPTION_DELTA_APPROXIMATION = 0.50
DEFAULT_MIN_TECHNICAL_REWARD_RISK = 1.0
DEFAULT_MAX_ENTRY_EXTENSION_PERCENT = 1.5
DEFAULT_RISK_SLOTS_PER_DAY = 3
DEFAULT_MIN_REENTRY_MINUTES = 0
DEFAULT_OPTION_CAPITAL_PER_ENTRY = 1.0

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

# The monitor loop can still run every second, but repeated broker reads are
# cached briefly to stay below Upstox rate limits. Order placement is never
# cached.
BROKER_READ_CACHE = {}


def broker_read_cache_seconds():
    return max(to_float(os.getenv("MONITOR_BROKER_CACHE_SECONDS"), 2.0), 0.5)


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


def configured_positive_float(env_key, default):
    value = to_float(os.getenv(env_key), default)
    if value <= 0:
        raise RuntimeError(f"{env_key} must be greater than 0")
    return value


def index_point_exit_settings(symbol):
    defaults = DEFAULT_INDEX_EXIT_POINTS[symbol]
    settings = {
        "target_points": configured_positive_float(
            f"{symbol}_TARGET_POINTS", defaults["target"]
        ),
        "stop_points": configured_positive_float(
            f"{symbol}_STOP_POINTS", defaults["stop"]
        ),
        "delta": configured_positive_float(
            "OPTION_DELTA_APPROXIMATION",
            DEFAULT_OPTION_DELTA_APPROXIMATION,
        ),
    }
    if settings["delta"] > 1:
        raise RuntimeError("OPTION_DELTA_APPROXIMATION must be greater than 0 and at most 1")
    return settings


def option_levels_from_index_points(
    symbol,
    entry_price,
    target_points=None,
    stop_points=None,
    delta=None,
):
    settings = index_point_exit_settings(symbol)
    target_points = float(target_points or settings["target_points"])
    stop_points = float(stop_points or settings["stop_points"])
    delta = float(delta or settings["delta"])
    entry = float(entry_price)
    return {
        "target_price": round(entry + target_points * delta, 2),
        "stop_loss_price": round(max(entry - stop_points * delta, 0.05), 2),
        "target_points": target_points,
        "stop_points": stop_points,
        "delta": delta,
    }


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


def revalidate_option_after_fill(
    symbol,
    direction,
    option_type,
    fill_price,
    target_points,
    stop_points,
    delta,
    technicals,
):
    """Recalculate levels and feasibility using the actual market fill."""
    fill = float(fill_price)
    levels = option_levels_from_index_points(
        symbol,
        fill,
        target_points=target_points,
        stop_points=stop_points,
        delta=delta,
    )
    delta = levels["delta"]
    recalculated_technicals = deepcopy(technicals or {})
    for timeframe in ("two_hour", "fifteen_min", "five_min"):
        recalculated_technicals[timeframe] = convert_index_levels_to_option_premium(
            recalculated_technicals.get(timeframe, {}),
            option_side=option_type,
            option_entry_price=fill,
            delta=delta,
            transaction_type="BUY",
        )

    target = levels["target_price"]
    stop = levels["stop_loss_price"]
    feasibility = evaluate_trade_feasibility(
        direction,
        fill,
        target,
        stop,
        recalculated_technicals,
        transaction_type="BUY",
    )
    recalculated_technicals["trade_feasibility"] = feasibility
    return {
        "allowed": bool(feasibility.get("allowed")),
        "target_price": float(target),
        "stop_loss_price": float(stop),
        "target_points": levels["target_points"],
        "stop_points": levels["stop_points"],
        "delta": levels["delta"],
        "technicals": recalculated_technicals,
        "feasibility": feasibility,
    }


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


@contextmanager
def protective_stop_lock(symbol):
    """Serialize broker-stop creation across entry and monitor processes."""
    lock_path = BASE_DIR / f".{symbol.lower()}_protective_stop.lock"
    with lock_path.open("a+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_reentry_guard(symbol):
    guard = read_json(reentry_guard_file(symbol), {})
    if guard.get("date") != now_ist().strftime("%Y-%m-%d"):
        return {}
    return guard


def write_reentry_guard(symbol, guard):
    write_json(reentry_guard_file(symbol), guard)


def clear_reentry_guard(symbol):
    write_reentry_guard(symbol, {})


def loss_reentry_mode():
    """Control whether a losing exit needs a reset, cooldown, or no guard."""
    mode = os.getenv("LOSS_REENTRY_MODE", "cooldown").strip().lower()
    aliases = {
        "none": "off",
        "disabled": "off",
        "time": "cooldown",
        "signal_reset": "reset",
    }
    mode = aliases.get(mode, mode)
    if mode not in {"off", "cooldown", "reset"}:
        log(
            f"Invalid LOSS_REENTRY_MODE={mode!r}; using 'cooldown'. "
            "Allowed values: off, cooldown, reset"
        )
        return "cooldown"
    return mode


def register_losing_exit_guard(symbol, state, journal_row, exit_reason):
    if exit_reason not in {"STOP_LOSS", "SENTIMENT_EXIT"}:
        return
    if to_float(journal_row.get("gross_pnl")) >= 0:
        return

    mode = loss_reentry_mode()
    if mode == "off":
        clear_reentry_guard(symbol)
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
                "mode": mode,
            },
        )
        cooldown_minutes = configured_non_negative_float(
            "MIN_REENTRY_MINUTES",
            DEFAULT_MIN_REENTRY_MINUTES,
        )
        reset_note = "signal reset required" if mode == "reset" else "signal reset not required"
        log(
            f"{symbol} re-entry guard armed after losing {exit_reason}: "
            f"direction={state.get('direction')}; mode={mode}; "
            f"cooldown={cooldown_minutes:.1f}m; {reset_note}"
        )
    except Exception as error:
        log(f"{symbol} could not persist re-entry guard: {error}")


def observe_signal_reset(symbol, direction):
    if loss_reentry_mode() != "reset":
        return

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
    mode = loss_reentry_mode()
    if mode == "off":
        return ""

    guard = read_reentry_guard(symbol)
    if guard.get("blocked_direction") != direction:
        return ""

    if mode == "reset" and not guard.get("reset_seen"):
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
    clear_reentry_guard(symbol)
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


def stop_after_first_profit_or_loss_enabled():
    return os.getenv("STOP_AFTER_FIRST_PROFIT_OR_LOSS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def stop_after_first_outcome_enabled(outcome):
    """Allow profit and loss daily guards to be configured independently."""
    env_name = f"STOP_AFTER_FIRST_{str(outcome).upper()}"
    specific_value = os.getenv(env_name)
    if specific_value is None:
        return stop_after_first_profit_or_loss_enabled()
    return specific_value.strip().lower() in {"1", "true", "yes", "on"}


def first_index_trade_outcome_today(symbol):
    """Return the first closed outcome whose configured daily guard is enabled."""
    if not TRADE_HISTORY_FILE.exists():
        return None

    today = now_ist().strftime("%Y-%m-%d")
    with TRADE_HISTORY_FILE.open("r", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("trade_date")) != today:
                continue
            if str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            if str(row.get("instrument_class") or "INDEX_OPTION").upper() != "INDEX_OPTION":
                continue
            pnl = to_float(row.get("gross_pnl"))
            if pnl > 0 and stop_after_first_outcome_enabled("PROFIT"):
                return {"outcome": "PROFIT", "gross_pnl": pnl, "row": row}
            if pnl < 0 and stop_after_first_outcome_enabled("LOSS"):
                return {"outcome": "LOSS", "gross_pnl": pnl, "row": row}
    return None


def daily_index_entry_block_reason(symbol):
    outcome = first_index_trade_outcome_today(symbol)
    if not outcome:
        return ""
    return (
        f"daily first-outcome guard active after {outcome['outcome']}: "
        f"gross_pnl={outcome['gross_pnl']:.2f}; no further {symbol} entries today"
    )


def bot_unrealized_pnl():
    states = [read_state(symbol) for symbol in BOT_STATE_SLOTS]
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
    fallback = to_float(os.getenv("DAILY_PROFIT_TARGET"), 0)
    return to_float(active_value("dailyProfitTarget", fallback), fallback)


def after_profit_target_mode():
    return os.getenv("AFTER_PROFIT_TARGET_MODE", "paper").strip().lower()


def daily_profit_target_reached():
    target = daily_profit_target()

    if target <= 0:
        return False

    return today_realized_pnl() >= target


def daily_max_loss():
    fallback = to_float(os.getenv("DAILY_MAX_LOSS"), 0)
    return to_float(active_value("dailyMaxLoss", fallback), fallback)


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

def option_capital_per_entry():
    """Return the capital allocation for one index-option entry.

    A value of 1 is a sentinel for exactly one lot. Larger values are treated
    as rupees and converted to whole lots using the expected option premium.
    """
    fallback = os.getenv(
        "OPTION_CAPITAL_PER_ENTRY",
        str(DEFAULT_OPTION_CAPITAL_PER_ENTRY),
    ).strip()
    raw_value = str(active_value("optionCapitalPerEntry", fallback)).strip()
    try:
        capital = float(raw_value)
    except ValueError as error:
        raise RuntimeError(
            "OPTION_CAPITAL_PER_ENTRY must be 1 or a positive rupee amount"
        ) from error

    if capital < 1:
        raise RuntimeError(
            "OPTION_CAPITAL_PER_ENTRY must be 1 or greater"
        )
    return capital


def stock_option_rupee_levels(entry_price, quantity):
    """Convert fixed one-lot rupee reward/risk into option-premium levels."""
    entry = float(entry_price)
    quantity = int(quantity)
    if entry <= 0 or quantity <= 0:
        raise RuntimeError("Stock-option entry price and quantity must be positive")
    target_rupees = configured_non_negative_float("STOCK_OPTION_TARGET_RUPEES", 5000.0)
    stop_rupees = configured_non_negative_float("STOCK_OPTION_STOP_RUPEES", 5000.0)
    if target_rupees <= 0 or stop_rupees <= 0:
        raise RuntimeError("STOCK_OPTION_TARGET_RUPEES and STOCK_OPTION_STOP_RUPEES must be positive")
    tick = configured_non_negative_float("STOCK_OPTION_TICK_SIZE", 0.05) or 0.05

    def rounded(value):
        return round(round(value / tick) * tick, 2)

    return {
        "target_price": rounded(entry + target_rupees / quantity),
        "stop_loss_price": max(rounded(entry - stop_rupees / quantity), tick),
        "target_rupees": target_rupees,
        "stop_rupees": stop_rupees,
    }


def max_lots_per_entry():
    """Optional hard ceiling; zero means no additional lot ceiling."""
    raw_value = os.getenv("MAX_LOTS_PER_ENTRY", "0").strip()
    try:
        maximum = int(raw_value)
    except ValueError as error:
        raise RuntimeError("MAX_LOTS_PER_ENTRY must be a whole number or 0") from error
    if maximum < 0:
        raise RuntimeError("MAX_LOTS_PER_ENTRY cannot be negative")
    return maximum


def order_quantity_for(
    symbol,
    instrument,
    entry_price=None,
    stop_loss_price=None,
    transaction_type="BUY",
):
    lot_size = int(instrument["lot_size"])
    capital = option_capital_per_entry()

    # The only supported live entry is a long option BUY. Keep this guard so
    # an accidental legacy caller cannot use this sizing for a short option.
    if str(transaction_type).upper() != "BUY":
        return 0

    if capital == 1 or entry_price is None or float(entry_price) <= 0:
        lots = 1
    else:
        value_per_lot = float(entry_price) * lot_size
        lots = int(capital // value_per_lot)

    maximum = max_lots_per_entry()
    if maximum > 0:
        lots = min(lots, maximum)

    return lot_size * max(lots, 0)


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


def validate_stock_future_margin(instrument, transaction_type, quantity, price):
    required = estimate_order_margin(instrument, transaction_type, quantity, price)
    available = available_equity_margin()
    buffer_percent = configured_non_negative_float(
        "STOCK_FUTURES_MARGIN_BUFFER_PERCENT",
        20.0,
    )
    if buffer_percent >= 100:
        raise RuntimeError("STOCK_FUTURES_MARGIN_BUFFER_PERCENT must be less than 100")
    usable = available * (1 - buffer_percent / 100)
    return {
        "allowed": required > 0 and required <= usable,
        "required_margin": round(required, 2),
        "available_margin": round(available, 2),
        "usable_margin_after_buffer": round(usable, 2),
        "buffer_percent": round(buffer_percent, 2),
    }


def get_order_details(order_id, force=False):
    cache_key = f"order:{order_id}"
    cached = BROKER_READ_CACHE.get(cache_key)
    now = time_module.monotonic()
    if (
        not force
        and cached
        and now - cached["at"] < broker_read_cache_seconds()
    ):
        return deepcopy(cached["data"])

    # Prefer a very recent portfolio-stream event when it carries this order.
    # REST remains the authoritative fallback for older or incomplete events.
    stream = read_portfolio_cache()
    if stream and time_module.time() - to_float(stream.get("received_at"), 0) <= 15:
        def find_event(value):
            if isinstance(value, dict):
                event_order_id = str(value.get("order_id") or value.get("orderId") or "")
                if event_order_id == str(order_id) and value.get("status"):
                    return value
                for child in value.values():
                    found = find_event(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = find_event(child)
                    if found:
                        return found
            return None

        event = find_event(stream.get("message") or {})
        if event:
            BROKER_READ_CACHE[cache_key] = {"at": time_module.monotonic(), "data": deepcopy(event)}
            return deepcopy(event)

    try:
        result = upstox_request("GET", UPSTOX_ORDER_DETAILS_URL, params={"order_id": order_id})
    except RuntimeError as error:
        if cached and " 429:" in str(error):
            return deepcopy(cached["data"])
        raise
    data = result.get("data", {})

    if isinstance(data, list):
        data = data[0] if data else {}

    data = data or {}
    BROKER_READ_CACHE[cache_key] = {"at": time_module.monotonic(), "data": deepcopy(data)}
    return data


def wait_for_order_complete(order_id, attempts=5, delay_seconds=2):
    latest = {}

    for _ in range(attempts):
        latest = get_order_details(order_id, force=True)
        status = str(latest.get("status", "")).lower()

        if status in {"complete", "completed", "traded", "rejected", "cancelled", "canceled"}:
            return latest

        time_module.sleep(delay_seconds)

    return latest


def get_open_positions(force=False):
    cache_key = "positions"
    cached = BROKER_READ_CACHE.get(cache_key)
    now = time_module.monotonic()
    if (
        not force
        and cached
        and now - cached["at"] < broker_read_cache_seconds()
    ):
        return deepcopy(cached["data"])

    try:
        result = upstox_request("GET", UPSTOX_POSITIONS_URL)
    except RuntimeError as error:
        if cached and " 429:" in str(error):
            return deepcopy(cached["data"])
        raise
    data = result.get("data", []) or []
    BROKER_READ_CACHE[cache_key] = {"at": time_module.monotonic(), "data": deepcopy(data)}
    return data


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
        age_seconds = time_module.time() - INSTRUMENT_CACHE.stat().st_mtime
        if age_seconds < 12 * 60 * 60:
            return

    log("Refreshing Upstox instrument file...")
    temporary = INSTRUMENT_CACHE.with_suffix(".download")
    try:
        urllib.request.urlretrieve(UPSTOX_INSTRUMENTS_URL, temporary)
        temporary.replace(INSTRUMENT_CACHE)
    finally:
        if temporary.exists():
            temporary.unlink()


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
    instrument_class="INDEX_OPTION",
    underlying_symbol=None,
    target_points=None,
    stop_points=None,
    option_delta_used=None,
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
            precision = 2 if instrument_class in {"STOCK_FUTURE", "STOCK_OPTION"} else 0
            target_price = round(float(target_price), precision)
            stop_loss_price = round(float(stop_loss_price), precision)

    state = {
        "date": now_ist().strftime("%Y-%m-%d"),
        "symbol": symbol,
        "entry_order_id": order_id,
        "buy_order_id": order_id if entry_transaction_type == "BUY" else None,
        "entry_transaction_type": entry_transaction_type,
        "exit_transaction_type": "BUY" if entry_transaction_type == "SELL" else "SELL",
        "position_side": (
            "SHORT_FUTURE" if instrument_class == "STOCK_FUTURE" and entry_transaction_type == "SELL"
            else "LONG_FUTURE" if instrument_class == "STOCK_FUTURE"
            else "SHORT_OPTION" if entry_transaction_type == "SELL"
            else "LONG_OPTION"
        ),
        "instrument_class": instrument_class,
        "underlying_symbol": underlying_symbol or instrument.get("underlying_symbol") or symbol,
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
        "planned_target_price": target_price,
        "stop_loss_price": stop_loss_price,
        "original_stop_loss_price": stop_loss_price,
        "target_percent": target_percent,
        "stop_percent": stop_percent,
        "target_points": target_points,
        "stop_points": stop_points,
        "option_delta_used": option_delta_used,
        "status": "POSITION_OPEN",
        "created_at": now_ist().isoformat(),
        "highest_ltp": round(float(entry_price), 2),
        "lowest_ltp": round(float(entry_price), 2),
        "profit_protection_stage": 0,
        "target_progress_percent": 0.0,
        "trailing_stop_active": False,
        "trailing_stop_reason": "",
    }
    state["profit_booking_percent"] = profit_booking_target_percent()
    state["profit_booking_price"] = profit_booking_price(state)

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

def run_position_monitor(log_empty=True):
    for symbol in BOT_STATE_SLOTS:
        try:
            state = read_state(symbol)

            if not state:
                if log_empty:
                    log(f"{symbol} monitor: no open bot state.")
                continue

            handle_existing_state(symbol, state, verbose=log_empty)

        except Exception as e:
            log(f"{symbol} monitor ERROR: {e}")


def run_position_monitor_loop():
    """Monitor bot positions every five seconds while the Indian market is open."""
    log("Position monitor loop started: five-second checks enabled.")
    while True:
        current = now_ist().time()
        if current > time(15, 30):
            log("Position monitor loop stopped at 03:30 PM IST.")
            return

        if time(9, 20) <= current <= time(15, 30):
            run_position_monitor(log_empty=False)
        time_module.sleep(5)

def arm_protective_stop(symbol, state):
    instrument = {
        "instrument_key": state["instrument_key"],
        "trading_symbol": state.get("trading_symbol"),
    }
    exit_transaction = state.get("exit_transaction_type") or (
        "BUY" if str(state.get("entry_transaction_type") or "BUY").upper() == "SELL" else "SELL"
    )
    result, payload = place_stop_market_order(
        instrument,
        exit_transaction,
        int(state["quantity"]),
        float(state["stop_loss_price"]),
    )
    order_id = result.get("data", {}).get("order_id")
    if not order_id:
        raise RuntimeError(f"protective {exit_transaction} stop returned no order_id: {result}")
    details = wait_for_order_complete(order_id, attempts=1, delay_seconds=0)
    if order_is_rejected(details):
        raise RuntimeError(
            f"protective {exit_transaction} stop was {order_status(details)}: {details}"
        )
    state["protective_stop_order_id"] = order_id
    write_state(symbol, state)
    log(f"{symbol} broker protective {exit_transaction} stop armed: order_id={order_id} payload={payload}")
    return state


def ensure_protective_stop(symbol, state):
    """Arm exactly one broker stop even when entry and monitor overlap."""
    if state.get("protective_stop_order_id"):
        return state

    with protective_stop_lock(symbol):
        fresh_state = read_state(symbol)
        if fresh_state.get("protective_stop_order_id"):
            return fresh_state

        # A pending marker without an order id is stale once this lock is
        # acquired, because any active arming process would still hold it.
        fresh_state["protective_stop_pending"] = True
        write_state(symbol, fresh_state)
        try:
            armed_state = arm_protective_stop(symbol, fresh_state)
            armed_state.pop("protective_stop_pending", None)
            write_state(symbol, armed_state)
            return armed_state
        except Exception:
            failed_state = read_state(symbol)
            failed_state.pop("protective_stop_pending", None)
            write_state(symbol, failed_state)
            raise


def arm_short_protective_stop(symbol, state):
    """Backward-compatible name used by existing option-selling paths."""
    return arm_protective_stop(symbol, state)


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

    # A cancel response only acknowledges the request. Confirm that the
    # broker stop is actually cancelled before submitting a second exit.
    details = wait_for_order_complete(order_id, attempts=3, delay_seconds=0.25)
    if order_is_complete(details):
        complete_exit(symbol, state, details, state.get("stop_loss_price"), "STOP_LOSS")
        return True
    if not order_is_rejected(details):
        log(
            f"{symbol} protective stop cancellation is still pending; "
            f"deferring market exit order_id={order_id} status={order_status(details)}"
        )
        return True

    state.pop("protective_stop_order_id", None)
    write_state(symbol, state)
    log(f"{symbol} protective stop cancelled before active exit: order_id={order_id}")
    return False


def handle_existing_state(symbol, state, verbose=True):
    instrument_key = state.get("instrument_key")
    if not instrument_key:
        return False

    entry_transaction = str(state.get("entry_transaction_type") or "BUY").upper()
    exit_transaction = "BUY" if entry_transaction == "SELL" else "SELL"

    needs_broker_stop = entry_transaction == "SELL" or state.get("instrument_class") == "STOCK_FUTURE"
    if needs_broker_stop and protective_stop_filled(symbol, state):
        return True

    # A stop cancelled in the broker UI can remain in the local state file.
    # Remove that stale id so the guarded arming path creates one replacement.
    if needs_broker_stop and state.get("protective_stop_order_id"):
        stop_details = get_order_details(state["protective_stop_order_id"])
        if order_is_rejected(stop_details):
            log(
                f"{symbol} protective stop is {order_status(stop_details)}; "
                "clearing stale stop id before re-arming"
            )
            state.pop("protective_stop_order_id", None)
            write_state(symbol, state)

    if state.get("status") == "EXIT_PENDING":
        if monitor_pending_exit(symbol, state):
            return True
        state = read_state(symbol)

    position = find_matching_position_for_side(instrument_key, entry_transaction)
    if position:
        ltp = position_ltp(position)
        qty = abs(position_quantity(position))
        if needs_broker_stop and not state.get("protective_stop_order_id"):
            try:
                state = ensure_protective_stop(symbol, state)
            except Exception as error:
                log(f"{symbol} CRITICAL: position has no broker stop; flattening now: {error}")
                instrument = {"instrument_key": instrument_key, "trading_symbol": state.get("trading_symbol")}
                result, payload = place_market_order(instrument, exit_transaction, qty)
                order_id = result.get("data", {}).get("order_id")
                details = wait_for_order_complete(order_id) if order_id else {}
                complete_exit(symbol, state, details, ltp, "PROTECTION_FAILURE", result, payload)
                return True
        if ltp is not None:
            state = apply_trailing_stop(symbol, state, ltp)
        target_price = float(state.get("target_price"))
        stop_loss_price = float(state.get("stop_loss_price"))
        is_short = entry_transaction == "SELL"
        booking_price = profit_booking_price(state)
        if to_float(state.get("profit_booking_price")) != booking_price:
            state["profit_booking_percent"] = profit_booking_target_percent()
            state["profit_booking_price"] = booking_price
            state["trailing_stop_active"] = False
            state["trailing_stop_reason"] = ""
            write_state(symbol, state)

        if verbose:
            log(
                f"{symbol} open {state.get('position_side', 'LONG_OPTION')} active: "
                f"{state.get('trading_symbol')} qty={qty} ltp={ltp} "
                f"entry={state.get('entry_price')} planned_target={target_price} "
                f"book_profit_at={booking_price} stop_loss={stop_loss_price}"
            )

        sentiment_exit = False
        sentiment_reason = ""
        if sentiment_check_due(state):
            state["last_sentiment_check_at"] = now_ist().isoformat()
            write_state(symbol, state)
            sentiment_exit, sentiment_reason = should_exit_on_sentiment_change(symbol, state, ltp)
        target_hit = ltp is not None and (ltp <= booking_price if is_short else ltp >= booking_price)
        stop_hit = ltp is not None and (ltp >= stop_loss_price if is_short else ltp <= stop_loss_price)
        if ltp is not None and (target_hit or stop_hit or sentiment_exit):
            # Stock-future and short-option stops are already protected at the
            # broker. Do not send a second market exit when the local LTP also
            # reaches the stop; let the broker stop fill and confirm it here.
            if stop_hit and needs_broker_stop and state.get("protective_stop_order_id"):
                log(
                    f"{symbol} local stop reached; waiting for broker protective stop "
                    f"order_id={state['protective_stop_order_id']}"
                )
                return True

            if sentiment_exit:
                exit_reason = "SENTIMENT_EXIT"
                log(f"{symbol} sentiment exit triggered: {sentiment_reason}")
            else:
                exit_reason = "TARGET" if target_hit else "STOP_LOSS"

            # Publish the exit state before making broker calls. The separate
            # entry and monitor cron jobs can overlap, so this prevents both
            # processes from submitting the same exit order.
            state["status"] = "EXIT_PENDING"
            state["exit_reason"] = exit_reason
            write_state(symbol, state)

            if needs_broker_stop and cancel_protective_stop(symbol, state):
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
            instrument_class = state.get("instrument_class", "INDEX_OPTION")
            pending_technical_context = state.get("technical_context") or {}
            pending_option_type = state.get("option_type")
            if instrument_class == "STOCK_OPTION":
                fixed_levels = stock_option_rupee_levels(entry_price, quantity)
                state["target_price"] = fixed_levels["target_price"]
                state["stop_loss_price"] = fixed_levels["stop_loss_price"]
            save_open_position_state(
                symbol, entry_order_id, instrument, state.get("direction"),
                state.get("confidence"), state.get("score"), entry_price, quantity,
                state.get("target_price"), state.get("stop_loss_price"),
                state.get("target_percent"), state.get("stop_percent"),
                entry_transaction_type=entry_transaction,
                instrument_class=instrument_class,
                underlying_symbol=state.get("underlying_symbol"),
                target_points=state.get("target_points"),
                stop_points=state.get("stop_points"),
                option_delta_used=state.get("option_delta_used"),
            )
            state = read_state(symbol)
            if instrument_class == "STOCK_OPTION":
                state.update(
                    {
                        "option_type": pending_option_type,
                        "technical_context": pending_technical_context,
                        "fixed_target_rupees": fixed_levels["target_rupees"],
                        "fixed_stop_rupees": fixed_levels["stop_rupees"],
                    }
                )
                write_state(symbol, state)
                clear_reentry_guard(symbol)
                log(
                    f"{symbol} delayed stock-option fill levels set: entry={entry_price} "
                    f"target={state['target_price']} stop_loss={state['stop_loss_price']}"
                )
                return True
            technical_context = state.get("technical_context") or {}
            option_type = state.get("option_type") or option_type_for(
                state.get("direction"), "BUY"
            )
            post_fill = revalidate_option_after_fill(
                symbol,
                state.get("direction"),
                option_type,
                entry_price,
                state.get("target_points"),
                state.get("stop_points"),
                state.get("option_delta_used"),
                technical_context,
            )
            state["post_fill_feasibility"] = post_fill["feasibility"]
            if not post_fill["allowed"]:
                reason = "; ".join(post_fill["feasibility"].get("reasons", []))
                state["status"] = "EXIT_PENDING"
                state["exit_reason"] = "POST_FILL_GUARDRAIL"
                state["post_fill_guardrail_reason"] = reason
                state["target_price"] = post_fill["target_price"]
                state["stop_loss_price"] = post_fill["stop_loss_price"]
                write_state(symbol, state)
                log(
                    f"{symbol} delayed-fill guardrail rejected position: fill={entry_price} "
                    f"reason={reason}; flattening immediately"
                )
                instrument = {
                    "instrument_key": instrument_key,
                    "trading_symbol": state.get("trading_symbol"),
                }
                exit_result, exit_payload = place_market_order(
                    instrument, "SELL", quantity
                )
                exit_order_id = exit_result.get("data", {}).get("order_id")
                if not exit_order_id:
                    raise RuntimeError(
                        f"{symbol} delayed-fill guardrail exit returned no order_id"
                    )
                exit_details = wait_for_order_complete(exit_order_id)
                if order_is_complete(exit_details):
                    complete_exit(
                        symbol,
                        state,
                        exit_details,
                        entry_price,
                        "POST_FILL_GUARDRAIL",
                        exit_result,
                        exit_payload,
                    )
                else:
                    state["exit_order_id"] = exit_order_id
                    state["exit_fallback_price"] = entry_price
                    write_state(symbol, state)
                    log(
                        f"{symbol} delayed-fill guardrail exit pending: "
                        f"order_id={exit_order_id}"
                    )
                return True

            state.update(
                {
                    "target_price": post_fill["target_price"],
                    "stop_loss_price": post_fill["stop_loss_price"],
                    "original_stop_loss_price": post_fill["stop_loss_price"],
                    "technical_context": post_fill["technicals"],
                }
            )
            write_state(symbol, state)
            log(
                f"{symbol} delayed-fill levels validated: fill={entry_price} "
                f"target={post_fill['target_price']} "
                f"stop_loss={post_fill['stop_loss_price']} "
                f"reward_risk={post_fill['feasibility'].get('technical_reward_risk')}"
            )
            if needs_broker_stop:
                try:
                    ensure_protective_stop(symbol, state)
                except Exception as error:
                    log(f"{symbol} CRITICAL: delayed fill has no broker stop; flattening: {error}")
                    result, payload = place_market_order(instrument, exit_transaction, quantity)
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

    if needs_broker_stop and state.get("protective_stop_order_id"):
        if protective_stop_filled(symbol, state):
            return True
        cancel_protective_stop(symbol, state)
    if verbose:
        log(f"{symbol} state exists but no matching open position found. Clearing stale state.")
    clear_state(symbol)
    return False

def profit_booking_target_percent():
    value = configured_non_negative_float("PROFIT_BOOKING_TARGET_PERCENT", 80.0)
    if value <= 0 or value > 100:
        raise RuntimeError("PROFIT_BOOKING_TARGET_PERCENT must be greater than 0 and at most 100")
    return value


def profit_protection_settings():
    """Return validated, staged profit-protection thresholds."""
    enabled = os.getenv("PROFIT_PROTECTION_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    settings = {
        "enabled": enabled,
        "stage_one_trigger": configured_positive_float(
            "PROFIT_PROTECTION_STAGE_ONE_TRIGGER_PERCENT", 60.0
        ),
        "stage_one_lock": configured_non_negative_float(
            "PROFIT_PROTECTION_STAGE_ONE_LOCK_PERCENT", 20.0
        ),
        "stage_two_trigger": configured_positive_float(
            "PROFIT_PROTECTION_STAGE_TWO_TRIGGER_PERCENT", 70.0
        ),
        "stage_two_lock": configured_non_negative_float(
            "PROFIT_PROTECTION_STAGE_TWO_LOCK_PERCENT", 35.0
        ),
        "booking_trigger": profit_booking_target_percent(),
    }
    if not (
        0 <= settings["stage_one_lock"] < settings["stage_one_trigger"]
        < settings["stage_two_trigger"] < settings["booking_trigger"] <= 100
    ):
        raise RuntimeError(
            "Profit-protection triggers must satisfy 0 <= stage-one lock < "
            "stage-one trigger < stage-two trigger < booking trigger <= 100"
        )
    if not (
        settings["stage_one_lock"] <= settings["stage_two_lock"]
        < settings["stage_two_trigger"]
    ):
        raise RuntimeError(
            "Profit-protection locks must satisfy stage-one lock <= stage-two "
            "lock < stage-two trigger"
        )
    return settings


def profit_booking_price(state):
    """Return the premium that represents the configured share of target progress."""
    entry_price = float(state.get("entry_price") or 0)
    target_price = float(state.get("planned_target_price") or state.get("target_price") or 0)
    is_short = str(state.get("entry_transaction_type") or "BUY").upper() == "SELL"
    if entry_price <= 0 or (target_price >= entry_price if is_short else target_price <= entry_price):
        return target_price

    progress = profit_booking_target_percent() / 100.0
    booking_price = entry_price + (target_price - entry_price) * progress
    precision = 2 if state.get("instrument_class") in {"STOCK_FUTURE", "STOCK_OPTION"} else 0
    return round(booking_price, precision)


def apply_trailing_stop(symbol, state, ltp):
    """Apply two one-time profit locks before the 80% profit-booking exit.

    The original stop remains untouched below 60% target progress. At 60%
    progress the stop protects 20% of the planned move, and at 70% progress it
    protects 35%. The stop never moves backwards.
    """
    if ltp is None or state.get("instrument_class") not in {
        "INDEX_OPTION", "STOCK_OPTION",
    }:
        return state

    settings = profit_protection_settings()
    if not settings["enabled"]:
        return state

    entry = to_float(state.get("entry_price"))
    target = to_float(state.get("planned_target_price") or state.get("target_price"))
    current_stop = to_float(state.get("stop_loss_price"))
    current_ltp = to_float(ltp)
    if not entry or not target or not current_stop or not current_ltp:
        return state

    is_short = str(state.get("entry_transaction_type") or "BUY").upper() == "SELL"
    planned_move = entry - target if is_short else target - entry
    favorable_move = entry - current_ltp if is_short else current_ltp - entry
    if planned_move <= 0:
        return state

    progress = max(favorable_move / planned_move * 100.0, 0.0)
    current_stage = int(to_float(state.get("profit_protection_stage"), 0) or 0)
    new_stage = current_stage
    lock_percent = None
    if progress >= settings["stage_two_trigger"] and current_stage < 2:
        new_stage = 2
        lock_percent = settings["stage_two_lock"]
    elif progress >= settings["stage_one_trigger"] and current_stage < 1:
        new_stage = 1
        lock_percent = settings["stage_one_lock"]

    state_changed = False
    if is_short:
        lowest = min(to_float(state.get("lowest_ltp"), entry), current_ltp)
        if lowest != to_float(state.get("lowest_ltp"), entry):
            state["lowest_ltp"] = round(lowest, 2)
            state_changed = True
    else:
        highest = max(to_float(state.get("highest_ltp"), entry), current_ltp)
        if highest != to_float(state.get("highest_ltp"), entry):
            state["highest_ltp"] = round(highest, 2)
            state_changed = True

    if lock_percent is not None:
        locked_move = planned_move * lock_percent / 100.0
        proposed_stop = entry - locked_move if is_short else entry + locked_move
        proposed_stop = round(proposed_stop, 2)
        improved_stop = min(current_stop, proposed_stop) if is_short else max(current_stop, proposed_stop)
        if improved_stop != current_stop:
            state["stop_loss_price"] = improved_stop
            state_changed = True
        state["profit_protection_stage"] = new_stage
        state["trailing_stop_active"] = True
        state["trailing_stop_reason"] = (
            f"stage {new_stage}: progress={progress:.1f}% lock={lock_percent:.1f}%"
        )
        state_changed = True
        log(
            f"{symbol} staged profit protection activated: stage={new_stage} "
            f"progress={progress:.1f}% ltp={current_ltp} "
            f"stop={state['stop_loss_price']} lock={lock_percent:.1f}%"
        )

    if state_changed:
        state["target_progress_percent"] = round(progress, 2)
        write_state(symbol, state)
    return state

def minutes_since_created(state):
    try:
        created_at = datetime.fromisoformat(state.get("created_at"))
        return (now_ist() - created_at).total_seconds() / 60
    except Exception:
        return 999


def sentiment_check_due(state):
    """Throttle expensive option-chain sentiment checks inside the 1-second monitor."""
    if state.get("instrument_class") in {"STOCK_FUTURE", "STOCK_OPTION"}:
        return False
    interval = max(
        int(float(os.getenv("MONITOR_SENTIMENT_INTERVAL_SECONDS", "60"))),
        1,
    )
    raw = state.get("last_sentiment_check_at")
    if not raw:
        return True
    try:
        last_check = datetime.fromisoformat(raw)
        return (now_ist() - last_check).total_seconds() >= interval
    except Exception:
        return True


def should_exit_on_sentiment_change(symbol, state, ltp):
    if state.get("instrument_class") in {"STOCK_FUTURE", "STOCK_OPTION"}:
        return False, ""
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
    for symbol in BOT_STATE_SLOTS:
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
                if state.get("protective_stop_order_id") and cancel_protective_stop(symbol, state):
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
            if state.get("protective_stop_order_id"):
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
    """Compatibility rule used by offline counterfactual research only."""
    score_value = float(weighted_score.get("score") or 0)
    fifteen = technicals.get("fifteen_min", {}) or {}
    five = technicals.get("five_min", {}) or {}
    atm_flow = technicals.get("atm_option_flow", {}) or {}
    institutional = technicals.get("institutional_flow", {}) or {}
    atm_close = float(atm_flow.get("close") or 0)
    atm_vwap = float(atm_flow.get("vwap") or 999999)

    return (
        score_value >= DEFAULT_CAUTIOUS_OVERRIDE_SCORE
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
    if str(transaction_type).upper() != "BUY":
        return {
            "allowed": False,
            "reason": "Only long option buying is enabled.",
            "transaction_type": "BUY",
        }, None
    direction = rec["direction"]
    atm = rec["atm"]
    option_type = option_type_for(direction, transaction_type)
    entry_price = entry_price_for(atm, direction, transaction_type)
    if entry_price <= 0:
        return None, "missing option premium"
    exit_settings = index_point_exit_settings(symbol)

    instrument = find_index_option_instrument(
        symbol,
        atm["expiry"],
        atm["strike"],
        option_type,
    )
    stream_quote = read_market_cache(instrument.get("instrument_key"))
    option_quality = option_contract_quality(atm, option_type, stream_quote)
    option_quality["instrument_key"] = instrument.get("instrument_key")
    option_quality["max_spread_percent"] = to_float(
        os.getenv("MAX_OPTION_SPREAD_PERCENT"), 2.5
    )
    option_quality["depth_filter_enabled"] = os.getenv(
        "OPTION_DEPTH_FILTER", "false"
    ).lower() == "true"
    option_quality["greeks_filter_enabled"] = os.getenv(
        "OPTION_GREEKS_FILTER", "true"
    ).lower() == "true"
    min_delta = to_float(os.getenv("MIN_OPTION_DELTA"), 0.20)
    max_delta = to_float(os.getenv("MAX_OPTION_DELTA"), 0.80)
    option_quality["entry_allowed"] = True
    quality_reasons = []
    if option_quality.get("spread_percent") is not None and option_quality["spread_percent"] > option_quality["max_spread_percent"]:
        option_quality["entry_allowed"] = False
        quality_reasons.append(
            f"spread {option_quality['spread_percent']:.2f}% exceeds "
            f"{option_quality['max_spread_percent']:.2f}%"
        )
    if option_quality.get("greeks_filter_enabled") and option_quality.get("delta") is not None:
        if abs(option_quality["delta"]) < min_delta or abs(option_quality["delta"]) > max_delta:
            option_quality["entry_allowed"] = False
            quality_reasons.append(
                f"delta {option_quality['delta']:.3f} outside {min_delta:.2f}-{max_delta:.2f}"
            )
    if option_quality.get("depth_filter_enabled") and option_quality.get("depth_bias") not in {"NEUTRAL", direction}:
        option_quality["entry_allowed"] = False
        quality_reasons.append(
            f"depth {option_quality.get('depth_bias')} conflicts with {direction}"
        )
    option_quality["rejection_reasons"] = quality_reasons

    # The stream is dynamic because the ATM strike changes. The persistent
    # service will subscribe to the next set on its next refresh/restart.
    write_stream_instruments([
        "NSE_INDEX|Nifty 50",
        "NSE_INDEX|Nifty Bank",
        "NSE_INDEX|India VIX",
        instrument.get("instrument_key"),
    ])
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
    technicals["option_market_quality"] = option_quality

    for timeframe in ("two_hour", "fifteen_min", "five_min"):
        technicals[timeframe] = convert_index_levels_to_option_premium(
            technicals.get(timeframe, {}),
            option_side=option_type,
            option_entry_price=entry_price,
            delta=exit_settings["delta"],
            transaction_type=transaction_type,
        )

    option_summary = {
        "symbol": symbol,
        "bias": direction,
        "confidence": rec["confidence"],
        "chain_bias": rec.get("chain_bias", direction),
        "chain_confidence": rec.get("chain_confidence", rec["confidence"]),
        "neutral_chain_override": bool(rec.get("neutral_chain_override")),
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
        "option_market_quality": option_quality,
    }
    weighted = weighted_alignment_score(option_summary, technicals, option_trend)
    option_summary["weighted_alignment"] = weighted

    if not option_quality.get("entry_allowed"):
        return {
            "allowed": False,
            "reason": "option market quality rejected: " + "; ".join(quality_reasons),
            "transaction_type": transaction_type,
            "instrument": instrument,
            "technicals": technicals,
            "option_summary": option_summary,
            "weighted": weighted,
        }, None

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
    if symbol == "BANKNIFTY" and option_summary.get("neutral_chain_override"):
        minimum = max(
            minimum,
            configured_non_negative_float("BANKNIFTY_NEUTRAL_CHAIN_MIN_SCORE", 75.0),
        )
    if weighted.get("grade") == "SKIP" or (
        score_value < minimum
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
    levels = option_levels_from_index_points(
        symbol,
        entry_price,
        target_points=exit_settings["target_points"],
        stop_points=exit_settings["stop_points"],
        delta=exit_settings["delta"],
    )
    target = levels["target_price"]
    stop = levels["stop_loss_price"]
    option_summary.update(
        {
            "target_price": target,
            "stop_loss_price": stop,
            "cautious_trade": cautious,
            "target_points": levels["target_points"],
            "stop_points": levels["stop_points"],
            "option_delta_used": levels["delta"],
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

    return {
        "allowed": True,
        "reason": "qualified",
        "transaction_type": transaction_type,
        "instrument": instrument,
        "entry_price": entry_price,
        "target_price": float(target),
        "stop_loss_price": float(stop),
        "target_percent": None,
        "stop_percent": None,
        "target_points": levels["target_points"],
        "stop_points": levels["stop_points"],
        "option_delta_used": levels["delta"],
        "technicals": technicals,
        "option_summary": option_summary,
        "weighted": weighted,
    }, None


def select_trade_candidate(candidates, allow_sell=True):
    qualified = [
        candidate
        for candidate in candidates
        if candidate
        and candidate.get("allowed")
        and candidate.get("transaction_type") == "BUY"
    ]
    return max(
        qualified,
        key=lambda item: float(item.get("weighted", {}).get("score") or 0),
        default=None,
    )


def evaluate_symbol_buy_or_sell(symbol, allow_option_sell=False):
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
    directional_high = (
        direction in {"BULLISH", "BEARISH"}
        and confidence == "HIGH"
        and abs(score) >= 4
    )
    neutral_banknifty_candidate = symbol == "BANKNIFTY" and direction == "NEUTRAL"
    if not directional_high and not neutral_banknifty_candidate:
        collect_institutional_footprint(symbol, rec)
        log(f"{symbol} no trade: signal is not directional HIGH confidence.")
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

    if symbol == "BANKNIFTY":
        try:
            breadth = get_banknifty_breadth(INSTRUMENT_CACHE, upstox_request)
        except Exception as error:
            breadth = {
                "bias": "NEUTRAL",
                "confidence": "LOW",
                "score": 0,
                "reasons": [f"BANKNIFTY breadth unavailable: {error}"],
            }
        base_technicals["banknifty_breadth"] = breadth
        log(
            f"BANKNIFTY breadth: bias={breadth.get('bias')} "
            f"confidence={breadth.get('confidence')} score={breadth.get('score')} "
            f"reasons={breadth.get('reasons')}"
        )

    if neutral_banknifty_candidate:
        inferred_direction, blockers = banknifty_neutral_chain_direction(base_technicals)
        if not inferred_direction:
            collect_institutional_footprint(symbol, rec)
            log(
                "BANKNIFTY no trade: neutral option chain and strong-technical "
                "override failed: " + "; ".join(blockers)
            )
            return False
        original_direction = direction
        original_confidence = confidence
        rec = deepcopy(rec)
        rec.update(
            {
                "chain_bias": original_direction,
                "chain_confidence": original_confidence,
                "neutral_chain_override": True,
                "direction": inferred_direction,
            }
        )
        direction = inferred_direction
        log(
            f"BANKNIFTY neutral-chain override candidate: direction={direction}; "
            "5M/15M, 2H and major-bank breadth passed preconditions"
        )

    observe_signal_reset(symbol, direction)
    blocked_reason = reentry_block_reason(symbol, direction)
    if blocked_reason:
        log(f"{symbol} no trade: {blocked_reason}")
        return False

    institutional = collect_institutional_footprint(symbol, rec)
    option_trend = get_option_chain_trend(symbol, direction, expiry=atm.get("expiry"))
    candidates = []
    try:
        candidate, _ = build_trade_candidate(
            symbol,
            rec,
            base_technicals,
            institutional,
            option_trend,
            "BUY",
        )
        if candidate:
            candidates.append(candidate)
            log(
                f"{symbol} BUY candidate: allowed={candidate.get('allowed')} "
                f"score={candidate.get('weighted', {}).get('score')} reason={candidate.get('reason')} "
                f"contract={candidate.get('instrument', {}).get('trading_symbol')}"
            )
    except Exception as error:
        log(f"{symbol} BUY candidate unavailable: {error}")

    preferred = select_trade_candidate(candidates, allow_sell=allow_option_sell)
    if not preferred:
        eligible = [
            item for item in candidates
            if allow_option_sell or item.get("transaction_type") != "SELL"
        ]
        best = max(eligible, key=lambda item: float(item.get("weighted", {}).get("score") or 0), default=None)
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
        log(f"{symbol} no trade: BUY structure did not pass deterministic gates.")
        return False

    qualified = [
        item
        for item in candidates
        if item
        and item.get("allowed")
        and item.get("transaction_type") == "BUY"
    ]
    ordered = [preferred] + [item for item in qualified if item is not preferred]

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
                f"{symbol} {transaction_type} structure skipped: configured option "
                "capital is insufficient for one whole lot."
            )
            continue

        option_summary = chosen["option_summary"]
        technicals = chosen["technicals"]
        decision = {
            "execute_trade": True,
            "decision": direction,
            "confidence": (
                "HIGH"
                if chosen.get("weighted", {}).get("grade") == "TRADE"
                else "MEDIUM"
            ),
            "target_price": chosen["target_price"],
            "stop_loss_price": chosen["stop_loss_price"],
            "reason": (
                "Approved by deterministic option-chain, technical, market-quality, "
                "and entry-feasibility rules."
            ),
        }
        record_analysis(symbol, option_summary, technicals, decision)

        chosen.update(
            {
                "symbol": symbol,
                "direction": direction,
                "confidence": confidence,
                "signal_score": score,
                "decision": decision,
            }
        )
        return chosen

    log(f"{symbol} no trade: BUY structure did not pass deterministic rules.")
    return False


def execute_selected_candidate(chosen):
    symbol = chosen["symbol"]
    direction = chosen["direction"]
    confidence = chosen["confidence"]
    score = chosen["signal_score"]
    transaction_type = chosen["transaction_type"]
    if transaction_type != "BUY":
        log(f"{symbol} blocked unsupported transaction type: {transaction_type}")
        return False
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
        log(f"{symbol} no trade: configured option capital is insufficient for one whole lot.")
        return False

    live = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
    log(
        f"{symbol} selected {transaction_type}: {instrument['trading_symbol']} qty={quantity} "
        f"entry={entry_price} target={target} stop={stop} live={live}"
    )
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
            "target_points": chosen["target_points"],
            "stop_points": chosen["stop_points"],
            "option_delta_used": chosen["option_delta_used"],
            "option_type": chosen.get("option_summary", {}).get("option_type"),
            "technical_context": chosen.get("technicals", {}),
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
        target_points=chosen["target_points"],
        stop_points=chosen["stop_points"],
        option_delta_used=chosen["option_delta_used"],
    )

    post_fill = revalidate_option_after_fill(
        symbol,
        direction,
        chosen.get("option_summary", {}).get("option_type"),
        fill,
        chosen["target_points"],
        chosen["stop_points"],
        chosen["option_delta_used"],
        chosen.get("technicals", {}),
    )
    state = read_state(symbol)
    state["post_fill_feasibility"] = post_fill["feasibility"]
    if not post_fill["allowed"]:
        reason = "; ".join(post_fill["feasibility"].get("reasons", []))
        state["status"] = "EXIT_PENDING"
        state["exit_reason"] = "POST_FILL_GUARDRAIL"
        state["post_fill_guardrail_reason"] = reason
        state["target_price"] = post_fill["target_price"]
        state["stop_loss_price"] = post_fill["stop_loss_price"]
        write_state(symbol, state)
        log(
            f"{symbol} post-fill guardrail rejected position: fill={fill} "
            f"reason={reason}; flattening immediately"
        )
        exit_result, exit_payload = place_market_order(instrument, "SELL", quantity)
        exit_order_id = exit_result.get("data", {}).get("order_id")
        if not exit_order_id:
            raise RuntimeError(f"{symbol} post-fill guardrail exit returned no order_id")
        exit_details = wait_for_order_complete(exit_order_id)
        if order_is_complete(exit_details):
            complete_exit(
                symbol,
                state,
                exit_details,
                fill,
                "POST_FILL_GUARDRAIL",
                exit_result,
                exit_payload,
            )
        else:
            state["exit_order_id"] = exit_order_id
            state["exit_fallback_price"] = fill
            write_state(symbol, state)
            log(f"{symbol} post-fill guardrail exit pending: order_id={exit_order_id}")
        return True

    state.update(
        {
            "target_price": post_fill["target_price"],
            "stop_loss_price": post_fill["stop_loss_price"],
            "original_stop_loss_price": post_fill["stop_loss_price"],
            "technical_context": post_fill["technicals"],
        }
    )
    write_state(symbol, state)
    log(
        f"{symbol} post-fill levels validated: fill={fill} "
        f"target={post_fill['target_price']} stop_loss={post_fill['stop_loss_price']} "
        f"reward_risk={post_fill['feasibility'].get('technical_reward_risk')}"
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


def execute_stock_future_candidate(chosen):
    log("STOCK_FUTURE entry blocked: stock-futures execution is disabled.")
    return False


def execute_stock_option_candidate(chosen):
    """Buy exactly one lot of the selected stock option and track it independently."""
    symbol = STOCK_OPTION_STATE
    instrument = chosen["instrument"]
    quantity = int(instrument.get("lot_size") or 0)
    expected_entry = float(chosen["entry_price"])
    if quantity <= 0:
        raise RuntimeError("Selected stock option has no valid lot size")
    levels = stock_option_rupee_levels(expected_entry, quantity)
    live = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
    log(
        f"STOCK_OPTION selected: {chosen['direction']} "
        f"{instrument.get('trading_symbol')} qty={quantity} entry={expected_entry} "
        f"target={levels['target_price']} stop={levels['stop_loss_price']} "
        f"score={chosen.get('weighted', {}).get('score')} live={live}"
    )
    if not live:
        log("STOCK_OPTION dry run only: ENABLE_LIVE_TRADING is not true; no order placed.")
        return False

    result, payload = place_market_order(instrument, "BUY", quantity)
    order_id = result.get("data", {}).get("order_id")
    if not order_id:
        raise RuntimeError(f"STOCK_OPTION BUY returned no order_id: {result}")
    log(f"STOCK_OPTION MARKET BUY placed: order_id={order_id} payload={payload}")
    details = wait_for_order_complete(order_id)
    if not order_is_complete(details):
        write_state(
            symbol,
            {
                "date": now_ist().strftime("%Y-%m-%d"),
                "symbol": symbol,
                "underlying_symbol": chosen["underlying_symbol"],
                "instrument_class": "STOCK_OPTION",
                "entry_order_id": order_id,
                "buy_order_id": order_id,
                "entry_transaction_type": "BUY",
                "exit_transaction_type": "SELL",
                "instrument_key": instrument["instrument_key"],
                "trading_symbol": instrument.get("trading_symbol"),
                "quantity": quantity,
                "lot_size": quantity,
                "lot_multiplier": 1,
                "entry_price": expected_entry,
                "target_price": levels["target_price"],
                "stop_loss_price": levels["stop_loss_price"],
                "fixed_target_rupees": levels["target_rupees"],
                "fixed_stop_rupees": levels["stop_rupees"],
                "option_type": chosen.get("option_summary", {}).get("option_type"),
                "technical_context": chosen.get("technicals", {}),
                "direction": chosen["direction"],
                "confidence": chosen["confidence"],
                "score": chosen["signal_score"],
                "status": "BUY_PLACED_NOT_COMPLETE",
                "created_at": now_ist().isoformat(),
            },
        )
        log(
            f"STOCK_OPTION BUY is pending; state retained: order_id={order_id} "
            f"status={order_status(details)}"
        )
        return True

    position = find_matching_position_for_side(instrument["instrument_key"], "BUY")
    fill = position_avg_price(position, "BUY") if position else None
    fill = fill or to_float(details.get("average_price")) or expected_entry
    levels = stock_option_rupee_levels(fill, quantity)
    save_open_position_state(
        symbol,
        order_id,
        instrument,
        chosen["direction"],
        chosen["confidence"],
        chosen["signal_score"],
        fill,
        quantity,
        target_price=levels["target_price"],
        stop_loss_price=levels["stop_loss_price"],
        entry_transaction_type="BUY",
        instrument_class="STOCK_OPTION",
        underlying_symbol=chosen["underlying_symbol"],
    )
    state = read_state(symbol)
    state.update(
        {
            "option_type": chosen.get("option_summary", {}).get("option_type"),
            "technical_context": chosen.get("technicals", {}),
            "fixed_target_rupees": levels["target_rupees"],
            "fixed_stop_rupees": levels["stop_rupees"],
            "mover_type": chosen.get("mover_type"),
            "mover_change_percent": chosen.get("mover_change_percent"),
        }
    )
    write_state(symbol, state)
    clear_reentry_guard(symbol)
    return True


def run_stock_options_scan():
    if os.getenv("ENABLE_STOCK_OPTIONS_TRADING", "false").lower() != "true":
        return False
    if read_state(STOCK_OPTION_STATE).get("instrument_key"):
        log("STOCK_OPTION already has an active bot position; scanner skipped.")
        return False

    ensure_instruments_file()
    try:
        result = scan_stock_option_candidates(
            INSTRUMENT_CACHE,
            upstox_request,
            read_market_cache,
            log,
        )
    except Exception as error:
        log(f"STOCK_OPTION scanner ERROR: {error}")
        return False
    qualified = result.get("qualified", [])
    if not qualified:
        log("STOCK_OPTION no trade: all shortlisted NIFTY-50 stocks failed deterministic gates.")
        return False

    chosen = qualified[0]
    decision = {
        "execute_trade": True,
        "decision": chosen["direction"],
        "confidence": "HIGH",
        "target_price": None,
        "stop_loss_price": None,
        "reason": (
            f"{chosen['mover_type']} passed tradeability, 5M/15M structure, "
            "VWAP/momentum, futures OI, market context, option evidence, "
            "and directional-score gates."
        ),
    }
    record_analysis(
        chosen["underlying_symbol"],
        chosen["option_summary"],
        chosen["technicals"],
        decision,
    )
    try:
        return execute_stock_option_candidate(chosen)
    except Exception as error:
        log(f"STOCK_OPTION order execution ERROR: {error}")
        return False


def _legacy_execute_stock_future_candidate(chosen):
    symbol = STOCK_FUTURE_STATE
    instrument = chosen["instrument"]
    transaction_type = chosen["transaction_type"]
    quantity = int(chosen["quantity"])
    expected_entry = float(chosen["entry_price"])
    target = float(chosen["target_price"])
    stop = float(chosen["stop_loss_price"])
    live = (
        os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
        and os.getenv("ENABLE_STOCK_FUTURES_LIVE_TRADING", "false").lower() == "true"
    )
    risk_reason, risk_mode = risk_limit_mode()

    log(
        f"STOCK_FUTURE selected: {transaction_type} {instrument['trading_symbol']} "
        f"qty={quantity} entry={expected_entry} target={target} stop={stop} "
        f"score={chosen['signal_score']} strategy={chosen.get('strategy', 'TREND_FOLLOWING')} live={live}"
    )
    if risk_mode == "paper":
        log(
            f"STOCK_FUTURE PAPER ONLY after daily risk limit: reason={risk_reason} "
            f"would_{transaction_type.lower()}={instrument['trading_symbol']} qty={quantity} "
            f"entry={expected_entry} target={target} stop_loss={stop}"
        )
        return True
    if risk_mode == "stop":
        log(f"STOCK_FUTURE no trade: daily risk limit reached ({risk_reason})")
        return False
    if not live:
        log(
            f"STOCK_FUTURE DRY RUN ONLY: would {transaction_type} one lot of "
            f"{instrument['trading_symbol']}"
        )
        return True

    result, payload = place_market_order(instrument, transaction_type, quantity)
    order_id = result.get("data", {}).get("order_id")
    if not order_id:
        raise RuntimeError(f"STOCK_FUTURE {transaction_type} returned no order_id: {result}")
    increment_trade_count(STOCK_FUTURE_STATE)
    log(f"STOCK_FUTURE MARKET {transaction_type} placed: order_id={order_id} payload={payload}")
    details = wait_for_order_complete(order_id)
    if not order_is_complete(details):
        write_state(
            symbol,
            {
                "date": now_ist().strftime("%Y-%m-%d"),
                "symbol": symbol,
                "underlying_symbol": chosen["underlying_symbol"],
                "instrument_class": "STOCK_FUTURE",
                "entry_order_id": order_id,
                "entry_transaction_type": transaction_type,
                "exit_transaction_type": "SELL" if transaction_type == "BUY" else "BUY",
                "instrument_key": instrument["instrument_key"],
                "trading_symbol": instrument["trading_symbol"],
                "quantity": quantity,
                "lot_size": int(instrument["lot_size"]),
                "lot_multiplier": 1,
                "target_price": target,
                "stop_loss_price": stop,
                "direction": chosen["direction"],
                "confidence": chosen["confidence"],
                "score": chosen["signal_score"],
                "status": f"{transaction_type}_PLACED_NOT_COMPLETE",
                "created_at": now_ist().isoformat(),
            },
        )
        return True

    position = find_matching_position_for_side(instrument["instrument_key"], transaction_type)
    fill = position_avg_price(position, transaction_type) if position else None
    fill = fill or to_float(details.get("average_price")) or expected_entry
    shift = fill - expected_entry
    target = round(target + shift, 2)
    stop = round(stop + shift, 2)
    save_open_position_state(
        symbol,
        order_id,
        instrument,
        chosen["direction"],
        chosen["confidence"],
        chosen["signal_score"],
        fill,
        quantity,
        target_price=target,
        stop_loss_price=stop,
        entry_transaction_type=transaction_type,
        instrument_class="STOCK_FUTURE",
        underlying_symbol=chosen["underlying_symbol"],
    )
    try:
        ensure_protective_stop(symbol, read_state(symbol))
    except Exception as error:
        log(f"STOCK_FUTURE CRITICAL: protective stop failed; flattening immediately: {error}")
        exit_transaction = "SELL" if transaction_type == "BUY" else "BUY"
        emergency, emergency_payload = place_market_order(instrument, exit_transaction, quantity)
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


def run_stock_futures_fallback():
    log("Stock-futures scanner disabled: no stock-futures orders will be placed.")
    return False


def _legacy_run_stock_futures_fallback():
    if os.getenv("ENABLE_STOCK_FUTURES_SCANNER", "false").lower() != "true":
        write_scanner_status(
            STOCK_SCANNER_STATUS_FILE,
            enabled=False,
            status="DISABLED",
            message="Stock futures fallback is disabled in .env",
        )
        return False

    max_daily_trades = max(to_int(os.getenv("STOCK_FUTURES_MAX_TRADES_PER_DAY"), 1), 0)
    if max_daily_trades and trade_count_for(STOCK_FUTURE_STATE) >= max_daily_trades:
        message = (
            f"Stock futures daily cap reached: "
            f"{trade_count_for(STOCK_FUTURE_STATE)}/{max_daily_trades}"
        )
        write_scanner_status(
            STOCK_SCANNER_STATUS_FILE,
            enabled=True,
            status="DAILY_CAP_REACHED",
            message=message,
        )
        log(message)
        return False

    ensure_instruments_file()
    try:
        result = scan_stock_futures(INSTRUMENT_CACHE, upstox_request, log)
    except Exception as error:
        write_scanner_status(
            STOCK_SCANNER_STATUS_FILE,
            enabled=True,
            status="ERROR",
            message=str(error),
        )
        log(f"Stock futures scanner ERROR: {error}")
        return False

    qualified = result.get("qualified", [])
    attempts = []
    live = (
        os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
        and os.getenv("ENABLE_STOCK_FUTURES_LIVE_TRADING", "false").lower() == "true"
    )
    check_margin = live or os.getenv("CHECK_STOCK_MARGIN_IN_DRY_RUN", "false").lower() == "true"
    risk_budget = configured_non_negative_float(
        "STOCK_FUTURES_MAX_RISK_PER_TRADE",
        max_risk_per_trade(STOCK_FUTURE_STATE),
    )

    for candidate in qualified:
        risk = abs(float(candidate["entry_price"]) - float(candidate["stop_loss_price"])) * int(candidate["quantity"])
        attempt = {
            "underlying": candidate["underlying_symbol"],
            "trading_symbol": candidate["instrument"]["trading_symbol"],
            "direction": candidate["direction"],
            "score": candidate["signal_score"],
            "strategy": candidate.get("strategy", "TREND_FOLLOWING"),
            "estimated_risk": round(risk, 2),
        }
        if risk_budget > 0 and risk > risk_budget:
            attempt.update(status="RISK_REJECTED", reason=f"risk {risk:.2f} exceeds {risk_budget:.2f}")
            attempts.append(attempt)
            log(
                f"STOCK_FUTURE {candidate['underlying_symbol']} skipped: one-lot risk "
                f"{risk:.2f} exceeds budget {risk_budget:.2f}; trying next stock."
            )
            continue

        if check_margin:
            try:
                margin = validate_stock_future_margin(
                    candidate["instrument"],
                    candidate["transaction_type"],
                    candidate["quantity"],
                    candidate["entry_price"],
                )
            except Exception as error:
                attempt.update(status="MARGIN_ERROR", reason=str(error))
                attempts.append(attempt)
                log(
                    f"STOCK_FUTURE {candidate['underlying_symbol']} margin check failed: "
                    f"{error}; trying next stock."
                )
                continue
            attempt["margin"] = margin
            if not margin.get("allowed"):
                attempt.update(status="INSUFFICIENT_FUNDS", reason="buffered margin is insufficient")
                attempts.append(attempt)
                log(
                    f"STOCK_FUTURE insufficient funds for {candidate['instrument']['trading_symbol']}: "
                    f"required={margin['required_margin']} available={margin['available_margin']} "
                    f"usable={margin['usable_margin_after_buffer']}; trying next stock."
                )
                continue

        attempt["status"] = "SELECTED"
        attempts.append(attempt)
        write_scanner_status(
            STOCK_SCANNER_STATUS_FILE,
            enabled=True,
            status="SELECTED",
            universe_count=result.get("universe_count"),
            shortlist_count=result.get("shortlist_count"),
            qualified_count=len(qualified),
            selected=attempt,
            attempts=attempts,
            rejected=result.get("rejected", [])[:20],
        )
        return execute_stock_future_candidate(candidate)

    status = "INSUFFICIENT_FUNDS" if any(item.get("status") == "INSUFFICIENT_FUNDS" for item in attempts) else "NO_TRADE"
    write_scanner_status(
        STOCK_SCANNER_STATUS_FILE,
        enabled=True,
        status=status,
        universe_count=result.get("universe_count"),
        shortlist_count=result.get("shortlist_count"),
        qualified_count=len(qualified),
        attempts=attempts,
        rejected=result.get("rejected", [])[:20],
        message="No affordable qualified stock-futures candidate was found",
    )
    log(f"Stock futures fallback: {status}; no order placed.")
    return False


def run_signal_check():
    if not market_window_ok():
        log("Outside trading window. No action.")
        return

    for symbol in BOT_STATE_SLOTS:
        state = read_state(symbol)
        if state:
            try:
                handle_existing_state(symbol, state)
            except Exception as error:
                log(f"{symbol} existing-position check ERROR: {error}")

    active_index = {
        symbol
        for symbol in SYMBOLS
        if read_state(symbol).get("instrument_key")
    }

    try:
        tracked_instrument_keys = {
            read_state(symbol).get("instrument_key")
            for symbol in BOT_STATE_SLOTS
            if read_state(symbol).get("instrument_key")
        }
        untracked_derivative_positions = [
            position
            for position in get_open_positions()
            if position_quantity(position) != 0
            and (position.get("instrument_token") or position.get("instrument_key")) not in tracked_instrument_keys
            and (
                str(position.get("exchange") or position.get("segment") or "").upper() in {"NSE_FO", "NFO"}
                or str(position.get("instrument_token") or position.get("instrument_key") or "").startswith("NSE_FO|")
            )
        ]
        if untracked_derivative_positions:
            allow_manual_overlap = (
                os.getenv(
                    "ALLOW_BOT_WITH_UNTRACKED_DERIVATIVE_POSITIONS", "false"
                ).lower()
                == "true"
            )
            if not allow_manual_overlap:
                log(
                    "An untracked NSE derivatives position "
                    "exists; no bot entry. Set "
                    "ALLOW_BOT_WITH_UNTRACKED_DERIVATIVE_POSITIONS=true only when "
                    "manual positions are intentionally allowed to coexist."
                )
                return
            log(
                "Manual derivative position detected; override enabled. "
                "Bot may enter index-option positions and will manage only its own state."
            )
    except Exception as error:
        log(f"Global broker-position precheck failed; no new entry for safety: {error}")
        return

    qualified = []
    for symbol in SYMBOLS:
        if symbol in active_index:
            log(f"{symbol} already has an active bot position; skipping only {symbol} entry.")
            continue
        try:
            daily_block = daily_index_entry_block_reason(symbol)
        except Exception as error:
            log(
                f"{symbol} daily first-outcome guard could not read trade history; "
                f"no new entry for safety: {error}"
            )
            continue
        if daily_block:
            log(f"{symbol} no trade: {daily_block}")
            continue
        try:
            candidate = evaluate_symbol_buy_or_sell(symbol, allow_option_sell=False)
            if candidate:
                qualified.append(candidate)
        except Exception as e:
            log(f"{symbol} ERROR: {e}")

    if not qualified:
        log("No new qualified NIFTY or BANKNIFTY BUY structure.")
    else:
        ordered = sorted(
            qualified,
            key=lambda item: (
                float(item.get("weighted", {}).get("score") or 0),
                1 if item.get("transaction_type") == "BUY" else 0,
            ),
            reverse=True,
        )
        choices = [
            (item["symbol"], item["transaction_type"], item.get("weighted", {}).get("score"))
            for item in qualified
        ]
        log("Independent index selections qualified: " + str(choices))
        for chosen in ordered:
            try:
                execute_selected_candidate(chosen)
            except Exception as error:
                log(f"{chosen['symbol']} order execution ERROR: {error}")

    # Stock options use an independent state slot, so an index position does
    # not block this lane and a stock-option position does not block an index.
    run_stock_options_scan()


def main():
    load_env()

    if "--squareoff" in sys.argv:
        run_squareoff()
    elif "--monitor" in sys.argv:
        run_position_monitor_loop()
    else:
        run_signal_check()


if __name__ == "__main__":
    main()
