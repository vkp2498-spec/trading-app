import os
import sys
import json
import gzip
import hashlib
import time as time_module
import socket
import urllib.request
from pathlib import Path
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import csv
import random
import pandas as pd
from copy import deepcopy
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None

from analysis_journal import record_analysis
from adaptive_score_calibration import read_effective_score_rule
from scan_journal import record_scan_decision
from banknifty_breadth import get_banknifty_breadth
from nifty_breadth import get_nifty_breadth
from institutional_flow import (
    get_institutional_footprint,
    neutral_institutional_footprint,
)
from market_technicals import (
    get_technical_analysis,
    convert_index_levels_to_option_premium,
    get_option_volume_vwap_analysis,
    candle_confirmation,
    fetch_v3_historical_hours,
    fetch_v3_historical_minutes,
    fetch_v3_intraday_minutes,
)
from ganesh_gap_reversal import (
    BULLISH as GANESH_BULLISH,
    BEARISH as GANESH_BEARISH,
    NO_GAP as GANESH_NO_GAP,
    active_two_hour_start,
    advance_entry_confirmation,
    advance_exit_confirmation,
    atm_strike,
    bollinger_bands,
    candle_colour,
    classic_pivots,
    continuation_for_gap,
    nearest_target,
    nearest_continuation_target,
    opening_gap,
    score_gap_continuation,
    target_reached,
    transition_for_gap,
)
from live_trade_filters import (
    bollinger_exhaustion_reversal,
    classify_market_regime,
    entry_structure_for_direction,
    live_entry_gate,
    structural_invalidation,
    underlying_exit_reason,
)
from stock_futures_scanner import scan_stock_futures, write_scanner_status

from option_chain_trend import get_option_chain_trend, record_option_chain_snapshot
from signal_score import (
    banknifty_neutral_chain_direction,
    nifty_neutral_chain_direction,
    weighted_alignment_score,
)
from portfolio_risk import (
    aggregate_risk_decision,
    correlation_decision,
    proposed_position_risk,
    state_is_active,
    total_open_risk,
)

import requests
import urllib3.util.connection as urllib3_cn

from strategy_core import (
    fetch_upstox_option_chain,
    get_index_recommendation,
    now_ist,
    option_chain_signal,
)
from strategy_core import option_contract_quality
from trade_journal import record_closed_trade
from trading_config import active_value
from unified_entry_score import unified_entry_score
from safe_storage import atomic_write_json, file_lock, locked_append_csv
from upstox_streams import (
    read_market_cache,
    read_portfolio_cache,
    read_recent_ticks,
    write_stream_instruments,
)

from apns_push import send_trade_closed_notification, send_trade_entered_notification


def send_apple_closed_trade_alert(journal_row):
    """Never allow a notification failure to interrupt trading cleanup."""
    try:
        result = send_trade_closed_notification(journal_row)
        log(f"Apple trade-close notification result: {result}")
    except Exception as error:
        log(f"Apple trade-close notification failed: {error}")


def send_apple_trade_entered_alert(position_state):
    """Send at most one entry alert for each broker entry order."""
    order_id = str(
        position_state.get("entry_order_id")
        or position_state.get("buy_order_id")
        or ""
    ).strip()
    if not order_id:
        order_id = "|".join(
            str(position_state.get(key) or "")
            for key in ("strategy", "instrument_key", "created_at")
        )
    receipt_key = f"{position_state.get('date') or now_ist().date()}:{order_id}"

    with file_lock(ENTRY_NOTIFICATION_RECEIPTS_LOCK_FILE):
        receipts = read_json(ENTRY_NOTIFICATION_RECEIPTS_FILE, {})
        if receipt_key in receipts:
            return {"sent": 0, "failed": 0, "duplicate": True}
        try:
            result = send_trade_entered_notification(position_state)
            receipts[receipt_key] = now_ist().isoformat()
            receipts = dict(list(receipts.items())[-500:])
            atomic_write_json(
                ENTRY_NOTIFICATION_RECEIPTS_FILE,
                receipts,
                sort_keys=True,
            )
            log(f"Apple trade-entry notification result: {result}")
            return result
        except Exception as error:
            log(f"Apple trade-entry notification failed: {error}")
            return {"sent": 0, "failed": 1}


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
DAY_RISK_STATE_FILE = BASE_DIR / "data" / "day_risk_state.json"
MONITOR_HEALTH_FILE = BASE_DIR / "data" / "monitor_health.json"
BOT_RUNTIME_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
ENTRY_NOTIFICATION_RECEIPTS_FILE = BASE_DIR / "data" / "entry_notification_receipts.json"
ENTRY_NOTIFICATION_RECEIPTS_LOCK_FILE = BASE_DIR / ".entry_notification_receipts.lock"
PORTFOLIO_ENTRY_LOCK_FILE = BASE_DIR / ".portfolio_entry.lock"
WATCH_STATE_DIR = BASE_DIR / "data" / "watch_states"

SYMBOLS = ["NIFTY", "BANKNIFTY"]
GANESH_GAP_STATE_BY_SYMBOL = {
    symbol: f"GANESH_GAP_{symbol}" for symbol in SYMBOLS
}
# Backward-compatible alias for the original NIFTY-only state slot.
GANESH_GAP_STATE = GANESH_GAP_STATE_BY_SYMBOL["NIFTY"]
GANESH_GAP_STATE_SLOTS = list(GANESH_GAP_STATE_BY_SYMBOL.values())
STOCK_FUTURE_STATE = "STOCK_FUTURE"
VAMSI_STATE_SLOTS = SYMBOLS + [STOCK_FUTURE_STATE]
BOT_STATE_SLOTS = VAMSI_STATE_SLOTS + GANESH_GAP_STATE_SLOTS
GANESH_GAP_SCAN_FILE = BASE_DIR / "data" / "ganesh_gap_scans.csv"
GANESH_GAP_BANKNIFTY_SCAN_FILE = BASE_DIR / "data" / "ganesh_gap_banknifty_scans.csv"

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
    "NIFTY": {"target": 20.0, "stop": 20.0},
    "BANKNIFTY": {"target": 40.0, "stop": 40.0},
}
DEFAULT_OPTION_DELTA_APPROXIMATION = 0.50
DEFAULT_MIN_TECHNICAL_REWARD_RISK = 0.8
DEFAULT_MAX_ENTRY_EXTENSION_PERCENT = 1.5
DEFAULT_RISK_SLOTS_PER_DAY = 3
DEFAULT_MIN_REENTRY_MINUTES = 0
DEFAULT_OPTION_CAPITAL_PER_ENTRY = "MAX"

SYMBOL_CONFIG = {
    "NIFTY": {
        "underlying_candidates": ["NIFTY"],
    },
    "BANKNIFTY": {
        "underlying_candidates": ["BANKNIFTY", "NIFTY BANK"],
    },
}
UNDERLYING_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}

UPSTOX_PLACE_ORDER_URL = "https://api-hft.upstox.com/v2/order/place"
UPSTOX_CANCEL_ORDER_URL = "https://api-hft.upstox.com/v2/order/cancel"
UPSTOX_MODIFY_ORDER_URL = "https://api-hft.upstox.com/v2/order/modify"
UPSTOX_ORDER_DETAILS_URL = "https://api.upstox.com/v2/order/details"
UPSTOX_ORDER_BOOK_URL = "https://api.upstox.com/v2/order/retrieve-all"
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


def market_protection_percent():
    value = to_int(os.getenv("MARKET_PROTECTION_PERCENT"), 3)
    if not 1 <= value <= 25:
        raise RuntimeError("MARKET_PROTECTION_PERCENT must be between 1 and 25")
    return value


def state_file(symbol):
    return BASE_DIR / f"trade_state_{symbol}.json"


def reentry_guard_file(symbol):
    return BASE_DIR / f"reentry_guard_{symbol}.json"


def watch_state_file(symbol):
    return WATCH_STATE_DIR / f"{str(symbol).upper()}.json"


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


def concise_trade_logs():
    return os.getenv("CONCISE_TRADE_LOGS", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def verbose_log(msg):
    if not concise_trade_logs():
        log(msg)


def format_score(value):
    try:
        numeric = float(value)
        return str(int(numeric)) if numeric.is_integer() else f"{numeric:.1f}"
    except Exception:
        return str(value)


def log_scan_decision(symbol, score, action, score_version=None):
    version_text = f" version={score_version}" if score_version else ""
    log(f"{symbol} score {format_score(score)} {action}{version_text}")
    try:
        record_scan_decision(
            symbol,
            score,
            action,
            score_version=score_version,
        )
    except Exception as error:
        verbose_log(f"{symbol} scan journal write failed: {error}")


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


def configured_bool(env_key, default=False):
    raw = os.getenv(env_key)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def trading_engine():
    engine = str(os.getenv("TRADING_ENGINE", "VAMSI")).strip().upper()
    if engine not in {"VAMSI", "GANESH"}:
        raise RuntimeError("TRADING_ENGINE must be VAMSI or GANESH")
    return engine


def score_cutoff_mode_enabled():
    return configured_bool("SCORE_CUTOFF_MODE_ENABLED", True)


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
        "source": "STATIC_ENV",
    }
    if settings["delta"] > 1:
        raise RuntimeError("OPTION_DELTA_APPROXIMATION must be greater than 0 and at most 1")
    if configured_bool("VAMSI_ADAPTIVE_SCORE_ENABLED", True):
        adaptive = read_effective_score_rule(symbol, now_ist().date())
        exit_levels = (adaptive or {}).get("exit_levels") or {}
        if exit_levels.get("source") == "ADAPTIVE_HISTORY_AVERAGE":
            target_points = to_float(exit_levels.get("target_points"))
            stop_points = to_float(exit_levels.get("stop_points"))
            if target_points > 0 and stop_points > 0:
                settings["target_points"] = target_points
                settings["stop_points"] = stop_points
                settings["source"] = "ADAPTIVE_HISTORY_AVERAGE"
    return settings


def score_based_exit_settings(symbol, weighted_score):
    """Use wider profit objectives only for exceptional scored setups."""
    settings = dict(index_point_exit_settings(symbol))
    threshold = configured_non_negative_float("EXTREME_SETUP_MIN_SCORE", 90.0)
    score = to_float(weighted_score)
    settings["profile"] = (
        "ADAPTIVE_HISTORY"
        if settings.get("source") == "ADAPTIVE_HISTORY_AVERAGE"
        else "STANDARD"
    )
    if settings["profile"] == "ADAPTIVE_HISTORY":
        return settings
    if score < threshold:
        return settings

    settings["profile"] = "EXTREME"
    default_target = 30.0 if symbol == "NIFTY" else 60.0
    settings["target_points"] = configured_positive_float(
        f"{symbol}_EXTREME_TARGET_POINTS", default_target
    )
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
    symbol=None,
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

    # Directional scoring deliberately uses the front-expiry ATM flow, while
    # execution extension must compare like-for-like against the next-expiry
    # contract that will actually be bought.
    option_flow = (
        technicals.get("execution_atm_option_flow")
        or technicals.get("atm_option_flow")
        or {}
    )
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

    # The 5M chart controls entry timing and confirmation elsewhere in the
    # live-structure gate. Reward headroom must come from the more stable 15M
    # structure so a nearby 5M pivot/band does not veto an otherwise viable
    # intraday setup.
    analysis = technicals.get("fifteen_min", {}) or {}
    technical_target = to_float(analysis.get("option_target_price"), 0)
    target_is_valid = technical_target < entry if is_short else technical_target > entry
    target_candidates = []
    if analysis.get("bias") == direction and target_is_valid:
        target_candidates.append((technical_target, "15M"))

    result["technical_target_candidates"] = [
        {"timeframe": label, "target_price": round(value, 2)}
        for value, label in target_candidates
    ]
    if not target_candidates:
        result["reasons"].append(
            "No aligned 15M option-premium target is available"
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

    if reward <= 0:
        result["reasons"].append(
            f"Technical reward/risk {reward_risk:.2f} has no positive reachable reward; "
            f"{limiting_timeframe} target={reachable_target:.2f}"
        )
        return result

    if reward_risk < min_rr:
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
        symbol=symbol,
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


def make_post_fill_diagnostic_only(post_fill):
    """Retain post-fill analytics without using them to flatten a position."""
    feasibility = post_fill.get("feasibility", {}) or {}
    diagnostic_rejected = not bool(post_fill.get("allowed"))
    feasibility["post_fill_diagnostic_only"] = True
    feasibility["post_fill_diagnostic_rejected"] = diagnostic_rejected
    feasibility["hard_execution_gate"] = False
    post_fill["feasibility"] = feasibility
    post_fill["allowed"] = True
    return post_fill


def read_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path, data):
    atomic_write_json(path, data, sort_keys=True)


def read_state(symbol):
    return read_json(state_file(symbol), {})


def write_state(symbol, state):
    write_json(state_file(symbol), state)


def clear_state(symbol):
    write_state(symbol, {})


def ganesh_gap_max_trades_per_day():
    return max(to_int(os.getenv("GANESH_GAP_MAX_TRADES_PER_DAY"), 1), 0)


def ganesh_gap_trade_count_today():
    """Return the combined Ganesh count across both index lanes."""
    return sum(trade_count_for(slot) for slot in GANESH_GAP_STATE_SLOTS)


def ganesh_gap_live_enabled():
    return configured_bool("ENABLE_LIVE_TRADING", False) and configured_bool(
        "GANESH_GAP_LIVE_TRADING", False
    )


def is_ganesh_gap_strategy(value):
    strategy = value.get("strategy") if isinstance(value, dict) else value
    return str(strategy or "").strip().upper() in {
        "GANESH_GAP_REVERSAL",
        "GANESH_GAP_CONTINUATION",
    }


def ganesh_gap_continuation_enabled():
    return configured_bool("GANESH_CONTINUATION_ENABLED", True)


def ganesh_gap_breadth(symbol):
    try:
        ensure_instruments_file()
        if symbol == "BANKNIFTY":
            return get_banknifty_breadth(INSTRUMENT_CACHE, upstox_request)
        return get_nifty_breadth(INSTRUMENT_CACHE, upstox_request)
    except Exception as error:
        return {
            "bias": "NEUTRAL",
            "confidence": "LOW",
            "score": 0.0,
            "reasons": [f"{symbol} breadth unavailable: {error}"],
        }


def ganesh_gap_continuation_evidence(snapshot, option):
    symbol = str(snapshot.get("symbol") or "NIFTY").upper()
    analysis = option.get("near_expiry_analysis") or {}
    recommendation = option.get("recommendation") or {}
    flow = analysis.get("option_flow") or {}
    breadth = ganesh_gap_breadth(symbol)
    try:
        institutional = get_institutional_footprint(symbol, recommendation, flow)
    except Exception as error:
        institutional = neutral_institutional_footprint(
            f"institutional footprint unavailable: {error}"
        )
    chain = {
        "direction": analysis.get("chain_bias") or recommendation.get("direction"),
        "confidence": analysis.get("chain_confidence") or recommendation.get("confidence"),
        "score": analysis.get("chain_score", recommendation.get("score")),
    }
    evaluation = score_gap_continuation(
        snapshot,
        breadth,
        chain,
        flow,
        institutional,
        minimum_score=configured_positive_float("GANESH_CONTINUATION_MIN_SCORE", 75.0),
        minimum_volume_ratio=configured_positive_float(
            "GANESH_CONTINUATION_MIN_OPTION_VOLUME_RATIO", 1.20
        ),
        maximum_extension_range=configured_non_negative_float(
            "GANESH_CONTINUATION_MAX_EXTENSION_RANGE", 0.75
        ),
        retest_tolerance_range=configured_non_negative_float(
            "GANESH_CONTINUATION_RETEST_TOLERANCE_RANGE", 0.10
        ),
    )
    evaluation.update(
        {
            "breadth": breadth,
            "institutional": institutional,
            "chain": chain,
            "option_flow": flow,
        }
    )
    return evaluation


def _frame_in_ist(frame):
    if frame is None or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    index = pd.DatetimeIndex(result.index)
    if index.tz is None:
        index = index.tz_localize(IST)
    else:
        index = index.tz_convert(IST)
    result.index = index
    return result.sort_index()


def _latest_stream_ltp(instrument_key, maximum_age_seconds):
    quote = read_market_cache(instrument_key) or {}
    received_at = to_float(quote.get("received_at"), 0)
    age = time_module.time() - received_at if received_at else None
    ltp = to_float(quote.get("ltp"), 0)
    if ltp > 0 and age is not None and 0 <= age <= maximum_age_seconds:
        return ltp, age
    return 0.0, age


def _ganesh_candle_age_seconds(candle_start, current_time, interval_minutes=1):
    """Measure candle freshness from interval end; Upstox stamps interval start."""
    candle_end = pd.Timestamp(candle_start) + pd.Timedelta(minutes=interval_minutes)
    return max((pd.Timestamp(current_time) - candle_end).total_seconds(), 0.0)


def _ganesh_ohlc_summary(frame, start, end):
    rows = frame[(frame.index >= start) & (frame.index < end)]
    if rows.empty:
        return {"complete": False}
    return {
        "complete": True,
        "start": pd.Timestamp(start).isoformat(),
        "end": pd.Timestamp(end).isoformat(),
        "open": round(float(rows.iloc[0]["open"]), 2),
        "high": round(float(rows["high"].max()), 2),
        "low": round(float(rows["low"].min()), 2),
        "close": round(float(rows.iloc[-1]["close"]), 2),
    }


def ganesh_gap_market_snapshot(symbol="NIFTY", current_time=None):
    """Build a live index gap/2H/pivot/Bollinger snapshot."""
    # Preserve the old positional call shape ganesh_gap_market_snapshot(now).
    if not isinstance(symbol, str):
        current_time = symbol
        symbol = "NIFTY"
    symbol = str(symbol).strip().upper()
    if symbol not in GANESH_GAP_STATE_BY_SYMBOL:
        raise ValueError(f"Unsupported Ganesh gap symbol: {symbol}")
    current_time = current_time or now_ist()
    instrument_key = UNDERLYING_INDEX_KEYS[symbol]
    intraday = _frame_in_ist(fetch_v3_intraday_minutes(instrument_key, minutes=1))
    if intraday.empty:
        raise RuntimeError(f"{symbol} one-minute intraday candles are unavailable")
    today_rows = intraday[intraday.index.date == current_time.date()]
    if today_rows.empty:
        raise RuntimeError(f"Today's {symbol} intraday candles are unavailable")

    history_15 = _frame_in_ist(
        fetch_v3_historical_minutes(instrument_key, minutes=15, lookback_days=12)
    )
    previous_rows = history_15[history_15.index.date < current_time.date()]
    if previous_rows.empty:
        raise RuntimeError(f"Previous trading-day {symbol} OHLC is unavailable")
    previous_date = previous_rows.index.date[-1]
    previous_day = previous_rows[previous_rows.index.date == previous_date]
    previous_ohlc = {
        "high": float(previous_day["high"].max()),
        "low": float(previous_day["low"].min()),
        "close": float(previous_day.iloc[-1]["close"]),
    }

    maximum_age = configured_positive_float("GANESH_DATA_MAX_AGE_SECONDS", 120.0)
    stream_spot, stream_age = _latest_stream_ltp(instrument_key, maximum_age)
    spot = stream_spot or float(today_rows.iloc[-1]["close"])
    latest_candle_age = _ganesh_candle_age_seconds(
        today_rows.index[-1],
        current_time,
        interval_minutes=1,
    )
    data_age = stream_age if stream_spot else latest_candle_age
    if data_age is not None and data_age > maximum_age:
        raise RuntimeError(f"{symbol} market data is stale ({data_age:.1f}s)")

    candle_start = active_two_hour_start(current_time)
    active_rows = today_rows[today_rows.index >= candle_start]
    if active_rows.empty:
        raise RuntimeError(f"Active two-hour {symbol} candle is unavailable")
    candle_open = float(active_rows.iloc[0]["open"])
    active_high = max(float(active_rows["high"].max()), spot)
    active_low = min(float(active_rows["low"].min()), spot)
    active_volume = float(active_rows["volume"].sum())

    historical_2h = _frame_in_ist(
        fetch_v3_historical_hours(instrument_key, hours=2, lookback_days=60)
    )
    prior_2h = historical_2h[historical_2h.index < candle_start]
    closes = [float(value) for value in prior_2h.get("close", pd.Series(dtype=float)).dropna()]
    closes.append(spot)
    bands = bollinger_bands(
        closes,
        period=max(to_int(os.getenv("GANESH_BB_PERIOD"), 20), 2),
        standard_deviations=configured_positive_float("GANESH_BB_STDDEV", 2.0),
    )
    if not bands:
        raise RuntimeError("Insufficient two-hour candles for Bollinger Bands")
    recent_volumes = [
        float(value)
        for value in prior_2h.get("volume", pd.Series(dtype=float)).dropna().tail(20)
    ]
    average_volume = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 0.0
    volume_ratio = active_volume / average_volume if average_volume > 0 else 0.0

    gap = opening_gap(
        previous_ohlc["close"],
        float(today_rows.iloc[0]["open"]),
        configured_non_negative_float("GANESH_MIN_GAP_PERCENT", 0.20),
    )
    pivots = classic_pivots(
        previous_ohlc["high"],
        previous_ohlc["low"],
        previous_ohlc["close"],
    )
    colour = candle_colour(
        candle_open,
        spot,
        configured_non_negative_float("GANESH_COLOUR_NEUTRAL_BUFFER_POINTS", 0.0),
    )
    session_open = pd.Timestamp(current_time.replace(hour=9, minute=15, second=0, microsecond=0))
    opening_minutes = max(to_int(os.getenv("GANESH_OPENING_RANGE_MINUTES"), 15), 5)
    opening_end = session_open + pd.Timedelta(minutes=opening_minutes)
    opening_range = (
        _ganesh_ohlc_summary(today_rows, session_open, opening_end)
        if pd.Timestamp(current_time) >= opening_end
        else {"complete": False}
    )
    completed_five_end = pd.Timestamp(current_time).floor("5min")
    completed_five_start = completed_five_end - pd.Timedelta(minutes=5)
    latest_completed_five = (
        _ganesh_ohlc_summary(today_rows, completed_five_start, completed_five_end)
        if completed_five_end > session_open
        else {"complete": False}
    )
    return {
        "symbol": symbol,
        "underlying_instrument_key": instrument_key,
        "timestamp": current_time.isoformat(),
        "spot": round(spot, 2),
        "data_age_seconds": round(float(data_age or 0), 2),
        "previous_date": str(previous_date),
        "previous_high": round(previous_ohlc["high"], 2),
        "previous_low": round(previous_ohlc["low"], 2),
        "previous_close": round(previous_ohlc["close"], 2),
        "today_open": round(float(today_rows.iloc[0]["open"]), 2),
        "gap": gap,
        "candle_start": candle_start.isoformat(),
        "candle_open": round(candle_open, 2),
        "candle_high": round(active_high, 2),
        "candle_low": round(active_low, 2),
        "candle_colour": colour,
        "bollinger": bands,
        "pivots": pivots,
        "active_volume": round(active_volume, 2),
        "volume_average_20": round(average_volume, 2),
        "volume_ratio": round(volume_ratio, 3),
        "volume_confirmed": average_volume > 0 and active_volume > average_volume,
        "opening_range": opening_range,
        "latest_completed_5m": latest_completed_five,
    }


GANESH_GAP_SCAN_COLUMNS = [
    "timestamp", "spot", "previous_close", "today_open", "gap_direction",
    "gap_points", "gap_percent", "candle_start", "candle_open", "candle_colour",
    "previous_confirmed_colour", "bb_upper", "bb_middle", "bb_lower", "P", "R1",
    "R2", "R3", "S1", "S2", "S3", "target_type", "target_level",
    "target_distance", "atm_strike", "expiry", "option_type", "option_ltp", "bid",
    "ask", "spread_percent", "volume_ratio", "entry_eligible", "reason", "phase",
]


def record_ganesh_gap_scan(snapshot, state, eligible, reason, option=None, target=None):
    option = option or {}
    target = target or {}
    gap = snapshot.get("gap") or {}
    bands = snapshot.get("bollinger") or {}
    pivots = snapshot.get("pivots") or {}
    row = {
        "timestamp": snapshot.get("timestamp") or now_ist().isoformat(),
        "spot": snapshot.get("spot"),
        "previous_close": snapshot.get("previous_close"),
        "today_open": snapshot.get("today_open"),
        "gap_direction": gap.get("direction"),
        "gap_points": gap.get("points"),
        "gap_percent": gap.get("percent"),
        "candle_start": snapshot.get("candle_start"),
        "candle_open": snapshot.get("candle_open"),
        "candle_colour": snapshot.get("candle_colour"),
        "previous_confirmed_colour": state.get("previous_confirmed_colour"),
        "bb_upper": bands.get("upper"),
        "bb_middle": bands.get("middle"),
        "bb_lower": bands.get("lower"),
        **{key: pivots.get(key) for key in ("P", "R1", "R2", "R3", "S1", "S2", "S3")},
        "target_type": target.get("type"),
        "target_level": target.get("level"),
        "target_distance": target.get("distance"),
        "atm_strike": option.get("strike"),
        "expiry": option.get("expiry"),
        "option_type": option.get("option_type"),
        "option_ltp": option.get("ltp"),
        "bid": option.get("bid_price"),
        "ask": option.get("ask_price"),
        "spread_percent": option.get("spread_percent"),
        "volume_ratio": snapshot.get("volume_ratio"),
        "entry_eligible": bool(eligible),
        "reason": reason,
        "phase": state.get("phase"),
    }
    scan_file = (
        GANESH_GAP_BANKNIFTY_SCAN_FILE
        if snapshot.get("symbol") == "BANKNIFTY"
        else GANESH_GAP_SCAN_FILE
    )
    locked_append_csv(scan_file, GANESH_GAP_SCAN_COLUMNS, row)


def read_watch_state(symbol):
    return read_json(watch_state_file(symbol), {})


def write_watch_state(symbol, state):
    write_json(watch_state_file(symbol), state)


def clear_watch_state(symbol):
    path = watch_state_file(symbol)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def configured_clock(env_key, default):
    raw = str(os.getenv(env_key, default)).strip()
    try:
        hour, minute = raw.split(":", 1)
        return time(int(hour), int(minute))
    except Exception as error:
        raise RuntimeError(f"{env_key} must use HH:MM format") from error


def watch_mode_enabled():
    return configured_bool("INDEX_WATCH_MODE_ENABLED", False)


def watch_mode_shadow_only():
    return configured_bool("INDEX_WATCH_MODE_SHADOW_ONLY", True)


def watch_minimum_score():
    return configured_non_negative_float("INDEX_WATCH_MIN_SCORE", 65.0)


def static_vamsi_minimum_score():
    minimum = configured_non_negative_float(
        "VAMSI_UNIFIED_SCORE_FALLBACK",
        55.0,
    )
    if minimum > 100:
        raise RuntimeError("VAMSI_UNIFIED_SCORE_FALLBACK must be at most 100")
    return minimum


def vamsi_score_rule(symbol):
    """Return today's adaptive rule or the configured static fallback."""
    fallback = static_vamsi_minimum_score()
    if configured_bool("VAMSI_ADAPTIVE_SCORE_ENABLED", True):
        adaptive = read_effective_score_rule(symbol, now_ist().date())
        if adaptive:
            return {
                "source": "ADAPTIVE",
                "mode": adaptive["mode"],
                "min_score": float(adaptive["min_score"]),
                "max_score": adaptive.get("max_score"),
            }
    return {
        "source": "STATIC_FALLBACK",
        "mode": "MIN",
        "min_score": fallback,
        "max_score": None,
    }


def direct_entry_minimum_score(symbol):
    return float(vamsi_score_rule(symbol)["min_score"])


def format_vamsi_score_rule(rule):
    if rule.get("mode") == "RANGE":
        return (
            f"adaptive range {float(rule['min_score']):.1f}-"
            f"{float(rule['max_score']):.1f}"
        )
    label = "adaptive minimum" if rule.get("source") == "ADAPTIVE" else "minimum"
    return f"{label} {float(rule['min_score']):.1f}"


def vamsi_entry_score_qualifies(score, symbol):
    value = to_float(score)
    rule = vamsi_score_rule(symbol)
    if rule.get("source") == "ADAPTIVE":
        if value < float(rule["min_score"]):
            return False
        maximum = rule.get("max_score")
        return maximum is None or value <= float(maximum)
    return value > float(rule["min_score"])


def vamsi_weighted_score_qualifies(score, symbol):
    """Backward-compatible name for callers outside the live entry path."""
    return vamsi_entry_score_qualifies(score, symbol)


def score_direction_from_technicals(technicals):
    """Use the completed 15M direction when a neutral chain needs a candidate."""
    fifteen = technicals.get("fifteen_min", {}) or {}
    fifteen_bias = fifteen.get("bias")
    if fifteen_bias in {"BULLISH", "BEARISH"}:
        return fifteen_bias
    return None


def option_flow_supports_watch(candidate):
    flow = ((candidate.get("technicals") or {}).get("atm_option_flow") or {})
    minimum_ratio = configured_non_negative_float(
        "INDEX_WATCH_MIN_OPTION_VOLUME_RATIO", 1.20
    )
    close = to_float(flow.get("close"))
    vwap = to_float(flow.get("vwap"))
    volume_ratio = to_float(flow.get("volume_ratio"))
    return {
        "allowed": bool(
            close > 0
            and vwap > 0
            and close > vwap
            and volume_ratio >= minimum_ratio
        ),
        "close": close,
        "vwap": vwap,
        "volume_ratio": volume_ratio,
        "minimum_volume_ratio": minimum_ratio,
    }


def start_watch(symbol, candidate):
    now = now_ist()
    if now.time() >= configured_clock("INDEX_WATCH_START_CUTOFF", "14:45"):
        return False
    confirmation_timeframe = "5M" if candidate.get("timing_watch") else "15M"
    analysis_key = "five_min" if confirmation_timeframe == "5M" else "fifteen_min"
    confirmation_candle = (
        (candidate.get("technicals") or {}).get(analysis_key) or {}
    )
    required = ("candle_time", "open", "high", "low", "close")
    if any(confirmation_candle.get(key) is None for key in required):
        return False
    direction = str(candidate.get("direction") or "").upper()
    if direction not in {"BULLISH", "BEARISH"}:
        return False

    existing = read_watch_state(symbol)
    if (
        existing.get("direction") == direction
        and existing.get("confirmation_timeframe") == confirmation_timeframe
        and existing.get("base_candle", {}).get("candle_time")
        == confirmation_candle.get("candle_time")
    ):
        return True

    state = {
        "symbol": symbol,
        "direction": direction,
        "base_score": round(candidate_weighted_score(candidate), 2),
        "started_at": now.isoformat(),
        "confirmation_timeframe": confirmation_timeframe,
        "base_candle": {
            key: confirmation_candle.get(key)
            for key in ("candle_time", "open", "high", "low", "close")
        },
        "chain_bias": (candidate.get("option_summary") or {}).get("chain_bias"),
        "chain_confidence": (candidate.get("option_summary") or {}).get("chain_confidence"),
        "shadow_only": watch_mode_shadow_only(),
    }
    write_watch_state(symbol, state)
    log(
        f"{symbol} WATCH_STARTED direction={direction} score={state['base_score']:.1f} "
        f"timeframe={confirmation_timeframe} "
        f"base_candle={confirmation_candle.get('candle_time')} "
        f"shadow={state['shadow_only']}"
    )
    return True


def expire_watch(symbol, reason):
    if read_watch_state(symbol):
        log(f"{symbol} WATCH_EXPIRED reason={reason}")
    clear_watch_state(symbol)


def process_watch(symbol, candidate):
    """Return a revalidated candidate after its configured confirmation candle."""
    state = read_watch_state(symbol)
    if not state:
        return None
    if now_ist().time() >= configured_clock("INDEX_WATCH_ACTIVATION_CUTOFF", "15:00"):
        expire_watch(symbol, "activation cutoff reached")
        return None
    if not candidate:
        expire_watch(symbol, "signal no longer qualifies for watch band")
        return None
    if not candidate.get("watch_eligible") and not candidate.get("allowed"):
        expire_watch(symbol, f"live safety gate failed: {candidate.get('reason')}")
        return None

    direction = str(candidate.get("direction") or "").upper()
    if direction != state.get("direction"):
        expire_watch(symbol, f"direction changed to {direction or 'NEUTRAL'}")
        return None
    score = candidate_weighted_score(candidate)
    if not vamsi_entry_score_qualifies(score, symbol):
        score_rule = vamsi_score_rule(symbol)
        expire_watch(
            symbol,
            f"score {score:.1f} does not satisfy Vamsi "
            f"{format_vamsi_score_rule(score_rule)}",
        )
        return None

    technicals = candidate.get("technicals") or {}
    confirmation_timeframe = state.get("confirmation_timeframe", "15M")
    analysis_key = "five_min" if confirmation_timeframe == "5M" else "fifteen_min"
    interval_minutes = 5 if confirmation_timeframe == "5M" else 15
    confirmation_candle = technicals.get(analysis_key) or {}
    current_time_text = confirmation_candle.get("candle_time")
    base_time_text = (state.get("base_candle") or {}).get("candle_time")
    if not current_time_text or not base_time_text:
        expire_watch(symbol, f"completed {confirmation_timeframe} candle unavailable")
        return None
    current_time = datetime.fromisoformat(current_time_text)
    base_time = datetime.fromisoformat(base_time_text)
    if current_time <= base_time:
        return None
    if current_time > base_time + timedelta(minutes=interval_minutes, seconds=30):
        expire_watch(symbol, "one-candle confirmation window elapsed")
        return None

    option_summary = candidate.get("option_summary") or {}
    chain_bias = str(option_summary.get("chain_bias") or "NEUTRAL").upper()
    chain_confidence = str(option_summary.get("chain_confidence") or "LOW").upper()
    if (
        chain_confidence == "HIGH"
        and chain_bias in {"BULLISH", "BEARISH"}
        and chain_bias != direction
    ):
        expire_watch(symbol, "strong option-chain direction is opposite")
        return None

    five = technicals.get("five_min") or {}
    fifteen = technicals.get("fifteen_min") or {}
    if five.get("bias") != direction or fifteen.get("bias") != direction:
        expire_watch(
            symbol,
            "timing confirmation failed: "
            f"15M={fifteen.get('bias') or 'UNAVAILABLE'}; "
            f"5M={five.get('bias') or 'UNAVAILABLE'}; required={direction}",
        )
        return None

    candle_result = candle_confirmation(
        state.get("base_candle"),
        confirmation_candle,
        direction,
        min_body_ratio=configured_non_negative_float(
            "INDEX_WATCH_MIN_BODY_RATIO", 0.55
        ),
        close_edge_fraction=configured_non_negative_float(
            "INDEX_WATCH_CLOSE_EDGE_FRACTION", 0.25
        ),
    )
    flow_result = option_flow_supports_watch(candidate)
    if not candle_result.get("confirmed") or not flow_result.get("allowed"):
        reasons = []
        if not candle_result.get("confirmed"):
            reasons.append(candle_result.get("reason"))
        if not flow_result.get("allowed"):
            reasons.append(
                "option premium/VWAP/volume confirmation failed "
                f"(close={flow_result['close']:.2f}, vwap={flow_result['vwap']:.2f}, "
                f"volume_ratio={flow_result['volume_ratio']:.2f})"
            )
        expire_watch(symbol, "; ".join(reasons))
        return None

    log(
        f"{symbol} WATCH_CONFIRMED direction={direction} score={score:.1f} "
        f"timeframe={confirmation_timeframe} "
        f"body_ratio={candle_result.get('body_ratio')} "
        f"volume_ratio={flow_result.get('volume_ratio'):.2f} "
        f"patterns={candle_result.get('patterns')} shadow={watch_mode_shadow_only()}"
    )
    clear_watch_state(symbol)
    if watch_mode_shadow_only():
        record_analysis(
            symbol,
            candidate.get("option_summary") or {},
            candidate.get("technicals") or {},
            {
                "execute_trade": False,
                "decision": "WATCH_CONFIRMED_SHADOW",
                "confidence": "MEDIUM",
                "target_price": candidate.get("target_price"),
                "stop_loss_price": candidate.get("stop_loss_price"),
                "reason": "Watch confirmation passed in shadow-only mode.",
            },
        )
        return None

    confirmed = deepcopy(candidate)
    score_rule = vamsi_score_rule(symbol)
    confirmed["allowed"] = True
    confirmed["watch_confirmed"] = True
    confirmed["entry_minimum_score"] = score_rule["min_score"]
    confirmed["entry_maximum_score"] = score_rule.get("max_score")
    confirmed["score_rule_source"] = score_rule.get("source")
    confirmed["score_cutoff_approved"] = True
    confirmed["reason"] = "watch-mode completed candle confirmation passed"
    return confirmed


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


@contextmanager
def position_finalization_lock(symbol):
    """Serialize fill finalization between the entry and monitor processes."""
    lock_path = BASE_DIR / f".{symbol.lower()}_position_finalization.lock"
    with lock_path.open("a+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def portfolio_entry_lock():
    """Serialize the final portfolio check and broker entry submission."""
    with PORTFOLIO_ENTRY_LOCK_FILE.open("a+") as lock_file:
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


def register_same_index_reset_guard(symbol, state, journal_row, exit_reason):
    if not require_signal_reset_for_same_index_reentry():
        return
    if symbol not in SYMBOLS:
        return
    if str(state.get("instrument_class") or "INDEX_OPTION").upper() != "INDEX_OPTION":
        return
    direction = state.get("direction")
    if direction not in {"BULLISH", "BEARISH"}:
        return
    write_reentry_guard(
        symbol,
        {
            "date": now_ist().strftime("%Y-%m-%d"),
            "blocked_direction": direction,
            "trading_symbol": state.get("trading_symbol"),
            "stopped_at": now_ist().isoformat(),
            "exit_reason": exit_reason,
            "gross_pnl": journal_row.get("gross_pnl"),
            "reset_seen": False,
            "mode": "signal_reset",
        },
    )


def observe_signal_reset(symbol, direction):
    guard = read_reentry_guard(symbol)
    guard_mode = guard.get("mode")
    if guard_mode != "signal_reset" and loss_reentry_mode() != "reset":
        return

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
    if not guard:
        return ""
    if guard.get("blocked_direction") != direction:
        return ""

    if guard.get("mode") == "signal_reset":
        if not guard.get("reset_seen"):
            return (
                f"same-index second trade blocked after {guard.get('exit_reason')} at "
                f"{guard.get('stopped_at')}; wait for a neutral/opposite signal reset"
            )
        clear_reentry_guard(symbol)
        return ""

    mode = loss_reentry_mode()
    if mode == "off":
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
    maximum = max_index_trades_per_day()
    if maximum > 0 and index_trade_count_today() >= maximum:
        return f"maximum {maximum} index trades already used today"
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


def active_bot_states():
    return [
        state
        for state in (read_state(symbol) for symbol in BOT_STATE_SLOTS)
        if state_is_active(state)
    ]


def underlying_symbol_for_state_slot(state_slot, state=None):
    state = state or {}
    underlying = str(state.get("underlying_symbol") or state.get("symbol") or "").upper()
    if underlying in SYMBOLS:
        return underlying
    return state_slot


def selective_index_has_active_state(symbol):
    return state_is_active(read_state(symbol))


def active_index_instrument_keys(symbol):
    """Return the selective contract already managed for one underlying."""
    state = read_state(symbol)
    if state_is_active(state) and state.get("instrument_key"):
        return {state["instrument_key"]}
    return set()


def today_closed_trade_rows():
    if not TRADE_HISTORY_FILE.exists():
        return []
    today = now_ist().strftime("%Y-%m-%d")
    try:
        with TRADE_HISTORY_FILE.open("r", newline="") as handle:
            return [
                row
                for row in csv.DictReader(handle)
                if str(row.get("trade_date")) == today
                and str(row.get("status") or "CLOSED").upper() == "CLOSED"
            ]
    except Exception as error:
        log(f"Could not read today's closed trades for portfolio circuit: {error}")
        return []


def consecutive_losses_today():
    count = 0
    for row in reversed(today_closed_trade_rows()):
        pnl = to_float(row.get("gross_pnl"))
        if pnl < 0:
            count += 1
        else:
            break
    return count


def read_day_risk_state():
    today = now_ist().strftime("%Y-%m-%d")
    state = read_json(DAY_RISK_STATE_FILE, {})
    if state.get("date") != today:
        return {"date": today, "peak_pnl": 0.0}
    return state


def write_day_risk_state(state):
    state["date"] = now_ist().strftime("%Y-%m-%d")
    state["updated_at"] = now_ist().isoformat()
    write_json(DAY_RISK_STATE_FILE, state)


def portfolio_day_circuit():
    """Return the persistent day-level entry circuit; exits remain unaffected."""
    realized = today_realized_pnl()
    try:
        unrealized = bot_unrealized_pnl()
    except Exception as error:
        log(f"Portfolio circuit could not read unrealized P&L: {error}")
        unrealized = 0.0
    pnl = round(realized + unrealized, 2)
    broker_pnl = None
    if (
        os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
        and configured_bool("BROKER_RECONCILIATION_REQUIRED", True)
    ):
        try:
            broker_pnl = broker_derivatives_day_pnl()
            pnl = broker_pnl
        except Exception as error:
            return {
                "allowed": False,
                "reason": f"broker P&L reconciliation failed: {error}",
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "combined_pnl": pnl,
                "broker_day_pnl": None,
                "score_penalty": 0.0,
            }
    state = read_day_risk_state()
    state["peak_pnl"] = round(max(to_float(state.get("peak_pnl")), pnl, 0.0), 2)

    soft_loss_fallback = configured_non_negative_float("DAILY_SOFT_LOSS", 0.0)
    soft_loss = to_float(
        active_value("dailySoftLoss", soft_loss_fallback),
        soft_loss_fallback,
    )
    pause_minutes = configured_non_negative_float("DAILY_SOFT_PAUSE_MINUTES", 30.0)
    score_penalty = configured_non_negative_float("DAILY_SOFT_SCORE_PENALTY", 10.0)
    hard_loss = daily_max_loss()
    profit_target = daily_profit_target()
    max_consecutive = max(to_int(os.getenv("MAX_CONSECUTIVE_LOSSES"), 3), 0)
    giveback_trigger_fallback = configured_non_negative_float(
        "PEAK_PROFIT_GIVEBACK_TRIGGER", 0.0
    )
    giveback_trigger = to_float(
        active_value("peakProfitGivebackTrigger", giveback_trigger_fallback),
        giveback_trigger_fallback,
    )
    giveback_percent = configured_non_negative_float(
        "MAX_PEAK_GIVEBACK_PERCENT", 50.0
    )
    if giveback_percent > 100:
        raise RuntimeError("MAX_PEAK_GIVEBACK_PERCENT must be at most 100")

    now = now_ist()
    if soft_loss > 0 and pnl <= -soft_loss and not state.get("soft_triggered_at"):
        state["soft_triggered_at"] = now.isoformat()
        state["soft_pause_until"] = (now + timedelta(minutes=pause_minutes)).isoformat()

    write_day_risk_state(state)
    result = {
        "allowed": True,
        "reason": "daily portfolio circuit accepted",
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "combined_pnl": pnl,
        "broker_day_pnl": broker_pnl,
        "peak_pnl": state["peak_pnl"],
        "consecutive_losses": consecutive_losses_today(),
        "score_penalty": score_penalty if state.get("soft_triggered_at") else 0.0,
    }

    if profit_target > 0 and pnl >= profit_target:
        result.update(
            allowed=False,
            reason=f"daily profit target reached: P&L Rs {pnl:.2f} >= Rs {profit_target:.2f}",
        )
        return result
    if hard_loss > 0 and pnl <= -hard_loss:
        result.update(
            allowed=False,
            reason=f"daily hard loss reached: P&L Rs {pnl:.2f} <= -Rs {hard_loss:.2f}",
        )
        return result
    if max_consecutive > 0 and result["consecutive_losses"] >= max_consecutive:
        result.update(
            allowed=False,
            reason=f"{result['consecutive_losses']} consecutive losses reached daily limit",
        )
        return result
    if giveback_trigger > 0 and state["peak_pnl"] >= giveback_trigger:
        floor = state["peak_pnl"] * (1.0 - giveback_percent / 100.0)
        if pnl <= floor:
            result.update(
                allowed=False,
                reason=(
                    f"daily peak-profit giveback reached: P&L Rs {pnl:.2f}, "
                    f"peak Rs {state['peak_pnl']:.2f}, floor Rs {floor:.2f}"
                ),
            )
            return result

    pause_until = state.get("soft_pause_until")
    if pause_until:
        try:
            pause_time = datetime.fromisoformat(pause_until)
            if now < pause_time:
                result.update(
                    allowed=False,
                    reason=(
                        f"soft-loss pause active until {pause_time.strftime('%H:%M:%S')} IST; "
                        f"combined P&L Rs {pnl:.2f}"
                    ),
                )
        except Exception:
            pass
    return result


def candidate_entry_score(chosen):
    return to_float(
        (chosen.get("entry_score") or {}).get("score"),
        to_float(
            (chosen.get("weighted") or {}).get("score"),
            to_float(chosen.get("weighted_score"), to_float(chosen.get("signal_score"))),
        ),
    )


def candidate_weighted_score(chosen):
    """Backward-compatible accessor; unified entry score is preferred."""
    return candidate_entry_score(chosen)


def candidate_score_version(chosen):
    return (chosen.get("entry_score") or {}).get("score_version")


def pre_order_portfolio_decision(chosen, quantity, entry_price, stop_loss_price):
    """Final shared gate, designed to run under ``portfolio_entry_lock``."""
    health = monitor_health_gate()
    if not health["allowed"]:
        return {"allowed": False, "reason": health["reason"], "monitor_health": health}
    reconciliation = broker_pending_order_gate()
    if not reconciliation["allowed"]:
        return {
            "allowed": False,
            "reason": reconciliation["reason"],
            "broker_reconciliation": reconciliation,
        }
    circuit = portfolio_day_circuit()
    if not circuit["allowed"]:
        return {"allowed": False, "reason": circuit["reason"], "circuit": circuit}

    symbol = str(chosen.get("symbol") or "").upper()
    instrument_key = (chosen.get("instrument") or {}).get("instrument_key")
    if symbol in SYMBOLS:
        if selective_index_has_active_state(symbol):
            return {
                "allowed": False,
                "reason": f"{symbol} already has an active selective position",
            }
        if instrument_key and instrument_key in active_index_instrument_keys(symbol):
            return {
                "allowed": False,
                "reason": f"{symbol} contract {instrument_key} is already managed",
            }
    score = candidate_weighted_score(chosen)
    base_minimum = to_float(
        chosen.get("entry_minimum_score"),
        MIN_SCORE_BY_SYMBOL.get(symbol, 65),
    )
    maximum_score = chosen.get("entry_maximum_score")
    if maximum_score is not None and score > to_float(maximum_score, 100.0):
        return {
            "allowed": False,
            "reason": (
                f"entry score {score:.1f} is above today's adaptive maximum "
                f"{to_float(maximum_score):.1f}"
            ),
            "circuit": circuit,
        }
    required_score = base_minimum
    # A Vamsi score-band-approved setup has already passed the configured
    # unified entry-score rule. Keep hard day/risk/correlation gates below, but do
    # not silently raise the approved lower boundary again at order time.
    if not chosen.get("score_cutoff_approved"):
        required_score += to_float(circuit.get("score_penalty"))
        if symbol in SYMBOLS and index_trade_count_today() >= 1:
            required_score += second_index_trade_score_bonus()
            prior_trade = last_index_trade_today()
            if prior_trade and to_float(prior_trade.get("gross_pnl")) < 0:
                required_score += second_trade_after_loss_score_bonus()
    if score < required_score:
        return {
            "allowed": False,
            "reason": (
                f"entry score {score:.1f} is below portfolio-adjusted minimum "
                f"{required_score:.1f}"
            ),
            "circuit": circuit,
        }

    if symbol in SYMBOLS:
        proposed_risk = proposed_position_risk(
            entry_price,
            stop_loss_price,
            quantity,
            chosen.get("transaction_type", "BUY"),
        )
        remaining_budget = remaining_index_risk_budget()
        if remaining_budget > 0 and proposed_risk > remaining_budget:
            return {
                "allowed": False,
                "reason": (
                    f"planned risk Rs {proposed_risk:.2f} exceeds remaining "
                    f"index risk budget Rs {remaining_budget:.2f}"
                ),
                "circuit": circuit,
                "proposed_risk": proposed_risk,
                "remaining_index_risk_budget": remaining_budget,
            }

    states = active_bot_states()
    open_risk_fallback = configured_non_negative_float(
        "MAX_OPEN_PORTFOLIO_RISK", 0.0
    )
    risk = aggregate_risk_decision(
        states,
        entry_price,
        stop_loss_price,
        quantity,
        chosen.get("transaction_type", "BUY"),
        to_float(
            active_value("maxOpenPortfolioRisk", open_risk_fallback),
            open_risk_fallback,
        ),
        configured_non_negative_float("PORTFOLIO_RISK_BUFFER_PERCENT", 15.0),
    )
    if not risk["allowed"]:
        return {"allowed": False, "reason": risk["reason"], "risk": risk, "circuit": circuit}

    proposed = {
        "symbol": symbol,
        "underlying_symbol": chosen.get("underlying_symbol", symbol),
        "direction": chosen.get("direction"),
        "weighted_score": score,
    }
    correlation = correlation_decision(
        proposed,
        states,
        configured_non_negative_float("SAME_DIRECTION_INDEX_MIN_SCORE", 80.0),
        max(to_int(os.getenv("MAX_SAME_DIRECTION_POSITIONS"), 2), 1),
        configured_bool("ALLOW_PROFIT_LOCKED_CORRELATED_POSITIONS", True),
    )
    if not correlation["allowed"]:
        return {
            "allowed": False,
            "reason": correlation["reason"],
            "risk": risk,
            "correlation": correlation,
            "circuit": circuit,
        }
    return {
        "allowed": True,
        "reason": "portfolio entry accepted",
        "risk": risk,
        "correlation": correlation,
        "circuit": circuit,
        "required_score": required_score,
    }


def daily_profit_target():
    fallback = to_float(os.getenv("DAILY_PROFIT_TARGET"), 10000)
    return to_float(active_value("dailyProfitTarget", fallback), fallback)


def after_profit_target_mode():
    return os.getenv("AFTER_PROFIT_TARGET_MODE", "paper").strip().lower()


def daily_profit_target_reached():
    target = daily_profit_target()

    if target <= 0:
        return False

    return today_realized_pnl() >= target


def daily_max_loss():
    fallback = to_float(os.getenv("DAILY_MAX_LOSS"), 10000)
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


def index_trade_count_today():
    return sum(trade_count_for(symbol) for symbol in SYMBOLS)


def sentiment_exit_enabled_for_state(state):
    return True


def max_index_trades_per_day():
    return max(to_int(os.getenv("MAX_INDEX_TRADES_PER_DAY"), 2), 0)


def max_daily_index_risk():
    fallback = configured_non_negative_float("MAX_DAILY_INDEX_RISK", daily_max_loss())
    return to_float(active_value("maxDailyIndexRisk", fallback), fallback)


def index_risk_per_trade_limit():
    explicit = configured_non_negative_float("INDEX_RISK_PER_TRADE", 0.0)
    if explicit <= 0:
        trades = max(max_index_trades_per_day(), 1)
        budget = max_daily_index_risk()
        explicit = budget / trades if budget > 0 else 0.0
    return to_float(active_value("indexRiskPerTrade", explicit), explicit)


def second_index_trade_score_bonus():
    return configured_non_negative_float("SECOND_INDEX_TRADE_SCORE_BONUS", 5.0)


def second_trade_after_loss_score_bonus():
    return configured_non_negative_float("SECOND_TRADE_AFTER_LOSS_SCORE_BONUS", 10.0)


def require_signal_reset_for_same_index_reentry():
    return configured_bool("REQUIRE_SIGNAL_RESET_FOR_SAME_INDEX_REENTRY", True)


def today_index_trade_rows():
    if not TRADE_HISTORY_FILE.exists():
        return []
    today = now_ist().strftime("%Y-%m-%d")
    rows = []
    try:
        with TRADE_HISTORY_FILE.open("r", newline="", errors="ignore") as handle:
            for row in csv.DictReader(handle):
                if str(row.get("trade_date")) != today:
                    continue
                if str(row.get("symbol") or "").upper() not in SYMBOLS:
                    continue
                if str(row.get("instrument_class") or "INDEX_OPTION").upper() != "INDEX_OPTION":
                    continue
                rows.append(row)
    except Exception as error:
        verbose_log(f"Could not read today's index trades: {error}")
    return rows


def last_index_trade_today():
    rows = today_index_trade_rows()
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda row: row.get("exit_time") or row.get("entry_time") or "",
    )[-1]


def today_index_realized_pnl():
    return round(sum(to_float(row.get("gross_pnl")) for row in today_index_trade_rows()), 2)


def today_index_realized_loss():
    return max(-today_index_realized_pnl(), 0.0)


def current_index_open_risk():
    return total_open_risk([read_state(symbol) for symbol in SYMBOLS])


def remaining_index_risk_budget():
    budget = max_daily_index_risk()
    if budget <= 0:
        return 0.0
    remaining = budget - today_index_realized_loss() - current_index_open_risk()
    return round(max(remaining, 0.0), 2)


def planned_trade_context(symbol):
    sequence = index_trade_count_today() + 1 if symbol in SYMBOLS else trade_count_for(symbol) + 1
    prior = last_index_trade_today() if symbol in SYMBOLS else None
    prior_pnl = to_float((prior or {}).get("gross_pnl"), 0.0)
    return {
        "trade_sequence": sequence,
        "prior_trade_symbol": (prior or {}).get("symbol", ""),
        "prior_trade_outcome": (
            "WIN" if prior and prior_pnl > 0 else "LOSS" if prior and prior_pnl < 0 else "FLAT" if prior else ""
        ),
        "prior_trade_pnl": prior_pnl if prior else "",
        "remaining_index_risk_budget": remaining_index_risk_budget() if symbol in SYMBOLS else "",
        "risk_per_trade_limit": index_risk_per_trade_limit() if symbol in SYMBOLS else "",
    }


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
    method = str(method).upper()
    attempts = max(to_int(os.getenv("UPSTOX_READ_RETRY_ATTEMPTS"), 3), 1) if method == "GET" else 1
    retry_statuses = {429, 500, 502, 503, 504}
    for attempt in range(attempts):
        try:
            response = requests.request(
                method, url, headers=upstox_headers(), timeout=30, **kwargs
            )
        except requests.RequestException:
            if attempt + 1 >= attempts:
                raise
            delay = min(0.5 * (2 ** attempt) + random.uniform(0, 0.2), 4.0)
            time_module.sleep(delay)
            continue
        if response.status_code < 300:
            return response.json()
        if response.status_code not in retry_statuses or attempt + 1 >= attempts:
            raise RuntimeError(
                f"Upstox API failed {response.status_code}: {response.text[:500]}"
            )
        retry_after = to_float(response.headers.get("Retry-After"), 0)
        delay = retry_after or min(0.5 * (2 ** attempt) + random.uniform(0, 0.2), 4.0)
        time_module.sleep(delay)
    raise RuntimeError("Upstox API request failed without a response")

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

    ``MAX`` deploys the currently available Upstox equity balance after a
    configurable cash buffer. A value of 1 means exactly one lot. Larger
    numeric values are rupees and are rounded down to whole lots.
    """
    fallback = os.getenv(
        "OPTION_CAPITAL_PER_ENTRY",
        str(DEFAULT_OPTION_CAPITAL_PER_ENTRY),
    ).strip()
    raw_value = str(active_value("optionCapitalPerEntry", fallback)).strip()
    account_cap = configured_non_negative_float("ACCOUNT_MAX_OPTION_CAPITAL", 0.0)
    if raw_value.upper() == "MAX" or to_float(raw_value, 0.0) < 0:
        if account_cap > 0:
            return account_cap
        return "MAX"
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
    return min(capital, account_cap) if account_cap > 0 else capital


def maximum_available_option_capital(force_refresh=False):
    """Return usable buying capital while retaining a cash/charges buffer."""
    cache_key = "available_option_capital"
    now = time_module.time()
    cached = BROKER_READ_CACHE.get(cache_key) or {}
    cache_seconds = configured_non_negative_float(
        "MAX_CAPITAL_BALANCE_CACHE_SECONDS", 5.0
    )
    if (
        not force_refresh
        and cached
        and now - to_float(cached.get("timestamp")) <= cache_seconds
    ):
        return to_float(cached.get("value"))

    try:
        available = available_equity_margin()
    except Exception as error:
        log(f"MAX capital unavailable: could not read Upstox funds: {error}")
        return 0.0

    use_percent = configured_positive_float("MAX_CAPITAL_USE_PERCENT", 95.0)
    if use_percent > 100:
        raise RuntimeError("MAX_CAPITAL_USE_PERCENT must be at most 100")
    reserve = configured_non_negative_float("MAX_CAPITAL_RESERVE_RUPEES", 1000.0)
    usable = max(available - reserve, 0.0) * use_percent / 100.0
    account_cap = configured_non_negative_float("ACCOUNT_MAX_OPTION_CAPITAL", 0.0)
    if account_cap > 0:
        usable = min(usable, account_cap)
    BROKER_READ_CACHE[cache_key] = {"timestamp": now, "value": usable}
    verbose_log(
        f"MAX capital sizing: available={available:.2f} reserve={reserve:.2f} "
        f"use_percent={use_percent:.1f} usable={usable:.2f}"
    )
    return usable


def max_lots_per_entry():
    """Optional hard ceiling; zero means no additional lot ceiling."""
    raw_value = os.getenv("MAX_LOTS_PER_ENTRY", "0").strip()
    try:
        maximum = int(raw_value)
    except ValueError as error:
        raise RuntimeError("MAX_LOTS_PER_ENTRY must be a whole number or 0") from error
    if maximum < 0:
        raise RuntimeError("MAX_LOTS_PER_ENTRY cannot be negative")
    account_maximum = to_int(os.getenv("ACCOUNT_MAX_LOTS_PER_ENTRY"), 0)
    if account_maximum < 0:
        raise RuntimeError("ACCOUNT_MAX_LOTS_PER_ENTRY cannot be negative")
    ceilings = [value for value in (maximum, account_maximum) if value > 0]
    return min(ceilings) if ceilings else 0


def order_quantity_for(
    symbol,
    instrument,
    entry_price=None,
    stop_loss_price=None,
    transaction_type="BUY",
    capital_override=None,
):
    lot_size = int(instrument["lot_size"])
    capital = option_capital_per_entry() if capital_override is None else capital_override

    # The only supported live entry is a long option BUY. Keep this guard so
    # an accidental legacy caller cannot use this sizing for a short option.
    if str(transaction_type).upper() != "BUY":
        return 0

    if capital == "MAX":
        capital = maximum_available_option_capital()

    if capital == 1 or entry_price is None or float(entry_price) <= 0:
        lots = 1
    else:
        value_per_lot = float(entry_price) * lot_size
        lots = int(capital // value_per_lot)

    maximum = max_lots_per_entry()
    if maximum > 0:
        lots = min(lots, maximum)

    if symbol in SYMBOLS and entry_price is not None and stop_loss_price is not None:
        risk_per_lot = proposed_position_risk(
            entry_price,
            stop_loss_price,
            lot_size,
            transaction_type,
        )
        risk_candidates = [
            value
            for value in (
                index_risk_per_trade_limit(),
                remaining_index_risk_budget(),
            )
            if value > 0
        ]
        risk_budget = min(risk_candidates) if risk_candidates else 0.0
        if risk_per_lot > 0 and risk_budget > 0:
            lots = min(lots, int(risk_budget // risk_per_lot))

    return lot_size * max(lots, 0)


def place_market_order(instrument, transaction_type, quantity, product="I"):
    payload = {
        "quantity": int(quantity),
        "product": str(product).upper(),
        "validity": "DAY",
        "price": 0,
        "tag": "index_bot",
        "instrument_token": instrument["instrument_key"],
        "order_type": "MARKET",
        "transaction_type": transaction_type,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
        "market_protection": market_protection_percent(),
    }

    result = upstox_request("POST", UPSTOX_PLACE_ORDER_URL, json=payload)
    if str(transaction_type).upper() == "BUY":
        BROKER_READ_CACHE.pop("available_option_capital", None)
    return result, payload


def place_stop_market_order(instrument, transaction_type, quantity, trigger_price, product="I"):
    payload = {
        "quantity": int(quantity),
        "product": str(product).upper(),
        "validity": "DAY",
        "price": 0,
        "tag": "index_bot_stop",
        "instrument_token": instrument["instrument_key"],
        "order_type": "SL-M",
        "transaction_type": transaction_type,
        "disclosed_quantity": 0,
        "trigger_price": round(float(trigger_price), 1),
        "is_amo": False,
        "market_protection": market_protection_percent(),
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
        "market_protection": market_protection_percent(),
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


def broker_derivatives_day_pnl():
    total = 0.0
    for position in get_open_positions(force=True):
        key = str(position.get("instrument_token") or position.get("instrument_key") or "")
        segment = str(position.get("segment") or position.get("exchange") or "").upper()
        if "NSE_FO" not in key and "NFO" not in segment and "FO" not in segment:
            continue
        realised = position.get("realised")
        unrealised = position.get("unrealised")
        if realised is not None or unrealised is not None:
            total += to_float(realised) + to_float(unrealised)
        elif position.get("day_pnl") is not None:
            total += to_float(position.get("day_pnl"))
        else:
            total += to_float(position.get("pnl"))
    return round(total, 2)


def get_order_book(force=False):
    cache_key = "order_book"
    cached = BROKER_READ_CACHE.get(cache_key)
    now = time_module.monotonic()
    if not force and cached and now - cached["at"] < broker_read_cache_seconds():
        return deepcopy(cached["data"])
    result = upstox_request("GET", UPSTOX_ORDER_BOOK_URL)
    data = result.get("data", []) or []
    BROKER_READ_CACHE[cache_key] = {"at": now, "data": deepcopy(data)}
    return data


def broker_pending_order_gate():
    if os.getenv("ENABLE_LIVE_TRADING", "false").lower() != "true":
        return {"allowed": True, "reason": "dry-run mode"}
    if not configured_bool("BROKER_RECONCILIATION_REQUIRED", True):
        return {"allowed": True, "reason": "broker reconciliation disabled"}
    try:
        orders = get_order_book(force=True)
    except Exception as error:
        return {"allowed": False, "reason": f"broker order reconciliation failed: {error}"}
    known_ids = set()
    for state in active_bot_states():
        for key in ("entry_order_id", "exit_order_id", "protective_stop_order_id"):
            if state.get(key):
                known_ids.add(str(state[key]))
    pending_statuses = {
        "open", "pending", "trigger pending", "put order req received",
        "validation pending", "modify pending", "cancel pending",
    }
    unknown = []
    for order in orders:
        tag = str(order.get("tag") or "")
        status = str(order.get("status") or "").strip().lower()
        order_id = str(order.get("order_id") or "")
        if tag in {"index_bot", "index_bot_stop"} and status in pending_statuses and order_id not in known_ids:
            unknown.append(order_id or "unknown")
    if unknown:
        return {
            "allowed": False,
            "reason": "untracked pending bot orders exist at Upstox: " + ", ".join(unknown[:5]),
            "order_ids": unknown,
        }
    return {"allowed": True, "reason": "broker orders reconciled"}


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


def find_matching_position_for_side(
    instrument_key,
    entry_transaction_type="BUY",
    force=False,
):
    for pos in get_open_positions(force=force):
        pos_key = pos.get("instrument_token") or pos.get("instrument_key")
        qty = position_quantity(pos)

        expected_sign = -1 if str(entry_transaction_type).upper() == "SELL" else 1
        if pos_key == instrument_key and qty * expected_sign > 0:
            return pos

    return None


def prepare_protection_failure_exit(
    symbol,
    instrument_key,
    entry_transaction_type,
    requested_quantity,
):
    """Claim one emergency exit only when the broker still shows the position."""
    fresh_state = read_state(symbol)
    if fresh_state.get("status") == "EXIT_PENDING":
        return {
            "allowed": False,
            "reason": "another exit is already pending",
            "state": fresh_state,
            "quantity": 0,
        }

    remaining_position = find_matching_position_for_side(
        instrument_key,
        entry_transaction_type,
        force=True,
    )
    remaining_quantity = (
        abs(position_quantity(remaining_position))
        if remaining_position
        else 0
    )
    if remaining_quantity <= 0:
        clear_state(symbol)
        return {
            "allowed": False,
            "reason": "broker no longer reports the filled position",
            "state": fresh_state,
            "quantity": 0,
        }

    emergency_quantity = min(int(requested_quantity), int(remaining_quantity))
    fresh_state["status"] = "EXIT_PENDING"
    fresh_state["exit_reason"] = "PROTECTION_FAILURE"
    fresh_state["exit_submission_in_progress"] = True
    write_state(symbol, fresh_state)
    return {
        "allowed": True,
        "reason": "emergency exit claimed",
        "state": fresh_state,
        "quantity": emergency_quantity,
    }


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


def index_contract_rows(recommendation, direction):
    """Return ATM and nearest one-strike-ITM rows for execution comparison."""
    atm = dict(recommendation.get("atm") or {})
    if not atm:
        return []
    # NIFTY signal evidence comes from the nearest expiry, but execution uses
    # the next-expiry ATM contract exactly.
    if recommendation.get("symbol") == "NIFTY":
        return [atm]
    if not configured_bool("INDEX_CONTRACT_SELECTION_ENABLED", True):
        return [atm]
    option_type = option_type_for(direction, "BUY")
    atm_strike = to_float(atm.get("strike"))
    nearby = [dict(row) for row in recommendation.get("nearby_contracts", []) if isinstance(row, dict)]
    if option_type == "CE":
        eligible = [row for row in nearby if 0 < to_float(row.get("strike")) < atm_strike]
        itm = max(eligible, key=lambda row: to_float(row.get("strike")), default=None)
    else:
        eligible = [row for row in nearby if to_float(row.get("strike")) > atm_strike]
        itm = min(eligible, key=lambda row: to_float(row.get("strike")), default=None)
    rows = [atm]
    if itm and to_float(itm.get("strike")) != atm_strike:
        rows.append(itm)
    return rows


def ganesh_gap_occupied_contract_keys(force=False):
    """Return broker-held derivative contracts that Ganesh must not add to."""
    if not ganesh_gap_live_enabled():
        return set()

    occupied = set()
    for position in get_open_positions(force=force):
        if position_quantity(position) == 0:
            continue
        instrument_key = str(
            position.get("instrument_token")
            or position.get("instrument_key")
            or ""
        ).strip()
        segment = str(
            position.get("segment") or position.get("exchange") or ""
        ).upper()
        if instrument_key.startswith("NSE_FO|") or segment in {"NSE_FO", "NFO"}:
            occupied.add(instrument_key)
    return occupied


def ganesh_gap_execution_rows(chain, requested_strike, option_type):
    """Order next-expiry execution rows as ATM, then progressively ITM."""
    rows = chain.copy()
    rows["_strike"] = pd.to_numeric(rows["strike"], errors="coerce")
    rows = rows.dropna(subset=["_strike"])
    if rows.empty:
        return []

    atm_index = (rows["_strike"] - float(requested_strike)).abs().idxmin()
    atm_row = rows.loc[atm_index]
    atm_strike_value = float(atm_row["_strike"])
    expiry = str(atm_row.get("expiry") or "")
    same_expiry = rows[rows["expiry"].astype(str) == expiry]
    if option_type == "CE":
        itm = same_expiry[same_expiry["_strike"] < atm_strike_value].sort_values(
            "_strike", ascending=False
        )
    else:
        itm = same_expiry[same_expiry["_strike"] > atm_strike_value].sort_values(
            "_strike", ascending=True
        )
    ordered = [atm_row.to_dict(), *itm.to_dict("records")]
    for row in ordered:
        row.pop("_strike", None)
    return ordered


def ganesh_gap_option_candidate(snapshot, transition, symbol=None):
    """Analyze the front expiry and select a liquid next-expiry ATM option."""
    symbol = str(symbol or snapshot.get("symbol") or "NIFTY").strip().upper()
    recommendation = get_index_recommendation(symbol)
    chain = pd.DataFrame(recommendation.get("execution_chain") or [])
    if chain.empty:
        raise RuntimeError(f"Next-expiry {symbol} execution chain is unavailable")
    interval_name = f"GANESH_{symbol}_STRIKE_INTERVAL"
    default_interval = 50 if symbol == "NIFTY" else 100
    requested_strike = atm_strike(
        snapshot["spot"],
        interval=max(to_int(os.getenv(interval_name), default_interval), 1),
    )
    option_type = transition["option_type"]
    prefix = "CE" if option_type == "CE" else "PE"
    maximum_spread = configured_positive_float("GANESH_MAX_OPTION_SPREAD_PERCENT", 2.5)
    occupied_keys = ganesh_gap_occupied_contract_keys()
    attempts = []
    selected = None
    for row in ganesh_gap_execution_rows(chain, requested_strike, option_type):
        strike = int(float(row["strike"]))
        expiry = row.get("expiry")
        instrument = find_index_option_instrument(
            symbol,
            expiry,
            strike,
            option_type,
        )
        instrument_key = str(instrument.get("instrument_key") or "")
        if instrument_key in occupied_keys:
            attempts.append(f"{strike} {option_type} is already held at the broker")
            verbose_log(
                f"GANESH GAP {symbol} contract skipped: "
                f"{instrument.get('trading_symbol')} is already held manually or "
                "by another strategy"
            )
            continue

        stream = read_market_cache(instrument_key) or {}
        quality = option_contract_quality(row, option_type, stream)
        ltp = to_float(quality.get("ltp"), to_float(row.get(f"{prefix}_ltp")))
        bid = to_float(
            quality.get("bid_price"), to_float(row.get(f"{prefix}_bid_price"))
        )
        ask = to_float(
            quality.get("ask_price"), to_float(row.get(f"{prefix}_ask_price"))
        )
        spread_percent = quality.get("spread_percent")
        blockers = []
        if ltp <= 0:
            blockers.append("option LTP is unavailable")
        if bid <= 0 or ask <= 0:
            blockers.append("option bid/ask is unavailable")
        if spread_percent is None:
            blockers.append("option spread cannot be calculated")
        elif float(spread_percent) > maximum_spread:
            blockers.append(
                f"option spread {float(spread_percent):.2f}% exceeds "
                f"{maximum_spread:.2f}%"
            )
        if blockers:
            attempts.append(f"{strike} {option_type}: {'; '.join(blockers)}")
            continue
        selected = {
            "row": row,
            "strike": strike,
            "expiry": expiry,
            "instrument": instrument,
            "quality": quality,
            "ltp": ltp,
            "bid": bid,
            "ask": ask,
            "spread_percent": spread_percent,
        }
        break

    if not selected:
        return {
            "allowed": False,
            "reason": "no unoccupied tradeable ATM/ITM contract: "
            + "; ".join(attempts),
            "option_type": option_type,
            "near_expiry_analysis": {},
        }

    row = selected["row"]
    strike = selected["strike"]
    expiry = selected["expiry"]
    instrument = selected["instrument"]
    quality = selected["quality"]
    ltp = selected["ltp"]
    bid = selected["bid"]
    ask = selected["ask"]
    spread_percent = selected["spread_percent"]
    blockers = []
    near_expiry_analysis = {}
    stream_instruments = [
        UNDERLYING_INDEX_KEYS["NIFTY"],
        UNDERLYING_INDEX_KEYS["BANKNIFTY"],
        instrument.get("instrument_key"),
    ]
    analysis_row = dict(recommendation.get("analysis_atm") or {})
    analysis_instrument = find_index_option_instrument(
        symbol,
        analysis_row.get("expiry"),
        analysis_row.get("strike"),
        option_type,
    )
    try:
        analysis_flow = get_option_volume_vwap_analysis(
            analysis_instrument["instrument_key"],
            side_label=analysis_instrument["trading_symbol"],
        )
    except Exception as error:
        analysis_flow = {
            "bias": "NEUTRAL",
            "confidence": "LOW",
            "reasons": [f"near-expiry ATM option flow unavailable: {error}"],
        }
    stream_instruments.append(analysis_instrument.get("instrument_key"))
    near_expiry_analysis = {
        "expiry": recommendation.get("analysis_expiry"),
        "strike": analysis_row.get("strike"),
        "option_type": option_type,
        "instrument_key": analysis_instrument.get("instrument_key"),
        "trading_symbol": analysis_instrument.get("trading_symbol"),
        "chain_bias": recommendation.get("direction"),
        "chain_confidence": recommendation.get("confidence"),
        "chain_score": recommendation.get("score"),
        "chain_reasons": recommendation.get("reasons", []),
        "option_flow": analysis_flow,
    }
    write_stream_instruments(
        stream_instruments
    )
    return {
        "allowed": not blockers,
        "reason": "; ".join(blockers) if blockers else "option contract is tradeable",
        "strike": strike,
        "expiry": str(expiry),
        "option_type": option_type,
        "ltp": round(ltp, 2),
        "bid_price": round(bid, 2),
        "ask_price": round(ask, 2),
        "spread_percent": round(float(spread_percent), 3) if spread_percent is not None else None,
        "instrument": instrument,
        "quality": quality,
        "contract_fallback_used": strike != int(float(requested_strike)),
        "contract_selection_attempts": attempts,
        "near_expiry_analysis": near_expiry_analysis,
        "recommendation": recommendation,
    }


def ganesh_gap_quantity(instrument, option_price):
    lot_size = int(instrument.get("lot_size") or 0)
    lots = max(to_int(os.getenv("GANESH_LOTS_PER_ENTRY"), 1), 1)
    quantity = lot_size * lots
    if lot_size <= 0 or option_price <= 0:
        return 0
    if ganesh_gap_live_enabled():
        available = maximum_available_option_capital(force_refresh=True)
        if float(option_price) * quantity > available:
            return 0
    return quantity


def ganesh_gap_option_levels(entry_price, quantity, target_distance):
    stop_percent = configured_positive_float("GANESH_OPTION_STOP_PERCENT", 20.0)
    stop_distance = float(entry_price) * stop_percent / 100.0
    maximum_loss = configured_non_negative_float("GANESH_MAX_RISK_PER_TRADE", 0.0)
    if maximum_loss > 0 and quantity > 0:
        stop_distance = min(stop_distance, maximum_loss / quantity)
    stop = max(float(entry_price) - stop_distance, 0.05)
    delta = configured_positive_float("GANESH_OPTION_DELTA_APPROXIMATION", 0.50)
    if delta > 1:
        raise RuntimeError("GANESH_OPTION_DELTA_APPROXIMATION must be at most 1")
    target = float(entry_price) + float(target_distance) * delta
    return {
        "target_price": round(target, 2),
        "stop_loss_price": round(stop, 2),
        "delta": delta,
    }


def market_window_ok():
    now = now_ist().time()
    return configured_clock("VAMSI_FIRST_ENTRY_TIME", "09:15") <= now <= configured_clock(
        "VAMSI_LAST_ENTRY_TIME",
        "15:25",
    )


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
    exit_profile=None,
    profit_protection_enabled_for_trade=None,
    trade_metadata=None,
    order_product="I",
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
            precision = (
                2
                if instrument_class == "STOCK_FUTURE"
                or is_ganesh_gap_strategy((trade_metadata or {}).get("strategy"))
                else 0
            )
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
        "order_product": str(order_product).upper(),
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
        "exit_profile": exit_profile or {},
        "profit_protection_enabled_for_trade": (
            bool(profit_protection_enabled_for_trade)
            if profit_protection_enabled_for_trade is not None
            else True
        ),
        "status": "POSITION_OPEN",
        "created_at": now_ist().isoformat(),
        "highest_ltp": round(float(entry_price), 2),
        "lowest_ltp": round(float(entry_price), 2),
        "profit_protection_stage": 0,
        "target_progress_percent": 0.0,
        "trailing_stop_active": False,
        "trailing_stop_reason": "",
    }
    if trade_metadata:
        state.update(trade_metadata)
    state["profit_booking_percent"] = profit_booking_percent_for_state(state)
    state["profit_booking_mode"] = profit_booking_mode_for_state(state)
    if state["profit_booking_mode"] == "runner":
        progress = state["profit_booking_percent"] / 100.0
        state["runner_activation_price"] = round(
            float(entry_price) + (float(target_price) - float(entry_price)) * progress,
            2,
        )
    state["profit_booking_price"] = profit_booking_price(state)

    write_state(symbol, state)

    log(
        f"{symbol} score {format_score(score)} bought {state['entry_price']} "
        f"target {target_price} stop {stop_loss_price} qty {quantity}"
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

    try:
        state["broker_day_pnl_at_exit"] = broker_derivatives_day_pnl()
    except Exception as error:
        state["broker_day_pnl_at_exit"] = ""
        log(f"{symbol} broker P&L snapshot unavailable at exit: {error}")
    journal_row = record_closed_trade(state, exit_price, exit_reason)
    register_losing_exit_guard(symbol, state, journal_row, exit_reason)
    register_same_index_reset_guard(symbol, state, journal_row, exit_reason)
    send_apple_closed_trade_alert(journal_row)
    log(
        f"{symbol} bought {journal_row.get('entry_price')} closed {journal_row.get('exit_price')} "
        f"pnl {journal_row.get('gross_pnl')} reason {exit_reason}"
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

def monitor_health_gate():
    if os.getenv("ENABLE_LIVE_TRADING", "false").lower() != "true":
        return {"allowed": True, "reason": "dry-run mode"}
    if not configured_bool("REQUIRE_HEALTHY_POSITION_MONITOR", True):
        return {"allowed": True, "reason": "monitor health gate disabled"}
    health = read_json(MONITOR_HEALTH_FILE, {})
    monitor_version = str(health.get("runtime_version") or "")
    if monitor_version != BOT_RUNTIME_VERSION:
        return {
            "allowed": False,
            "reason": (
                "position monitor code version does not match the entry bot; "
                "restart the monitor before allowing a new trade"
            ),
            "monitor_runtime_version": monitor_version or "missing",
            "entry_runtime_version": BOT_RUNTIME_VERSION,
        }
    maximum_age = configured_positive_float("MONITOR_HEALTH_MAX_AGE_SECONDS", 20.0)
    updated_at = to_float(health.get("updated_epoch"), 0)
    age = time_module.time() - updated_at if updated_at else 999999.0
    if not health or age > maximum_age:
        return {
            "allowed": False,
            "reason": f"position monitor heartbeat is stale ({age:.1f}s)",
        }
    if not health.get("healthy"):
        return {
            "allowed": False,
            "reason": "position monitor is unhealthy: " + str(health.get("last_error") or "unknown error"),
        }
    return {"allowed": True, "reason": "position monitor healthy", "age_seconds": age}


def run_position_monitor(log_empty=True):
    errors = []
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
            errors.append(f"{symbol}: {e}")
    threshold = max(to_int(os.getenv("MONITOR_FAILURE_THRESHOLD"), 3), 1)
    failure_count = to_int(read_json(MONITOR_HEALTH_FILE, {}).get("failure_count"), 0)
    failure_count = failure_count + 1 if errors else 0
    write_json(
        MONITOR_HEALTH_FILE,
        {
            "healthy": failure_count < threshold,
            "runtime_version": BOT_RUNTIME_VERSION,
            "failure_count": failure_count,
            "last_error": "; ".join(errors)[:1000] if errors else "",
            "updated_at": now_ist().isoformat(),
            "updated_epoch": time_module.time(),
        },
    )


def position_monitor_interval_seconds():
    """Return the live position-monitor cadence with a one-second safety floor."""
    return max(
        configured_positive_float("POSITION_MONITOR_INTERVAL_SECONDS", 2.0),
        1.0,
    )


def run_position_monitor_loop():
    """Monitor bot positions at the configured cadence during market hours."""
    interval_seconds = position_monitor_interval_seconds()
    log(
        "Position monitor loop started: "
        f"{interval_seconds:g}-second checks enabled."
    )
    while True:
        current = now_ist().time()
        if current > time(15, 30):
            log("Position monitor loop stopped at 03:30 PM IST.")
            return

        if time(9, 20) <= current <= time(15, 30):
            run_position_monitor(log_empty=False)
        time_module.sleep(interval_seconds)


def broker_protective_stop_required(state):
    if not configured_bool("BROKER_PROTECTIVE_STOP_ENABLED", True):
        return False
    return state.get("instrument_class") in {
        "INDEX_OPTION", "STOCK_OPTION", "STOCK_FUTURE"
    }

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
        product=state.get("order_product") or "I",
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
    state["broker_protective_stop_price"] = float(state["stop_loss_price"])
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


def synchronize_broker_protective_stop(symbol, state):
    """Move the broker stop forward after local profit protection advances."""
    if not broker_protective_stop_required(state):
        return state
    order_id = state.get("protective_stop_order_id")
    if not order_id:
        return ensure_protective_stop(symbol, state)
    desired = to_float(state.get("stop_loss_price"), 0)
    broker_price = to_float(state.get("broker_protective_stop_price"), 0)
    if desired <= 0 or abs(desired - broker_price) < 0.049:
        return state
    with protective_stop_lock(symbol):
        fresh = read_state(symbol)
        order_id = fresh.get("protective_stop_order_id")
        if not order_id:
            return fresh
        desired = to_float(fresh.get("stop_loss_price"), 0)
        broker_price = to_float(fresh.get("broker_protective_stop_price"), 0)
        if abs(desired - broker_price) < 0.049:
            return fresh
        modify_stop_order(order_id, int(fresh["quantity"]), desired)
        fresh["broker_protective_stop_price"] = desired
        fresh["broker_protective_stop_updated_at"] = now_ist().isoformat()
        write_state(symbol, fresh)
        log(
            f"{symbol} broker protective stop advanced: order_id={order_id} "
            f"trigger={desired:.2f}"
        )
        return fresh


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
    state.pop("broker_protective_stop_price", None)
    write_state(symbol, state)
    log(f"{symbol} protective stop cancelled before active exit: order_id={order_id}")
    return False


def ganesh_gap_state_slot(state):
    configured = str((state or {}).get("state_slot") or "").strip().upper()
    if configured in GANESH_GAP_STATE_SLOTS:
        return configured
    underlying = str(
        (state or {}).get("underlying_symbol") or (state or {}).get("symbol") or "NIFTY"
    ).strip().upper()
    return GANESH_GAP_STATE_BY_SYMBOL.get(underlying, GANESH_GAP_STATE)


def ganesh_gap_state_target(state, field):
    """Read generic target metadata with support for pre-BANKNIFTY NIFTY states."""
    return state.get(f"underlying_target_{field}", state.get(f"nifty_target_{field}"))


def close_ganesh_gap_paper_position(state, option_ltp, exit_reason, state_slot=None):
    state_slot = state_slot or ganesh_gap_state_slot(state)
    state["highest_ltp"] = max(
        to_float(state.get("highest_ltp"), option_ltp), float(option_ltp)
    )
    state["lowest_ltp"] = min(
        to_float(state.get("lowest_ltp"), option_ltp), float(option_ltp)
    )
    journal_row = record_closed_trade(state, option_ltp, exit_reason)
    send_apple_closed_trade_alert(journal_row)
    log(
        f"GANESH GAP paper bought {journal_row.get('entry_price')} "
        f"closed {journal_row.get('exit_price')} pnl {journal_row.get('gross_pnl')} "
        f"reason {exit_reason}"
    )
    clear_state(state_slot)
    return True


def submit_ganesh_gap_exit(state, option_ltp, exit_reason, quantity, state_slot=None):
    """Publish exit intent before broker calls so overlapping monitors cannot duplicate it."""
    state_slot = state_slot or ganesh_gap_state_slot(state)
    state["status"] = "EXIT_PENDING"
    state["exit_reason"] = exit_reason
    state["exit_fallback_price"] = option_ltp
    write_state(state_slot, state)

    if state.get("paper_trade"):
        return close_ganesh_gap_paper_position(
            state, option_ltp, exit_reason, state_slot=state_slot
        )
    if state.get("protective_stop_order_id") and cancel_protective_stop(
        state_slot, state
    ):
        return True

    instrument = {
        "instrument_key": state["instrument_key"],
        "trading_symbol": state.get("trading_symbol"),
    }
    try:
        result, payload = place_market_order(
            instrument,
            "SELL",
            quantity,
            product=state.get("order_product") or "I",
        )
    except Exception:
        state["status"] = "POSITION_OPEN"
        state.pop("exit_reason", None)
        state.pop("exit_fallback_price", None)
        write_state(state_slot, state)
        raise
    order_id = result.get("data", {}).get("order_id")
    if not order_id:
        state["status"] = "POSITION_OPEN"
        write_state(state_slot, state)
        raise RuntimeError(f"GANESH GAP SELL returned no order_id: {result}")
    details = wait_for_order_complete(order_id)
    if order_is_complete(details):
        complete_exit(
            state_slot,
            state,
            details,
            option_ltp,
            exit_reason,
            result,
            payload,
        )
    else:
        state["exit_order_id"] = order_id
        write_state(state_slot, state)
        log(f"GANESH GAP SELL pending: order_id={order_id} status={order_status(details)}")
    return True


def ganesh_continuation_structure_exit(state, current_time):
    if str(state.get("strategy_lane") or "").upper() != "CONTINUATION":
        return None
    completed_bucket = pd.Timestamp(current_time).floor("5min")
    bucket_key = completed_bucket.isoformat()
    if state.get("continuation_last_structure_bucket") == bucket_key:
        return None
    underlying_key = state.get("underlying_instrument_key")
    try:
        frame = _frame_in_ist(fetch_v3_intraday_minutes(underlying_key, minutes=5))
        rows = frame[
            (frame.index.date == current_time.date())
            & (frame.index < completed_bucket)
        ]
        if rows.empty:
            return None
        candle_start = pd.Timestamp(rows.index[-1])
        state["continuation_last_structure_bucket"] = bucket_key
        state["continuation_last_completed_5m_start"] = candle_start.isoformat()
        state["continuation_last_completed_5m_close"] = round(
            float(rows.iloc[-1]["close"]), 2
        )
        entry_start = state.get("continuation_entry_5m_start")
        if entry_start and candle_start <= pd.Timestamp(entry_start):
            return None
        close = float(rows.iloc[-1]["close"])
        buffer_points = configured_non_negative_float(
            "GANESH_CONTINUATION_INVALIDATION_BUFFER_POINTS", 0.0
        )
        direction = str(state.get("direction") or "").upper()
        if direction == GANESH_BULLISH:
            boundary = to_float(state.get("continuation_opening_range_high"), 0)
            invalidated = boundary > 0 and close < boundary - buffer_points
        else:
            boundary = to_float(state.get("continuation_opening_range_low"), 0)
            invalidated = boundary > 0 and close > boundary + buffer_points
        return "CONTINUATION_5M_INVALIDATION" if invalidated else None
    except Exception as error:
        log(f"GANESH GAP continuation 5M check unavailable: {error}")
        return None


def handle_ganesh_gap_position(state, verbose=True, state_slot=None):
    state_slot = state_slot or ganesh_gap_state_slot(state)
    symbol = str(state.get("underlying_symbol") or state.get("symbol") or "NIFTY").upper()
    underlying_key = state.get("underlying_instrument_key") or UNDERLYING_INDEX_KEYS.get(symbol)
    instrument_key = state.get("instrument_key")
    if not instrument_key:
        return False
    if state.get("status") == "EXIT_PENDING":
        return monitor_pending_exit(state_slot, state)

    entry_order_id = state.get("entry_order_id") or state.get("buy_order_id")
    if entry_order_id and state.get("status") == "BUY_PLACED_NOT_COMPLETE":
        details = wait_for_order_complete(entry_order_id, attempts=1, delay_seconds=0)
        if order_is_rejected(details):
            log(f"GANESH GAP pending BUY was {order_status(details)}; clearing state")
            clear_state(state_slot)
            return True
        if not order_is_complete(details):
            return True
        position = find_matching_position_for_side(instrument_key, "BUY")
        fill = position_avg_price(position, "BUY") if position else None
        fill = fill or to_float(details.get("average_price")) or to_float(state.get("entry_price"))
        if fill <= 0:
            log("GANESH GAP BUY completed but fill price is unavailable; retaining state")
            return True
        instrument = {
            "instrument_key": instrument_key,
            "trading_symbol": state.get("trading_symbol"),
            "lot_size": int(state.get("lot_size") or state.get("quantity") or 1),
        }
        finalize_and_protect_ganesh_gap_position(
            state,
            fill,
            int(state.get("quantity") or 0),
            instrument,
            entry_order_id,
            state_slot=state_slot,
        )
        return True

    if not state.get("paper_trade") and protective_stop_filled(state_slot, state):
        return True

    position = None
    if not state.get("paper_trade"):
        position = find_matching_position_for_side(instrument_key, "BUY")
        if not position:
            log("GANESH GAP state exists but matching broker position is not visible")
            return True
        if broker_protective_stop_required(state) and not state.get("protective_stop_order_id"):
            state = ensure_protective_stop(state_slot, state)

    option_quote = read_market_cache(instrument_key) or {}
    option_ltp = position_ltp(position) if position else to_float(option_quote.get("ltp"), 0)
    option_ltp = option_ltp or to_float(state.get("entry_price"), 0)
    if option_ltp <= 0:
        log("GANESH GAP option LTP is unavailable; broker stop remains active")
        return True
    state["highest_ltp"] = max(to_float(state.get("highest_ltp"), option_ltp), option_ltp)
    state["lowest_ltp"] = min(to_float(state.get("lowest_ltp"), option_ltp), option_ltp)

    underlying_quote = read_market_cache(underlying_key) or {}
    received_at = to_float(underlying_quote.get("received_at"), 0)
    quote_age = time_module.time() - received_at if received_at else 999999.0
    spot = to_float(underlying_quote.get("ltp"), 0)
    stale_after = configured_positive_float("GANESH_DATA_STALE_EXIT_SECONDS", 60.0)
    exit_reason = "DAILY_MAX_LOSS" if daily_max_loss_reached() else None
    if not exit_reason and (spot <= 0 or quote_age > stale_after):
        created_at = state.get("created_at")
        try:
            state_age = max((now_ist() - datetime.fromisoformat(created_at)).total_seconds(), 0)
        except Exception:
            state_age = stale_after + 1
        if state_age > stale_after:
            exit_reason = "STALE_MARKET_DATA"
    elif not exit_reason:
        current_time = now_ist()
        candle_start = active_two_hour_start(current_time)
        candle_key = candle_start.isoformat()
        if state.get("monitor_candle_start") != candle_key:
            intraday = _frame_in_ist(
                fetch_v3_intraday_minutes(underlying_key, minutes=1)
            )
            active_rows = intraday[
                (intraday.index.date == current_time.date()) & (intraday.index >= candle_start)
            ]
            if active_rows.empty:
                log("GANESH GAP new two-hour candle open is unavailable; broker stop remains active")
                write_state(state_slot, state)
                return True
            state["monitor_candle_start"] = candle_key
            state["monitor_candle_open"] = float(active_rows.iloc[0]["open"])
            state["exit_confirmation_scans"] = 0
        colour = candle_colour(
            state.get("monitor_candle_open", state.get("entry_candle_open")),
            spot,
            configured_non_negative_float("GANESH_COLOUR_NEUTRAL_BUFFER_POINTS", 0.0),
        )
        state, opposite_confirmed = advance_exit_confirmation(
            state,
            colour,
            state.get("direction"),
            required_scans=max(to_int(os.getenv("GANESH_EXIT_CONFIRMATION_SCANS"), 2), 1),
        )
        state["current_candle_colour"] = colour
        state["last_underlying_ltp"] = round(spot, 2)
        state["last_underlying_quote_at"] = datetime.fromtimestamp(received_at, IST).isoformat()
        if target_reached(
            state.get("direction"), spot, ganesh_gap_state_target(state, "level")
        ):
            exit_reason = "UNDERLYING_TARGET"
        elif is_ganesh_gap_strategy(state):
            exit_reason = ganesh_continuation_structure_exit(state, current_time)
        if not exit_reason and opposite_confirmed:
            exit_reason = "CONFIRMED_OPPOSITE_2H_COLOUR"

    write_state(state_slot, state)
    if verbose:
        log(
            f"GANESH GAP open: {state.get('trading_symbol')} qty={state.get('quantity')} "
            f"option_ltp={option_ltp:.2f} {symbol.lower()}={spot:.2f} "
            f"target={ganesh_gap_state_target(state, 'type')}@"
            f"{ganesh_gap_state_target(state, 'level')} "
            f"colour={state.get('current_candle_colour')}"
        )
    if exit_reason:
        log(f"GANESH GAP exit triggered: {exit_reason}")
        quantity = int(state.get("quantity") or 0)
        if position:
            quantity = quantity or abs(position_quantity(position))
        return submit_ganesh_gap_exit(
            state,
            option_ltp,
            exit_reason,
            quantity,
            state_slot=state_slot,
        )
    return True


def handle_existing_state(symbol, state, verbose=True):
    """Handle one state slot while preventing concurrent selective finalization."""
    fresh = read_state(symbol)
    if not fresh:
        return False
    if is_ganesh_gap_strategy(fresh):
        return _handle_existing_state_locked(symbol, fresh, verbose=verbose)
    with position_finalization_lock(symbol):
        fresh = read_state(symbol)
        if not fresh:
            return False
        return _handle_existing_state_locked(symbol, fresh, verbose=verbose)


def _handle_existing_state_locked(symbol, state, verbose=True):
    if is_ganesh_gap_strategy(state):
        return handle_ganesh_gap_position(state, verbose=verbose, state_slot=symbol)
    instrument_key = state.get("instrument_key")
    if not instrument_key:
        return False

    underlying_symbol = underlying_symbol_for_state_slot(symbol, state)
    entry_transaction = str(state.get("entry_transaction_type") or "BUY").upper()
    exit_transaction = "BUY" if entry_transaction == "SELL" else "SELL"
    order_product = state.get("order_product") or "I"

    needs_broker_stop = broker_protective_stop_required(state)
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
        if state.get("exit_submission_in_progress"):
            return True
        if monitor_pending_exit(symbol, state):
            return True
        state = read_state(symbol)

    entry_order_id = state.get("entry_order_id") or state.get("buy_order_id")
    pending_status = f"{entry_transaction}_PLACED_NOT_COMPLETE"
    is_pending_entry = bool(
        entry_order_id
        and state.get("status") in {pending_status, "BUY_PLACED_NOT_COMPLETE"}
    )
    position = find_matching_position_for_side(instrument_key, entry_transaction)
    if position and not is_pending_entry:
        ltp = position_ltp(position)
        qty = abs(position_quantity(position))
        if needs_broker_stop and not state.get("protective_stop_order_id"):
            try:
                state = ensure_protective_stop(symbol, state)
            except Exception as error:
                log(f"{symbol} CRITICAL: position has no broker stop; flattening now: {error}")
                instrument = {"instrument_key": instrument_key, "trading_symbol": state.get("trading_symbol")}
                state["status"] = "EXIT_PENDING"
                state["exit_reason"] = "PROTECTION_FAILURE"
                state["exit_submission_in_progress"] = True
                write_state(symbol, state)
                try:
                    result, payload = place_market_order(
                        instrument, exit_transaction, qty, product=order_product
                    )
                except Exception:
                    state["status"] = "POSITION_OPEN"
                    state.pop("exit_submission_in_progress", None)
                    write_state(symbol, state)
                    raise
                order_id = result.get("data", {}).get("order_id")
                if not order_id:
                    raise RuntimeError(
                        f"{symbol} emergency {exit_transaction} returned no order_id: {result}"
                    )
                details = wait_for_order_complete(order_id)
                if order_is_complete(details):
                    complete_exit(
                        symbol,
                        state,
                        details,
                        ltp,
                        "PROTECTION_FAILURE",
                        result,
                        payload,
                    )
                else:
                    state.pop("exit_submission_in_progress", None)
                    state["exit_order_id"] = order_id
                    state["exit_fallback_price"] = ltp
                    write_state(symbol, state)
                    log(
                        f"{symbol} emergency {exit_transaction} pending: "
                        f"order_id={order_id} status={order_status(details)}"
                    )
                return True
        if ltp is not None:
            last_tick = to_float(state.get("last_processed_stream_tick_at"), 0)
            recent_ticks = read_recent_ticks(instrument_key, since_epoch=last_tick)
            for tick in recent_ticks:
                tick_ltp = to_float(tick.get("ltp"), 0)
                if tick_ltp > 0:
                    state = apply_trailing_stop(symbol, state, tick_ltp)
                state["last_processed_stream_tick_at"] = max(
                    to_float(state.get("last_processed_stream_tick_at"), 0),
                    to_float(tick.get("received_at"), 0),
                )
            if recent_ticks:
                write_state(symbol, state)
            state = apply_trailing_stop(symbol, state, ltp)
            if broker_protective_stop_required(state):
                try:
                    state = synchronize_broker_protective_stop(symbol, state)
                except Exception as error:
                    log(f"{symbol} protective stop synchronization pending: {error}")
        target_price = float(state.get("target_price"))
        stop_loss_price = float(state.get("stop_loss_price"))
        is_short = entry_transaction == "SELL"
        booking_price = profit_booking_price(state)
        if to_float(state.get("profit_booking_price")) != booking_price:
            state["profit_booking_percent"] = profit_booking_percent_for_state(state)
            state["profit_booking_mode"] = profit_booking_mode_for_state(state)
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
        if sentiment_exit_enabled_for_state(state) and sentiment_check_due(state):
            state["last_sentiment_check_at"] = now_ist().isoformat()
            write_state(symbol, state)
            sentiment_exit, sentiment_reason = should_exit_on_sentiment_change(
                underlying_symbol,
                state,
                ltp,
            )
        thesis_exit_reason = None
        thesis_exit_detail = ""
        underlying_key = state.get("underlying_instrument_key") or UNDERLYING_INDEX_KEYS.get(
            underlying_symbol
        )
        if state.get("instrument_class") == "INDEX_OPTION" and underlying_key:
            underlying_quote = read_market_cache(underlying_key) or {}
            quote_age = time_module.time() - to_float(underlying_quote.get("received_at"), 0)
            maximum_age = configured_positive_float("UNDERLYING_QUOTE_MAX_AGE_SECONDS", 20.0)
            underlying_ltp = (
                to_float(underlying_quote.get("ltp"), 0)
                if 0 <= quote_age <= maximum_age
                else 0
            )
            thesis_exit_reason = underlying_exit_reason(
                state,
                underlying_ltp,
                minutes_since_created(state),
                structural_enabled=configured_bool("UNDERLYING_STRUCTURAL_STOP_ENABLED", True),
                time_stop_enabled=configured_bool("INDEX_TIME_STOP_ENABLED", True),
                time_stop_minutes=configured_positive_float("INDEX_TIME_STOP_MINUTES", 20.0),
                minimum_progress_percent=configured_non_negative_float(
                    "INDEX_TIME_STOP_MIN_PROGRESS_PERCENT", 15.0
                ),
            )
            if thesis_exit_reason:
                thesis_exit_detail = (
                    f"underlying_ltp={underlying_ltp:.2f}, "
                    f"structural_stop={state.get('underlying_structural_stop')}, "
                    f"age={minutes_since_created(state):.1f}m"
                )
        target_hit = ltp is not None and (ltp <= booking_price if is_short else ltp >= booking_price)
        stop_hit = ltp is not None and (ltp >= stop_loss_price if is_short else ltp <= stop_loss_price)
        if ltp is not None and (target_hit or stop_hit or sentiment_exit or thesis_exit_reason):
            # Stock-future and short-option stops are already protected at the
            # broker. Do not send a second market exit when the local LTP also
            # reaches the stop; let the broker stop fill and confirm it here.
            broker_stop = to_float(state.get("broker_protective_stop_price"), 0)
            broker_covers_local_stop = (
                broker_stop <= stop_loss_price + 0.05
                if is_short
                else broker_stop >= stop_loss_price - 0.05
            )
            if (
                stop_hit and needs_broker_stop
                and state.get("protective_stop_order_id")
                and broker_covers_local_stop
            ):
                log(
                    f"{symbol} local stop reached; waiting for broker protective stop "
                    f"order_id={state['protective_stop_order_id']}"
                )
                return True

            if target_hit:
                exit_reason = "TARGET"
            elif stop_hit:
                exit_reason = "STOP_LOSS"
            elif thesis_exit_reason:
                exit_reason = thesis_exit_reason
                log(f"{symbol} {exit_reason} triggered: {thesis_exit_detail}")
            elif sentiment_exit:
                exit_reason = "SENTIMENT_EXIT"
                log(f"{symbol} sentiment exit triggered: {sentiment_reason}")

            # Publish the exit state before making broker calls. The separate
            # entry and monitor cron jobs can overlap, so this prevents both
            # processes from submitting the same exit order.
            state["status"] = "EXIT_PENDING"
            state["exit_reason"] = exit_reason
            write_state(symbol, state)

            if needs_broker_stop and cancel_protective_stop(symbol, state):
                return True

            instrument = {"instrument_key": instrument_key, "trading_symbol": state.get("trading_symbol")}
            result, payload = place_market_order(
                instrument, exit_transaction, qty, product=order_product
            )
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
                exit_profile=state.get("exit_profile", {}),
                profit_protection_enabled_for_trade=state.get("profit_protection_enabled_for_trade", True),
            )
            state = read_state(symbol)
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
            post_fill = make_post_fill_diagnostic_only(post_fill)
            if post_fill.get("feasibility", {}).get("post_fill_diagnostic_rejected"):
                log(
                    f"{symbol} delayed-fill post-fill diagnostic did not pass; "
                    "position retained because post-fill checks are observation-only"
                )
            state["post_fill_feasibility"] = post_fill["feasibility"]
            state.update(
                {
                    "target_price": post_fill["target_price"],
                    "stop_loss_price": post_fill["stop_loss_price"],
                    "original_stop_loss_price": post_fill["stop_loss_price"],
                    "technical_context": post_fill["technicals"],
                    "exit_profile": state.get("exit_profile", {}),
                    "profit_protection_enabled_for_trade": state.get("profit_protection_enabled_for_trade", True),
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
                    result, payload = place_market_order(
                        instrument, exit_transaction, quantity, product=order_product
                    )
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


def profit_booking_mode():
    mode = os.getenv("PROFIT_BOOKING_MODE", "exit").strip().lower()
    if mode not in {"exit", "runner"}:
        raise RuntimeError("PROFIT_BOOKING_MODE must be 'exit' or 'runner'")
    return mode


def profit_booking_percent_for_state(state):
    return profit_booking_target_percent()


def profit_booking_mode_for_state(state):
    return profit_booking_mode()


def env_bool_with_fallback(primary, fallback, default="true"):
    raw = os.getenv(primary)
    if raw is None:
        raw = os.getenv(fallback, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}



def profit_protection_settings(instrument_class=None):
    """Return validated, staged profit-protection thresholds for index options."""
    settings = {
        "enabled": configured_bool("PROFIT_PROTECTION_ENABLED", True),
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
        "runner_lock": configured_non_negative_float(
            "PROFIT_RUNNER_LOCK_PERCENT", 55.0
        ),
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
    if profit_booking_mode() == "runner" and not (
        settings["stage_two_lock"] <= settings["runner_lock"]
        < settings["booking_trigger"]
    ):
        raise RuntimeError(
            "Runner lock must satisfy stage-two lock <= runner lock < booking trigger"
        )
    return settings

def profit_booking_price(state):
    """Return the premium that represents the configured share of target progress."""
    entry_price = float(state.get("entry_price") or 0)
    target_price = float(state.get("planned_target_price") or state.get("target_price") or 0)
    is_short = str(state.get("entry_transaction_type") or "BUY").upper() == "SELL"
    if entry_price <= 0 or (target_price >= entry_price if is_short else target_price <= entry_price):
        return target_price

    if profit_booking_mode_for_state(state) == "runner":
        return round(target_price, 2)

    progress = profit_booking_percent_for_state(state) / 100.0
    booking_price = entry_price + (target_price - entry_price) * progress
    precision = 2 if state.get("instrument_class") == "STOCK_FUTURE" else 0
    return round(booking_price, precision)


def apply_trailing_stop(symbol, state, ltp):
    """Apply staged profit locks; runner mode protects gains through full target.

    The original stop remains untouched below 60% target progress. At 60%
    progress the stop protects 20% of the planned move, and at 70% progress it
    protects 35%. The stop never moves backwards.
    """
    if ltp is None or state.get("instrument_class") != "INDEX_OPTION":
        return state

    entry = to_float(state.get("entry_price"))
    current_ltp = to_float(ltp)
    if not entry or not current_ltp:
        return state
    is_short = str(state.get("entry_transaction_type") or "BUY").upper() == "SELL"
    if is_short:
        state["lowest_ltp"] = round(
            min(to_float(state.get("lowest_ltp"), entry), current_ltp), 2
        )
        state["highest_ltp"] = round(
            max(to_float(state.get("highest_ltp"), entry), current_ltp), 2
        )
    else:
        state["highest_ltp"] = round(
            max(to_float(state.get("highest_ltp"), entry), current_ltp), 2
        )
        state["lowest_ltp"] = round(
            min(to_float(state.get("lowest_ltp"), entry), current_ltp), 2
        )

    settings = profit_protection_settings()
    if not settings["enabled"]:
        return state
    if state.get("profit_protection_enabled_for_trade") is False:
        return state

    target = to_float(state.get("planned_target_price") or state.get("target_price"))
    current_stop = to_float(state.get("stop_loss_price"))
    if not entry or not target or not current_stop or not current_ltp:
        return state

    planned_move = entry - target if is_short else target - entry
    favorable_move = entry - current_ltp if is_short else current_ltp - entry
    if planned_move <= 0:
        return state

    progress = max(favorable_move / planned_move * 100.0, 0.0)
    current_stage = int(to_float(state.get("profit_protection_stage"), 0) or 0)
    new_stage = current_stage
    lock_percent = None
    if (
        profit_booking_mode() == "runner"
        and progress >= settings["booking_trigger"]
        and current_stage < 3
    ):
        new_stage = 3
        lock_percent = settings["runner_lock"]
    elif progress >= settings["stage_two_trigger"] and current_stage < 2:
        new_stage = 2
        lock_percent = settings["stage_two_lock"]
    elif progress >= settings["stage_one_trigger"] and current_stage < 1:
        new_stage = 1
        lock_percent = settings["stage_one_lock"]

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
        state.setdefault("profit_protection_activated_at", now_ist().isoformat())
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
    if state.get("instrument_class") == "STOCK_FUTURE":
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
    if state.get("instrument_class") == "STOCK_FUTURE":
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

        if state.get("paper_trade"):
            quote = read_market_cache(state["instrument_key"]) or {}
            ltp = to_float(quote.get("ltp"), to_float(state.get("entry_price")))
            if is_ganesh_gap_strategy(state):
                close_ganesh_gap_paper_position(
                    state, ltp, "SQUAREOFF", state_slot=symbol
                )
            else:
                journal_row = record_closed_trade(state, ltp, "SQUAREOFF")
                log(
                    f"{symbol} paper bought {journal_row.get('entry_price')} "
                    f"closed {journal_row.get('exit_price')} "
                    f"pnl {journal_row.get('gross_pnl')} reason SQUAREOFF"
                )
                clear_state(symbol)
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
        verbose_log(
            f"{symbol} institutional footprint: bias={footprint.get('bias')} "
            f"confidence={footprint.get('confidence')} score={footprint.get('score')} "
            f"reasons={footprint.get('reasons')}"
        )
        return footprint
    except Exception as error:
        log(f"{symbol} institutional footprint unavailable: {error}")
        return neutral_institutional_footprint(str(error))


def build_trade_candidate(
    symbol,
    rec,
    base_technicals,
    institutional,
    option_trend,
    transaction_type,
    contract_row=None,
):
    if str(transaction_type).upper() != "BUY":
        return {
            "allowed": False,
            "reason": "Only long option buying is enabled.",
            "transaction_type": "BUY",
        }, None
    direction = rec["direction"]
    atm = dict(contract_row or rec["atm"])
    analysis_atm = dict(rec.get("analysis_atm") or atm)
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
    analysis_instrument = find_index_option_instrument(
        symbol,
        analysis_atm["expiry"],
        analysis_atm["strike"],
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
    preferred_delta_min = configured_non_negative_float("PREFERRED_OPTION_DELTA_MIN", 0.45)
    preferred_delta_max = configured_non_negative_float("PREFERRED_OPTION_DELTA_MAX", 0.70)
    absolute_delta = abs(to_float(option_quality.get("delta"), 0))
    contract_rank = 0.0
    if preferred_delta_min <= absolute_delta <= preferred_delta_max:
        contract_rank += 5.0
    spread_percent = option_quality.get("spread_percent")
    if spread_percent is not None:
        contract_rank += max(0.0, 3.0 - float(spread_percent))
    contract_volume = to_float(atm.get(f"{option_type}_volume"), 0)
    contract_rank += min(contract_volume / 100000.0, 2.0)
    option_quality["selection_rank"] = round(contract_rank, 2)
    option_quality["preferred_delta_range"] = [preferred_delta_min, preferred_delta_max]

    # The stream is dynamic because the ATM strike changes. The persistent
    # service will subscribe to the next set on its next refresh/restart.
    write_stream_instruments([
        "NSE_INDEX|Nifty 50",
        "NSE_INDEX|Nifty Bank",
        "NSE_INDEX|India VIX",
        analysis_instrument.get("instrument_key"),
        instrument.get("instrument_key"),
    ])
    analysis_raw_flow = get_option_volume_vwap_analysis(
        analysis_instrument["instrument_key"],
        side_label=analysis_instrument["trading_symbol"],
    )
    if analysis_instrument["instrument_key"] == instrument["instrument_key"]:
        execution_raw_flow = analysis_raw_flow
    else:
        execution_raw_flow = get_option_volume_vwap_analysis(
            instrument["instrument_key"],
            side_label=instrument["trading_symbol"],
        )
    raw_flow = analysis_raw_flow
    analysis_flow = normalize_option_flow_for_position(
        analysis_raw_flow,
        transaction_type,
    )
    execution_flow = normalize_option_flow_for_position(
        execution_raw_flow,
        transaction_type,
    )
    technicals = deepcopy(base_technicals)
    technicals["raw_atm_option_flow"] = analysis_raw_flow
    technicals["atm_option_flow"] = analysis_flow
    technicals["raw_execution_atm_option_flow"] = execution_raw_flow
    technicals["execution_atm_option_flow"] = execution_flow
    technicals["option_expiry_context"] = {
        "analysis_expiry": str(analysis_atm.get("expiry")),
        "analysis_strike": analysis_atm.get("strike"),
        "analysis_trading_symbol": analysis_instrument.get("trading_symbol"),
        "execution_expiry": str(atm.get("expiry")),
        "execution_strike": atm.get("strike"),
        "execution_trading_symbol": instrument.get("trading_symbol"),
    }
    technicals["institutional_flow"] = institutional
    technicals["option_market_quality"] = option_quality
    technicals["market_regime"] = classify_market_regime(
        technicals,
        compression_width_percent=configured_positive_float(
            "REGIME_COMPRESSION_BB_WIDTH_PERCENT", 0.18
        ),
        extreme_atr_percent=configured_positive_float(
            f"{symbol}_REGIME_EXTREME_ATR_PERCENT",
            0.35 if symbol == "NIFTY" else 0.45,
        ),
    )
    technicals["entry_structure"] = entry_structure_for_direction(
        technicals,
        direction,
        retest_buffer_atr=configured_non_negative_float(
            "ENTRY_RETEST_BUFFER_ATR", 0.25
        ),
    )

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
        "analysis_strike": analysis_atm.get("strike"),
        "analysis_expiry": analysis_atm.get("expiry"),
        "analysis_trading_symbol": analysis_instrument.get("trading_symbol"),
        "execution_strike": atm.get("strike"),
        "execution_expiry": atm.get("expiry"),
        "entry_price": round(entry_price, 2),
        "reasons": rec.get("reasons", []),
        "trade_action": f"{transaction_type}_OPTION",
        "transaction_type": transaction_type,
        "option_type": option_type,
        "trading_symbol": instrument["trading_symbol"],
        "option_chain_trend": option_trend,
        "option_market_quality": option_quality,
        "strategy": rec.get("strategy", "TREND_FOLLOWING"),
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

    live_gate = live_entry_gate(
        direction,
        technicals,
        float(weighted.get("score") or 0),
        enabled=True,
        range_minimum_score=85.0,
        continuation_minimum_score=85.0,
    )
    technicals["live_entry_gate"] = live_gate
    option_summary["live_entry_gate"] = live_gate
    expiry_days = (parse_expiry(atm["expiry"]) - now_ist().date()).days
    technicals["market_regime"]["days_to_expiry"] = expiry_days

    cautious = weighted.get("grade") == "CAUTIOUS_TRADE"
    if option_summary.get("strategy") == "BOLLINGER_REVERSAL":
        minimum_target = 20.0 if symbol == "NIFTY" else 40.0
        exit_settings = {
            "target_points": max(
                minimum_target,
                configured_positive_float(
                    f"{symbol}_BOLLINGER_REVERSAL_TARGET_POINTS",
                    minimum_target,
                ),
            ),
            "stop_points": configured_positive_float(
                f"{symbol}_BOLLINGER_REVERSAL_STOP_POINTS",
                minimum_target,
            ),
            "delta": configured_positive_float(
                "OPTION_DELTA_APPROXIMATION",
                DEFAULT_OPTION_DELTA_APPROXIMATION,
            ),
            "profile": "BOLLINGER_REVERSAL",
        }
    else:
        exit_settings = score_based_exit_settings(
            symbol,
            float(weighted.get("score") or 0),
        )
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
            "target_profile": exit_settings["profile"],
        }
    )
    feasibility = evaluate_trade_feasibility(
        direction,
        entry_price,
        target,
        stop,
        technicals,
        transaction_type=transaction_type,
        symbol=symbol,
    )
    technicals["trade_feasibility"] = feasibility
    option_summary["trade_feasibility"] = feasibility

    entry_score = unified_entry_score(
        weighted,
        technicals,
        institutional,
        direction,
        feasibility=feasibility,
        days_to_expiry=expiry_days,
        live_gate=live_gate,
    )
    option_summary["unified_entry_score"] = entry_score
    technicals["unified_entry_score"] = entry_score
    verbose_log(
        f"{symbol} unified entry score: score={entry_score.get('score')} "
        f"version={entry_score.get('score_version')} "
        f"components={entry_score.get('components')}"
    )
    score_value = float(entry_score.get("score") or 0)
    score_rule = vamsi_score_rule(symbol)
    minimum = float(score_rule["min_score"])
    maximum = score_rule.get("max_score")
    score_approved = vamsi_entry_score_qualifies(score_value, symbol)
    cutoff_approved = score_approved
    watch_band = bool(
        not score_approved
        and watch_mode_enabled()
        and score_value >= watch_minimum_score()
        and score_value < minimum
        and (live_gate.get("watch_eligible") or technicals["entry_structure"].get("watch_eligible"))
    )
    if not score_approved and not watch_band:
        return {
            "allowed": False,
            "reason": (
                f"unified entry score {score_value:.1f} does not satisfy Vamsi "
                f"{format_vamsi_score_rule(score_rule)}"
            ),
            "transaction_type": transaction_type,
            "instrument": instrument,
            "technicals": technicals,
            "option_summary": option_summary,
            "weighted": weighted,
            "entry_score": entry_score,
        }, None
    structural = structural_invalidation(
        technicals,
        direction,
        atr_buffer=configured_non_negative_float("STRUCTURAL_STOP_ATR_BUFFER", 0.20),
    )
    technicals["structural_invalidation"] = structural

    if watch_band:
        watch_reason = (
            f"watch band unified score {score_value:.1f}; direct entry requires "
            f"{format_vamsi_score_rule(score_rule)}"
        )
        return {
            "allowed": False,
            "watch_eligible": True,
            "timing_watch": True,
            "reason": watch_reason,
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
            "exit_profile": {},
            "profit_protection_enabled_for_trade": True,
            "technicals": technicals,
            "option_summary": option_summary,
            "weighted": weighted,
            "entry_score": entry_score,
            "entry_minimum_score": minimum,
            "entry_maximum_score": maximum,
            "score_rule_source": score_rule.get("source"),
            "score_cutoff_approved": False,
            "target_profile": exit_settings["profile"],
            "contract_selection_rank": option_quality.get("selection_rank", 0.0),
            "structural_invalidation": structural,
        }, None

    return {
        "allowed": True,
        "reason": (
            f"Vamsi unified entry score approved at {score_value:.1f} using "
            f"{format_vamsi_score_rule(score_rule)}; "
            f"{exit_settings['profile'].lower()} target/stop "
            f"{levels['target_points']:.0f}/{levels['stop_points']:.0f} points"
        ),
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
        "exit_profile": {},
        "profit_protection_enabled_for_trade": True,
        "technicals": technicals,
        "option_summary": option_summary,
        "weighted": weighted,
        "entry_score": entry_score,
        "entry_minimum_score": minimum,
        "entry_maximum_score": maximum,
        "score_rule_source": score_rule.get("source"),
        "score_cutoff_approved": cutoff_approved,
        "target_profile": exit_settings["profile"],
        "contract_selection_rank": option_quality.get("selection_rank", 0.0),
        "structural_invalidation": structural,
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
        key=lambda item: (
            candidate_weighted_score(item),
            float(item.get("contract_selection_rank") or 0),
        ),
        default=None,
    )


def evaluate_symbol_buy_or_sell(
    symbol,
    allow_option_sell=False,
    include_rejected=False,
    excluded_instrument_keys=None,
):
    excluded_instrument_keys = set(excluded_instrument_keys or [])
    rec = get_index_recommendation(symbol)
    record_option_chain_snapshot(symbol, rec)
    direction = rec["direction"]
    confidence = rec["confidence"]
    score = rec["score"]
    atm = rec["atm"]
    analysis_atm = rec.get("analysis_atm") or atm
    verbose_log(
        f"{symbol} signal: {direction}, confidence={confidence}, score={score}, "
        f"analysis_strike={analysis_atm['strike']}, analysis_expiry={analysis_atm['expiry']}, "
        f"execution_strike={atm['strike']}, execution_expiry={atm['expiry']}"
    )
    # Chain strength now contributes up to 25 points instead of acting as a
    # separate veto. Any directional chain can therefore proceed to the full
    # 100-point assessment; weak evidence simply earns fewer points.
    directional_chain = direction in {"BULLISH", "BEARISH"}
    observe_signal_reset(symbol, direction)
    neutral_banknifty_candidate = symbol == "BANKNIFTY" and direction == "NEUTRAL"
    neutral_nifty_candidate = symbol == "NIFTY" and direction == "NEUTRAL"
    if not directional_chain and not neutral_banknifty_candidate and not neutral_nifty_candidate:
        collect_institutional_footprint(symbol, rec)
        log_scan_decision(symbol, score, "reject")
        return False

    try:
        base_technicals = get_technical_analysis(symbol)
    except Exception as error:
        base_technicals = {
            "two_hour": {"bias": "NEUTRAL", "confidence": "LOW", "reasons": [str(error)]},
            "fifteen_min": {"bias": "NEUTRAL", "confidence": "LOW", "reasons": [str(error)]},
            "five_min": {"bias": "NEUTRAL", "confidence": "LOW", "reasons": [str(error)]},
        }
        verbose_log(f"{symbol} technical analysis failed: {error}")

    ensure_instruments_file()
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
        verbose_log(
            f"BANKNIFTY breadth: bias={breadth.get('bias')} "
            f"confidence={breadth.get('confidence')} score={breadth.get('score')} "
            f"reasons={breadth.get('reasons')}"
        )
    elif symbol == "NIFTY":
        try:
            breadth = get_nifty_breadth(INSTRUMENT_CACHE, upstox_request)
        except Exception as error:
            breadth = {
                "bias": "NEUTRAL",
                "confidence": "LOW",
                "score": 0,
                "reasons": [f"NIFTY breadth unavailable: {error}"],
            }
        base_technicals["nifty_breadth"] = breadth
        verbose_log(
            f"NIFTY breadth: bias={breadth.get('bias')} "
            f"confidence={breadth.get('confidence')} score={breadth.get('score')} "
            f"reasons={breadth.get('reasons')}"
        )

    reversal = bollinger_exhaustion_reversal(
        base_technicals,
        min_extension_fraction=configured_non_negative_float(
            "BOLLINGER_REVERSAL_MIN_EXTENSION_FRACTION", 0.10
        ),
        min_extension_atr=configured_non_negative_float(
            "BOLLINGER_REVERSAL_MIN_EXTENSION_ATR", 0.20
        ),
        min_rejection_wick_fraction=configured_non_negative_float(
            "BOLLINGER_REVERSAL_MIN_WICK_FRACTION", 0.25
        ),
        min_five_minute_momentum=configured_non_negative_float(
            "BOLLINGER_REVERSAL_MIN_5M_MOMENTUM", 2.0
        ),
    )
    base_technicals["bollinger_reversal"] = reversal
    reversal_enabled = configured_bool("BOLLINGER_REVERSAL_ENABLED", True)
    if reversal_enabled and reversal.get("confirmed"):
        verbose_log(
            f"{symbol} Bollinger reversal confirmed: "
            f"direction={reversal.get('direction')} "
            f"extension_multiple={reversal.get('extension_multiple')} "
            f"reasons={reversal.get('reasons')}"
        )

    if neutral_banknifty_candidate:
        if score_cutoff_mode_enabled():
            inferred_direction = score_direction_from_technicals(base_technicals)
            blockers = [] if inferred_direction else ["technical direction is unavailable"]
        else:
            inferred_direction, blockers = banknifty_neutral_chain_direction(base_technicals)
        if (
            not inferred_direction
            and reversal_enabled
            and reversal.get("confirmed")
        ):
            inferred_direction = reversal.get("direction")
            blockers = []
        if not inferred_direction:
            collect_institutional_footprint(symbol, rec)
            log_scan_decision(symbol, score, "reject")
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
        verbose_log(
            f"BANKNIFTY neutral-chain override candidate: direction={direction}; "
            "the complete 100-point score will decide"
        )
    elif neutral_nifty_candidate:
        if score_cutoff_mode_enabled():
            inferred_direction = score_direction_from_technicals(base_technicals)
            blockers = [] if inferred_direction else ["technical direction is unavailable"]
        else:
            inferred_direction, blockers = nifty_neutral_chain_direction(base_technicals)
        if (
            not inferred_direction
            and reversal_enabled
            and reversal.get("confirmed")
        ):
            inferred_direction = reversal.get("direction")
            blockers = []
        if not inferred_direction:
            collect_institutional_footprint(symbol, rec)
            log_scan_decision(symbol, score, "reject")
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
        verbose_log(
            f"NIFTY neutral-chain override candidate: direction={direction}; "
            "the complete 100-point score will decide"
        )

    observe_signal_reset(symbol, direction)
    blocked_reason = reentry_block_reason(symbol, direction)
    if blocked_reason:
        log_scan_decision(symbol, score, "reject")
        verbose_log(f"{symbol} no trade: {blocked_reason}")
        return False

    if (
        reversal_enabled
        and reversal.get("confirmed")
        and reversal.get("direction") == direction
    ):
        rec = deepcopy(rec)
        rec["strategy"] = "BOLLINGER_REVERSAL"

    institutional = collect_institutional_footprint(symbol, rec)
    option_trend = get_option_chain_trend(
        symbol,
        direction,
        expiry=rec.get("analysis_expiry") or analysis_atm.get("expiry"),
    )
    candidates = []
    contract_rows = index_contract_rows(rec, direction)
    for contract_row in contract_rows:
        try:
            candidate, _ = build_trade_candidate(
                symbol,
                rec,
                base_technicals,
                institutional,
                option_trend,
                "BUY",
                contract_row=contract_row,
            )
            if candidate:
                instrument_key = (candidate.get("instrument") or {}).get("instrument_key")
                if instrument_key in excluded_instrument_keys:
                    verbose_log(
                        f"{symbol} contract skipped: {instrument_key} is already "
                        "managed by the other index lane"
                    )
                    continue
                candidate.update(
                    {
                        "symbol": symbol,
                        "direction": direction,
                        "confidence": confidence,
                        "signal_score": candidate_weighted_score(candidate),
                        "chain_signal_score": score,
                    }
                )
                candidates.append(candidate)
                verbose_log(
                    f"{symbol} BUY candidate: allowed={candidate.get('allowed')} "
                    f"score={candidate_weighted_score(candidate)} "
                    f"version={candidate_score_version(candidate) or 'LEGACY'} "
                    f"reason={candidate.get('reason')} "
                    f"contract={candidate.get('instrument', {}).get('trading_symbol')} "
                    f"contract_rank={candidate.get('contract_selection_rank', 0)}"
                )
        except Exception as error:
            verbose_log(
                f"{symbol} BUY candidate unavailable for strike "
                f"{contract_row.get('strike')}: {error}"
            )

    reversal_direction = reversal.get("direction")
    if (
        reversal_enabled
        and reversal.get("confirmed")
        and reversal_direction in {"BULLISH", "BEARISH"}
        and reversal_direction != direction
    ):
        reversal_block = reentry_block_reason(symbol, reversal_direction)
        if reversal_block:
            verbose_log(f"{symbol} Bollinger reversal blocked: {reversal_block}")
        else:
            reversal_rec = deepcopy(rec)
            reversal_rec.update(
                {
                    "chain_bias": rec.get("chain_bias", direction),
                    "chain_confidence": rec.get("chain_confidence", confidence),
                    "direction": reversal_direction,
                    "strategy": "BOLLINGER_REVERSAL",
                }
            )
            reversal_trend = get_option_chain_trend(
                symbol,
                reversal_direction,
                expiry=rec.get("analysis_expiry") or analysis_atm.get("expiry"),
            )
            for contract_row in index_contract_rows(reversal_rec, reversal_direction):
                try:
                    candidate, _ = build_trade_candidate(
                        symbol,
                        reversal_rec,
                        base_technicals,
                        institutional,
                        reversal_trend,
                        "BUY",
                        contract_row=contract_row,
                    )
                    if candidate:
                        instrument_key = (candidate.get("instrument") or {}).get("instrument_key")
                        if instrument_key in excluded_instrument_keys:
                            verbose_log(
                                f"{symbol} reversal contract skipped: {instrument_key} "
                                "is already managed by the other index lane"
                            )
                            continue
                        candidate.update(
                            {
                                "symbol": symbol,
                                "direction": reversal_direction,
                                "confidence": "MEDIUM",
                                "signal_score": candidate_weighted_score(candidate),
                                "chain_signal_score": score,
                            }
                        )
                        candidates.append(candidate)
                        verbose_log(
                            f"{symbol} BOLLINGER_REVERSAL candidate: "
                            f"allowed={candidate.get('allowed')} "
                            f"direction={reversal_direction} "
                            f"score={candidate_weighted_score(candidate)} "
                            f"version={candidate_score_version(candidate) or 'LEGACY'} "
                            f"reason={candidate.get('reason')} "
                            f"contract={candidate.get('instrument', {}).get('trading_symbol')}"
                        )
                except Exception as error:
                    verbose_log(
                        f"{symbol} BOLLINGER_REVERSAL candidate unavailable for strike "
                        f"{contract_row.get('strike')}: {error}"
                    )

    preferred = select_trade_candidate(candidates, allow_sell=allow_option_sell)
    if not preferred:
        eligible = [
            item for item in candidates
            if allow_option_sell or item.get("transaction_type") != "SELL"
        ]
        best = max(eligible, key=candidate_weighted_score, default=None)
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
        reject_score = candidate_weighted_score(best) if best else score
        log_scan_decision(
            symbol,
            reject_score,
            "reject",
            score_version=candidate_score_version(best) if best else None,
        )
        verbose_log(f"{symbol} no trade: unified entry score did not qualify.")
        if include_rejected and best:
            return best
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
        chosen_direction = chosen["direction"]
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
            "decision": chosen_direction,
            "confidence": (
                "HIGH"
                if chosen.get("entry_score", {}).get("grade") == "TRADE"
                else "MEDIUM"
            ),
            "target_price": chosen["target_price"],
            "stop_loss_price": chosen["stop_loss_price"],
            "reason": (
                "Approved by the unified signal-and-gate entry score; hard execution "
                "safeguards remain satisfied."
            ),
        }
        record_analysis(symbol, option_summary, technicals, decision)

        chosen.update(
            {
                "symbol": symbol,
                "direction": chosen_direction,
                "confidence": confidence,
                "signal_score": candidate_weighted_score(chosen),
                "chain_signal_score": score,
                "decision": decision,
            }
        )
        return chosen

    log_scan_decision(
        symbol,
        candidate_weighted_score(preferred),
        "reject",
        score_version=candidate_score_version(preferred),
    )
    verbose_log(f"{symbol} no trade: qualified score could not fund an executable order.")
    return False


def execute_selected_candidate(chosen):
    """Serialize entry submission and fill finalization against the monitor."""
    with position_finalization_lock(chosen["symbol"]):
        return _execute_selected_candidate_locked(chosen)


def _execute_selected_candidate_locked(chosen):
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
        capital_override=chosen.get("capital_override"),
    )
    if quantity <= 0:
        log(
            f"{symbol} no trade: configured option capital/risk budget is "
            "insufficient for one whole lot."
        )
        return False
    planned_risk = proposed_position_risk(
        entry_price,
        stop,
        quantity,
        transaction_type,
    )
    trade_context = planned_trade_context(symbol)
    structural = chosen.get("structural_invalidation") or {}
    entry_score = chosen.get("entry_score") or {}
    trade_metadata = {
        **trade_context,
        "planned_risk": round(planned_risk, 2),
        "score_cutoff_approved": bool(chosen.get("score_cutoff_approved")),
        "entry_minimum_score": to_float(chosen.get("entry_minimum_score")),
        "entry_maximum_score": chosen.get("entry_maximum_score"),
        "score_rule_source": chosen.get("score_rule_source"),
        "entry_score_version": entry_score.get("score_version"),
        "entry_score_components": entry_score.get("components", {}),
        "base_alignment_score": (chosen.get("weighted") or {}).get("score"),
        "manual_override": bool(chosen.get("manual_override")),
        "manual_command": chosen.get("manual_command", ""),
        "underlying_instrument_key": UNDERLYING_INDEX_KEYS.get(symbol),
        "underlying_entry_price": structural.get("entry_underlying"),
        "underlying_structural_stop": structural.get("stop_underlying"),
        "underlying_structural_reference": structural.get("reference"),
        "underlying_structural_reference_value": structural.get("reference_value"),
        "underlying_atr": structural.get("atr"),
        "market_regime": (chosen.get("technicals", {}).get("market_regime") or {}).get("regime"),
        "entry_structure": (chosen.get("technicals", {}).get("entry_structure") or {}).get("type"),
    }

    live = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
    verbose_log(
        f"{symbol} selected {transaction_type}: {instrument['trading_symbol']} qty={quantity} "
        f"entry={entry_price} target={target} stop={stop} "
        f"trailing={chosen.get('profit_protection_enabled_for_trade', True)} live={live}"
    )
    if not live:
        dry_run_note = " (dry run)"
    else:
        dry_run_note = ""

    write_stream_instruments([
        "NSE_INDEX|Nifty 50",
        "NSE_INDEX|Nifty Bank",
        "NSE_INDEX|India VIX",
        instrument.get("instrument_key"),
    ])

    with portfolio_entry_lock():
        portfolio_decision = pre_order_portfolio_decision(
            chosen,
            quantity,
            entry_price,
            stop,
        )
        if not portfolio_decision.get("allowed"):
            log_scan_decision(
                symbol,
                candidate_weighted_score(chosen),
                "reject",
                score_version=candidate_score_version(chosen),
            )
            verbose_log(
                f"{symbol} portfolio gate rejected entry: "
                f"{portfolio_decision.get('reason')}"
            )
            return False
        risk = portfolio_decision.get("risk", {})
        verbose_log(
            f"{symbol} portfolio gate accepted{dry_run_note}: "
            f"current_risk={risk.get('current_risk')} proposed_risk={risk.get('proposed_risk')} "
            f"projected_risk={risk.get('projected_risk')} limit={risk.get('risk_limit')}"
        )
        if not live:
            log(f"{symbol} DRY RUN ONLY: would {transaction_type} configured quantity.")
            return True
        result, payload = place_market_order(instrument, transaction_type, quantity)
        order_id = result.get("data", {}).get("order_id")
        if not order_id:
            raise RuntimeError(f"{symbol} {transaction_type} returned no order_id: {result}")
        # Reserve this risk slot before releasing the portfolio lock.
        write_state(symbol, {
            "date": now_ist().strftime("%Y-%m-%d"),
            "symbol": symbol,
            "entry_order_id": order_id,
            "entry_transaction_type": transaction_type,
            "instrument_class": "INDEX_OPTION",
            "order_product": "I",
            "instrument_key": instrument["instrument_key"],
            "trading_symbol": instrument["trading_symbol"],
            "quantity": int(quantity),
            "lot_size": int(instrument["lot_size"]),
            "entry_price": entry_price,
            "target_price": target,
            "stop_loss_price": stop,
            "target_points": chosen["target_points"],
            "stop_points": chosen["stop_points"],
            "option_delta_used": chosen["option_delta_used"],
            "exit_profile": chosen.get("exit_profile", {}),
            "profit_protection_enabled_for_trade": chosen.get("profit_protection_enabled_for_trade", True),
            "direction": direction,
            "confidence": confidence,
            "score": score,
            "weighted_score": candidate_weighted_score(chosen),
            **trade_metadata,
            "status": f"{transaction_type}_PLACED_NOT_COMPLETE",
            "created_at": now_ist().isoformat(),
        })
    verbose_log(f"{symbol} MARKET {transaction_type} placed: order_id={order_id} payload={payload}")
    details = wait_for_order_complete(order_id)
    if order_is_rejected(details):
        clear_state(symbol)
        log_scan_decision(
            symbol,
            candidate_weighted_score(chosen),
            "reject",
            score_version=candidate_score_version(chosen),
        )
        verbose_log(f"{symbol} MARKET {transaction_type} rejected: order_id={order_id} details={details}")
        return False
    if not order_is_complete(details):
        increment_trade_count(symbol)
        write_state(symbol, {
            "date": now_ist().strftime("%Y-%m-%d"),
            "symbol": symbol,
            "entry_order_id": order_id,
            "entry_transaction_type": transaction_type,
            "instrument_class": "INDEX_OPTION",
            "order_product": "I",
            "instrument_key": instrument["instrument_key"],
            "trading_symbol": instrument["trading_symbol"],
            "quantity": int(quantity),
            "lot_size": int(instrument["lot_size"]),
            "lot_multiplier": 1,
            "entry_price": entry_price,
            "target_price": target,
            "stop_loss_price": stop,
            "target_percent": chosen["target_percent"],
            "stop_percent": chosen["stop_percent"],
            "target_points": chosen["target_points"],
            "stop_points": chosen["stop_points"],
            "option_delta_used": chosen["option_delta_used"],
            "exit_profile": chosen.get("exit_profile", {}),
            "profit_protection_enabled_for_trade": chosen.get("profit_protection_enabled_for_trade", True),
            "option_type": chosen.get("option_summary", {}).get("option_type"),
            "technical_context": chosen.get("technicals", {}),
            "direction": direction,
            "confidence": confidence,
            "score": score,
            "weighted_score": candidate_weighted_score(chosen),
            **trade_metadata,
            "status": f"{transaction_type}_PLACED_NOT_COMPLETE",
            "created_at": now_ist().isoformat(),
        })
        return True

    position = find_matching_position_for_side(instrument["instrument_key"], transaction_type)
    fill = position_avg_price(position, transaction_type) if position else None
    fill = fill or to_float(details.get("average_price")) or entry_price
    increment_trade_count(symbol)
    save_open_position_state(
        symbol, order_id, instrument, direction, confidence, score, fill, quantity,
        target, stop, chosen["target_percent"], chosen["stop_percent"],
        entry_transaction_type=transaction_type,
        target_points=chosen["target_points"],
        stop_points=chosen["stop_points"],
        option_delta_used=chosen["option_delta_used"],
        exit_profile=chosen.get("exit_profile", {}),
        profit_protection_enabled_for_trade=chosen.get("profit_protection_enabled_for_trade", True),
        trade_metadata=trade_metadata,
    )

    if chosen.get("manual_override"):
        manual_levels = option_levels_from_index_points(
            symbol,
            fill,
            target_points=chosen["target_points"],
            stop_points=chosen["stop_points"],
            delta=chosen["option_delta_used"],
        )
        post_fill = {
            "allowed": True,
            "target_price": manual_levels["target_price"],
            "stop_loss_price": manual_levels["stop_loss_price"],
            "technicals": chosen.get("technicals", {}),
            "feasibility": {
                "allowed": True,
                "technical_reward_risk": round(
                    float(chosen["target_points"]) / float(chosen["stop_points"]),
                    3,
                ),
                "reasons": ["manual directional override"],
            },
        }
    else:
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
        post_fill = make_post_fill_diagnostic_only(post_fill)
        if post_fill.get("feasibility", {}).get("post_fill_diagnostic_rejected"):
            log(
                f"{symbol} post-fill diagnostic did not pass; position retained because "
                "post-fill checks are observation-only"
            )
    state = read_state(symbol)
    state["weighted_score"] = candidate_weighted_score(chosen)
    state["post_fill_feasibility"] = post_fill["feasibility"]
    state.update(
        {
            "target_price": post_fill["target_price"],
            "stop_loss_price": post_fill["stop_loss_price"],
            "original_stop_loss_price": post_fill["stop_loss_price"],
            "technical_context": post_fill["technicals"],
            "exit_profile": chosen.get("exit_profile", {}),
            "profit_protection_enabled_for_trade": chosen.get("profit_protection_enabled_for_trade", True),
        }
    )
    write_state(symbol, state)
    verbose_log(
        f"{symbol} post-fill levels validated: fill={fill} "
        f"target={post_fill['target_price']} stop_loss={post_fill['stop_loss_price']} "
        f"reward_risk={post_fill['feasibility'].get('technical_reward_risk')}"
    )
    if broker_protective_stop_required(read_state(symbol)):
        try:
            ensure_protective_stop(symbol, read_state(symbol))
        except Exception as error:
            log(f"{symbol} CRITICAL: protective stop failed; flattening immediately: {error}")
            emergency_claim = prepare_protection_failure_exit(
                symbol,
                instrument["instrument_key"],
                transaction_type,
                quantity,
            )
            if not emergency_claim["allowed"]:
                log(
                    f"{symbol} emergency exit not submitted: "
                    f"{emergency_claim['reason']}"
                )
                return True
            fresh_state = emergency_claim["state"]
            emergency_quantity = emergency_claim["quantity"]
            emergency_transaction = "BUY" if transaction_type == "SELL" else "SELL"
            emergency, emergency_payload = place_market_order(
                instrument, emergency_transaction, emergency_quantity
            )
            emergency_id = emergency.get("data", {}).get("order_id")
            if not emergency_id:
                fresh_state["status"] = "POSITION_OPEN"
                fresh_state.pop("exit_submission_in_progress", None)
                write_state(symbol, fresh_state)
                raise RuntimeError(
                    f"{symbol} emergency {emergency_transaction} returned no order_id"
                ) from error
            emergency_details = wait_for_order_complete(emergency_id) if emergency_id else {}
            if order_is_complete(emergency_details):
                complete_exit(
                    symbol,
                    read_state(symbol),
                    emergency_details,
                    fill,
                    "PROTECTION_FAILURE",
                    emergency,
                    emergency_payload,
                )
            else:
                fresh_state = read_state(symbol)
                fresh_state.pop("exit_submission_in_progress", None)
                fresh_state["exit_order_id"] = emergency_id
                fresh_state["exit_fallback_price"] = fill
                write_state(symbol, fresh_state)
            return True
    clear_reentry_guard(symbol)
    return True


def execute_stock_future_candidate(chosen):
    log("STOCK_FUTURE entry blocked: stock-futures execution is disabled.")
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


def finalize_ganesh_gap_position(
    initial_state, fill, quantity, instrument, order_id, state_slot=None
):
    symbol = str(
        initial_state.get("underlying_symbol") or initial_state.get("symbol") or "NIFTY"
    ).upper()
    state_slot = state_slot or GANESH_GAP_STATE_BY_SYMBOL[symbol]
    levels = ganesh_gap_option_levels(
        fill,
        quantity,
        ganesh_gap_state_target(initial_state, "distance"),
    )
    metadata = {
        **initial_state,
        "symbol": symbol,
        "underlying_symbol": symbol,
        "state_slot": state_slot,
        "strategy": initial_state.get("strategy") or "GANESH_GAP_REVERSAL",
        "underlying_instrument_key": UNDERLYING_INDEX_KEYS[symbol],
        "profit_protection_enabled_for_trade": False,
    }
    save_open_position_state(
        state_slot,
        order_id,
        instrument,
        initial_state.get("direction"),
        "HIGH",
        100,
        fill,
        quantity,
        levels["target_price"],
        levels["stop_loss_price"],
        entry_transaction_type="BUY",
        instrument_class="INDEX_OPTION",
        underlying_symbol=symbol,
        target_points=ganesh_gap_state_target(initial_state, "distance"),
        stop_points=None,
        option_delta_used=levels["delta"],
        profit_protection_enabled_for_trade=False,
        trade_metadata=metadata,
        order_product="I",
    )
    state = read_state(state_slot)
    state.update(
        {
            # Initial order metadata contains transitional values. Final fill
            # state must win after that metadata has been merged.
            "status": "POSITION_OPEN",
            "phase": "POSITION_OPEN",
            "target_price": levels["target_price"],
            "planned_target_price": levels["target_price"],
            "stop_loss_price": levels["stop_loss_price"],
            "original_stop_loss_price": levels["stop_loss_price"],
            "profit_booking_mode": "runner",
            "profit_booking_price": levels["target_price"],
        }
    )
    write_state(state_slot, state)
    return state


def finalize_and_protect_ganesh_gap_position(
    initial_state, fill, quantity, instrument, order_id, state_slot=None
):
    state_slot = state_slot or ganesh_gap_state_slot(initial_state)
    with position_finalization_lock(state_slot):
        fresh = read_state(state_slot)
        if not fresh or str(fresh.get("entry_order_id")) != str(order_id):
            return fresh
        if fresh.get("status") == "EXIT_PENDING":
            return fresh
        if fresh.get("protective_stop_order_id"):
            # Repair legacy/stale transitional state after the broker stop has
            # already been armed. This is idempotent and places no new order.
            if fresh.get("status") != "POSITION_OPEN" or fresh.get("phase") != "POSITION_OPEN":
                fresh.update({"status": "POSITION_OPEN", "phase": "POSITION_OPEN"})
                write_state(state_slot, fresh)
            return fresh
        state = (
            fresh
            if fresh.get("status") == "POSITION_OPEN"
            else finalize_ganesh_gap_position(
                fresh or initial_state,
                fill,
                quantity,
                instrument,
                order_id,
                state_slot=state_slot,
            )
        )
        try:
            return ensure_protective_stop(state_slot, state)
        except Exception as error:
            log(f"GANESH GAP CRITICAL: protective stop failed; flattening: {error}")
            state = read_state(state_slot)
            state["status"] = "EXIT_PENDING"
            state["exit_reason"] = "PROTECTION_FAILURE"
            write_state(state_slot, state)
            result, payload = place_market_order(instrument, "SELL", quantity, product="I")
            exit_order_id = result.get("data", {}).get("order_id")
            if not exit_order_id:
                state["status"] = "POSITION_OPEN"
                write_state(state_slot, state)
                raise RuntimeError("GANESH GAP emergency SELL returned no order_id") from error
            details = wait_for_order_complete(exit_order_id)
            if order_is_complete(details):
                complete_exit(
                    state_slot,
                    state,
                    details,
                    fill,
                    "PROTECTION_FAILURE",
                    result,
                    payload,
                )
            else:
                state["exit_order_id"] = exit_order_id
                state["exit_fallback_price"] = fill
                write_state(state_slot, state)
            return read_state(state_slot)


def execute_ganesh_gap_entry(state, snapshot, option, target, symbol=None):
    symbol = str(symbol or snapshot.get("symbol") or "NIFTY").strip().upper()
    state_slot = GANESH_GAP_STATE_BY_SYMBOL[symbol]
    instrument = option["instrument"]
    expected_entry = float(option["ltp"])
    quantity = ganesh_gap_quantity(instrument, expected_entry)
    if quantity <= 0:
        return False, "configured lots or available funds cannot buy one complete lot"
    levels = ganesh_gap_option_levels(expected_entry, quantity, target["distance"])
    lane = str(state.get("strategy_lane") or "REVERSAL").upper()
    transition = (
        continuation_for_gap(state["gap_direction"])
        if lane == "CONTINUATION"
        else transition_for_gap(state["gap_direction"])
    )
    if not transition:
        return False, "gap direction has no executable transition"
    strategy_name = (
        "GANESH_GAP_CONTINUATION" if lane == "CONTINUATION" else "GANESH_GAP_REVERSAL"
    )
    entry_score = (
        to_float(state.get("continuation_score"), 75.0)
        if lane == "CONTINUATION"
        else 100.0
    )
    candidate = {
        "symbol": symbol,
        "underlying_symbol": symbol,
        "direction": transition["direction"],
        "transaction_type": "BUY",
        "instrument": instrument,
        "entry_minimum_score": (
            configured_positive_float("GANESH_CONTINUATION_MIN_SCORE", 75.0)
            if lane == "CONTINUATION"
            else 0
        ),
        "score_cutoff_approved": True,
        "weighted": {"score": entry_score},
    }
    live = ganesh_gap_live_enabled()
    with portfolio_entry_lock():
        if live:
            try:
                occupied_keys = ganesh_gap_occupied_contract_keys(force=True)
            except Exception as error:
                return False, f"broker position collision check failed: {error}"
            if instrument.get("instrument_key") in occupied_keys:
                return False, (
                    "selected contract became occupied at the broker before order placement; "
                    "entry cancelled"
                )
        decision = pre_order_portfolio_decision(
            candidate,
            quantity,
            expected_entry,
            levels["stop_loss_price"],
        )
        if not decision.get("allowed"):
            return False, f"portfolio safety gate: {decision.get('reason')}"
        entry_state = {
            **state,
            "date": now_ist().strftime("%Y-%m-%d"),
            "symbol": symbol,
            "underlying_symbol": symbol,
            "state_slot": state_slot,
            "strategy": strategy_name,
            "strategy_lane": lane,
            "underlying_instrument_key": UNDERLYING_INDEX_KEYS[symbol],
            "direction": candidate["direction"],
            "option_type": option["option_type"],
            "strike": option["strike"],
            "expiry": option["expiry"],
            "instrument_key": instrument["instrument_key"],
            "trading_symbol": instrument["trading_symbol"],
            "lot_size": int(instrument["lot_size"]),
            "quantity": quantity,
            "entry_transaction_type": "BUY",
            "exit_transaction_type": "SELL",
            "instrument_class": "INDEX_OPTION",
            "order_product": "I",
            "underlying_entry_price": snapshot["spot"],
            "underlying_target_type": target["type"],
            "underlying_target_level": target["level"],
            "underlying_target_distance": target["distance"],
            "entry_candle_start": snapshot["candle_start"],
            "entry_candle_open": snapshot["candle_open"],
            "entry_transition": (
                f"{state.get('gap_direction')}_ACCEPTANCE"
                if lane == "CONTINUATION"
                else (
                    "RED_TO_GREEN"
                    if candidate["direction"] == GANESH_BULLISH
                    else "GREEN_TO_RED"
                )
            ),
            "continuation_score": state.get("continuation_score"),
            "continuation_reasons": state.get("continuation_reasons", []),
            "continuation_opening_range_high": (
                (snapshot.get("opening_range") or {}).get("high")
            ),
            "continuation_opening_range_low": (
                (snapshot.get("opening_range") or {}).get("low")
            ),
            "continuation_entry_5m_start": (
                (snapshot.get("latest_completed_5m") or {}).get("start")
            ),
            "entry_volume_confirmed": snapshot.get("volume_confirmed"),
            "entry_volume_ratio": snapshot.get("volume_ratio"),
            "near_expiry_analysis": option.get("near_expiry_analysis", {}),
            "target_price": levels["target_price"],
            "stop_loss_price": levels["stop_loss_price"],
            "entry_price": expected_entry,
            "score": entry_score,
            "weighted_score": entry_score,
            "status": "POSITION_OPEN" if not live else "BUY_PLACED_NOT_COMPLETE",
            "phase": "POSITION_OPEN" if not live else "ENTRY_PENDING",
            "created_at": now_ist().isoformat(),
            "paper_trade": not live,
        }
        write_stream_instruments(
            [
                UNDERLYING_INDEX_KEYS["NIFTY"],
                UNDERLYING_INDEX_KEYS["BANKNIFTY"],
                "NSE_INDEX|India VIX",
                instrument.get("instrument_key"),
            ]
        )
        if not live:
            entry_state.update(
                {
                    "highest_ltp": expected_entry,
                    "lowest_ltp": expected_entry,
                    "original_stop_loss_price": levels["stop_loss_price"],
                    "profit_protection_enabled_for_trade": False,
                }
            )
            write_state(state_slot, entry_state)
            increment_trade_count(state_slot)
            return True, "paper position opened"
        result, payload = place_market_order(instrument, "BUY", quantity, product="I")
        order_id = result.get("data", {}).get("order_id")
        if not order_id:
            return False, "broker BUY returned no order_id"
        entry_state["entry_order_id"] = order_id
        write_state(state_slot, entry_state)

    details = wait_for_order_complete(order_id)
    if order_is_rejected(details):
        state.update(
            {
                "phase": (
                    "CONTINUATION_READY" if lane == "CONTINUATION" else "WAITING_FOR_REVERSAL"
                ),
                "reversal_triggered": False,
            }
        )
        write_state(state_slot, state)
        return False, f"broker rejected BUY: {details.get('status_message') or details.get('status')}"
    increment_trade_count(state_slot)
    if not order_is_complete(details):
        return True, f"entry pending order_id={order_id}"
    position = find_matching_position_for_side(instrument["instrument_key"], "BUY")
    fill = position_avg_price(position, "BUY") if position else None
    fill = fill or to_float(details.get("average_price")) or expected_entry
    finalize_and_protect_ganesh_gap_position(
        entry_state, fill, quantity, instrument, order_id, state_slot=state_slot
    )
    return True, f"live position opened order_id={order_id}"


def run_ganesh_gap_symbol_signal_check(symbol, now=None):
    """Evaluate one Ganesh index lane while sharing the account-wide trade cap."""
    symbol = str(symbol).strip().upper()
    state_slot = GANESH_GAP_STATE_BY_SYMBOL[symbol]
    now = now or now_ist()
    state = read_state(state_slot)
    if state_is_active(state):
        log(f"GANESH GAP {symbol} position already active; no new entry.")
        return
    other_active = [
        active
        for active in active_bot_states()
        if active.get("state_slot") != state_slot
    ]
    if other_active:
        log(f"GANESH GAP {symbol} no trade: another bot-managed position is active.")
        return
    if ganesh_gap_max_trades_per_day() <= ganesh_gap_trade_count_today():
        log(f"GANESH GAP {symbol} maximum combined daily trade count reached.")
        return
    circuit = portfolio_day_circuit()
    if not circuit.get("allowed"):
        log(f"GANESH GAP {symbol} blocked by daily circuit: {circuit.get('reason')}")
        return

    try:
        snapshot = ganesh_gap_market_snapshot(symbol, now)
    except Exception as error:
        log(f"GANESH GAP {symbol} market snapshot unavailable: {error}")
        return
    required_gap_fields = (
        "gap_direction",
        "pivots",
        "previous_close",
        "today_open",
    )
    if (
        state.get("date") != now.strftime("%Y-%m-%d")
        or any(state.get(field) is None for field in required_gap_fields)
    ):
        state = {
            "date": now.strftime("%Y-%m-%d"),
            "symbol": symbol,
            "underlying_symbol": symbol,
            "state_slot": state_slot,
            "phase": "DETECTING_GAP",
            "gap_direction": snapshot["gap"]["direction"],
            "gap_points": snapshot["gap"]["points"],
            "gap_percent": snapshot["gap"]["percent"],
            "previous_high": snapshot["previous_high"],
            "previous_low": snapshot["previous_low"],
            "previous_close": snapshot["previous_close"],
            "today_open": snapshot["today_open"],
            "pivots": snapshot["pivots"],
        }
    if state.get("gap_direction") == GANESH_NO_GAP:
        state["phase"] = "DISABLED_FOR_DAY"
        write_state(state_slot, state)
        reason = f"opening gap {state.get('gap_percent')}% is below threshold"
        record_ganesh_gap_scan(snapshot, state, False, reason)
        log(f"GANESH GAP {symbol} no trade: {reason}")
        return

    reversal_transition = transition_for_gap(state["gap_direction"])
    continuation_transition = continuation_for_gap(state["gap_direction"])
    lane = str(state.get("strategy_lane") or "").upper()
    transition_confirmed = False
    if lane != "CONTINUATION":
        reversal_distance = abs(snapshot["spot"] - snapshot["candle_open"])
        minimum_reversal = configured_non_negative_float("GANESH_MIN_REVERSAL_POINTS", 0.0)
        buffer_confirmed = minimum_reversal > 0 and reversal_distance >= minimum_reversal
        state, transition_confirmed = advance_entry_confirmation(
            state,
            active_two_hour_start(now),
            snapshot["candle_colour"],
            state["gap_direction"],
            required_scans=max(to_int(os.getenv("GANESH_ENTRY_CONFIRMATION_SCANS"), 2), 1),
            buffer_confirmed=buffer_confirmed,
        )
        if transition_confirmed and not lane:
            lane = "REVERSAL"
            state["strategy_lane"] = lane
    state.update(
        {
            "last_scan_at": now.isoformat(),
            "current_candle_open": snapshot["candle_open"],
            "current_candle_colour": snapshot["candle_colour"],
            "bollinger": snapshot["bollinger"],
        }
    )
    minimum_target_distance = configured_non_negative_float(
        f"GANESH_{symbol}_MIN_TARGET_POINTS",
        configured_non_negative_float("GANESH_MIN_TARGET_POINTS", 15.0),
    )
    transition = reversal_transition
    target = None
    option = {}
    reason = "waiting for confirmed colour reversal"
    eligible = False

    if lane == "REVERSAL":
        transition = reversal_transition
        target = nearest_target(
            transition["direction"],
            snapshot["spot"],
            snapshot["bollinger"]["middle"],
            state["pivots"],
            minimum_distance=minimum_target_distance,
        )
        eligible = bool(transition_confirmed and target)
        if transition_confirmed and not target:
            reason = "no valid reversal target beyond the minimum distance"

    continuation_start = configured_clock("GANESH_CONTINUATION_START_TIME", "09:35")
    continuation_end = configured_clock("GANESH_CONTINUATION_LAST_ENTRY_TIME", "11:30")
    continuation_window = continuation_start <= now.time() <= continuation_end
    if (
        lane in {"", "CONTINUATION"}
        and ganesh_gap_continuation_enabled()
        and continuation_window
    ):
        opening = snapshot.get("opening_range") or {}
        latest_five = snapshot.get("latest_completed_5m") or {}
        bullish_continuation = continuation_transition["direction"] == GANESH_BULLISH
        opening_accepted = bool(
            opening.get("complete")
            and (
                to_float(opening.get("close")) > snapshot["today_open"]
                and to_float(opening.get("close")) > snapshot["previous_close"]
                if bullish_continuation
                else to_float(opening.get("close")) < snapshot["today_open"]
                and to_float(opening.get("close")) < snapshot["previous_close"]
            )
        )
        range_broken = bool(
            latest_five.get("complete")
            and (
                to_float(latest_five.get("close")) > to_float(opening.get("high"))
                if bullish_continuation
                else to_float(latest_five.get("close")) < to_float(opening.get("low"))
            )
        )
        if opening_accepted and range_broken:
            transition = continuation_transition
            target = nearest_continuation_target(
                transition["direction"],
                snapshot["spot"],
                snapshot["bollinger"],
                state["pivots"],
                minimum_distance=minimum_target_distance,
            )
            if not target:
                reason = "no valid continuation target beyond the minimum distance"
            else:
                try:
                    option = ganesh_gap_option_candidate(snapshot, transition, symbol=symbol)
                except Exception as error:
                    option = {"allowed": False, "reason": str(error)}
                if not option.get("allowed"):
                    reason = option.get("reason") or "ATM option is unavailable"
                else:
                    state["phase"] = "CONTINUATION_EVALUATION"
                    evidence = ganesh_gap_continuation_evidence(snapshot, option)
                    state.update(
                        {
                            "continuation_score": evidence.get("score"),
                            "continuation_reasons": evidence.get("reasons", []),
                            "continuation_blockers": evidence.get("blockers", []),
                            "continuation_retest_confirmed": evidence.get(
                                "retest_confirmed", False
                            ),
                        }
                    )
                    verbose_log(
                        f"GANESH GAP {symbol} continuation score={evidence.get('score')} "
                        f"allowed={evidence.get('allowed')} "
                        f"reasons={evidence.get('reasons')} "
                        f"blockers={evidence.get('blockers')}"
                    )
                    eligible = bool(evidence.get("allowed"))
                    if eligible:
                        lane = "CONTINUATION"
                        state.update(
                            {
                                "strategy_lane": lane,
                                "phase": "CONTINUATION_READY",
                            }
                        )
                        reason = (
                            f"gap continuation accepted with score {evidence.get('score'):.1f}"
                        )
                    else:
                        reason = "; ".join(evidence.get("blockers", []))

    if lane == "REVERSAL" and eligible and not option:
        try:
            option = ganesh_gap_option_candidate(snapshot, transition, symbol=symbol)
        except Exception as error:
            option = {"allowed": False, "reason": str(error)}
        if not option.get("allowed"):
            eligible = False
            reason = option.get("reason") or "ATM option is unavailable"
        else:
            analysis = option.get("near_expiry_analysis") or {}
            flow = analysis.get("option_flow") or {}
            verbose_log(
                f"GANESH GAP {symbol} expiry split: "
                f"analysis={analysis.get('expiry')} {analysis.get('trading_symbol')} "
                f"chain={analysis.get('chain_bias')}/{analysis.get('chain_confidence')} "
                f"vwap={flow.get('vwap')} volume_ratio={flow.get('volume_ratio')}; "
                f"execution={option.get('expiry')} strike={option.get('strike')} "
                f"{option.get('option_type')}"
            )

    mode = str(os.getenv("GANESH_GAP_MODE", "FAITHFUL")).strip().upper()
    if mode not in {"FAITHFUL", "ENHANCED"}:
        raise RuntimeError("GANESH_GAP_MODE must be FAITHFUL or ENHANCED")
    if eligible and lane == "REVERSAL" and mode == "ENHANCED":
        if configured_bool("GANESH_REQUIRE_VOLUME_CONFIRMATION", False) and not snapshot.get("volume_confirmed"):
            eligible = False
            reason = "enhanced volume confirmation failed"
        if eligible and configured_bool("GANESH_REQUIRE_BB_DIRECTION", False):
            middle = snapshot["bollinger"]["middle"]
            moving_toward = (
                snapshot["spot"] <= middle if transition["direction"] == GANESH_BULLISH
                else snapshot["spot"] >= middle
            )
            if not moving_toward:
                eligible = False
                reason = "enhanced Bollinger-direction filter failed"
        if eligible and configured_bool("GANESH_REQUIRE_MIN_REWARD_RISK", False):
            spot_stop = configured_positive_float("GANESH_SPOT_STOP_POINTS", 15.0)
            reward_risk = target["distance"] / spot_stop
            minimum_rr = configured_positive_float("GANESH_MIN_REWARD_RISK", 1.20)
            if reward_risk < minimum_rr:
                eligible = False
                reason = f"expected reward/risk {reward_risk:.2f} is below {minimum_rr:.2f}"

    if eligible and lane == "REVERSAL":
        reason = "confirmed opening-gap reversal with a valid locked target"

    write_state(state_slot, state)
    record_ganesh_gap_scan(snapshot, state, eligible, reason, option=option, target=target)
    if not eligible:
        log(
            f"GANESH GAP {symbol} {state.get('phase')} gap={state.get('gap_direction')} "
            f"colour={snapshot['candle_colour']} spot={snapshot['spot']} reject: {reason}"
        )
        return
    opened, result_reason = execute_ganesh_gap_entry(
        state, snapshot, option, target, symbol=symbol
    )
    log(
        f"GANESH GAP {symbol} {'entered' if opened else 'rejected'}: "
        f"{option.get('option_type')} "
        f"strike={option.get('strike')} target={target.get('type')}@{target.get('level')} "
        f"reason={result_reason}"
    )


def run_ganesh_gap_signal_check():
    now = now_ist()
    start = configured_clock("GANESH_STRATEGY_START_TIME", "09:30")
    end = configured_clock("GANESH_LAST_ENTRY_TIME", "15:25")
    if not start <= now.time() <= end:
        log("GANESH GAP outside entry window. No action.")
        return

    if active_bot_states():
        log("GANESH GAP position already active; no new entry scan.")
        return

    # Keep both spot feeds available before either symbol is evaluated. If an
    # option is opened, execute_ganesh_gap_entry retains these and adds it.
    write_stream_instruments(
        [
            UNDERLYING_INDEX_KEYS["NIFTY"],
            UNDERLYING_INDEX_KEYS["BANKNIFTY"],
            "NSE_INDEX|India VIX",
        ]
    )
    for symbol in SYMBOLS:
        run_ganesh_gap_symbol_signal_check(symbol, now=now)
        if any(
            state_is_active(read_state(slot)) for slot in GANESH_GAP_STATE_SLOTS
        ):
            break


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

    for state_slot in BOT_STATE_SLOTS:
        state = read_state(state_slot)
        if state:
            try:
                handle_existing_state(state_slot, state)
            except Exception as error:
                log(f"{state_slot} existing-position check ERROR: {error}")

    if trading_engine() == "GANESH":
        run_ganesh_gap_signal_check()
        return

    try:
        day_circuit = portfolio_day_circuit()
    except Exception as error:
        log(f"Portfolio day circuit failed; no new entry for safety: {error}")
        return
    if not day_circuit.get("allowed"):
        for symbol in SYMBOLS:
            expire_watch(symbol, f"portfolio day circuit: {day_circuit.get('reason')}")
        log(f"Portfolio day circuit blocked all new entries: {day_circuit.get('reason')}")
        return
    if to_float(day_circuit.get("score_penalty")) > 0:
        log(
            "Portfolio soft-loss mode active: new-entry minimum scores increased by "
            f"{day_circuit['score_penalty']:.1f} points"
        )

    active_selective_index = {
        symbol for symbol in SYMBOLS if state_is_active(read_state(symbol))
    }
    if active_selective_index:
        for symbol in SYMBOLS:
            expire_watch(symbol, "a selective index-option position is already active")
        verbose_log(
            "Selective index lane occupied by "
            + "/".join(sorted(active_selective_index))
            + "; no new index-option entry will be evaluated"
        )
        return

    try:
        tracked_instrument_keys = {
            read_state(state_slot).get("instrument_key")
            for state_slot in BOT_STATE_SLOTS
            if read_state(state_slot).get("instrument_key")
        }
        untracked_derivative_positions = [
            position
            for position in get_open_positions()
            if position_quantity(position) != 0
            and (position.get("instrument_token") or position.get("instrument_key"))
            not in tracked_instrument_keys
            and (
                str(position.get("exchange") or position.get("segment") or "").upper()
                in {"NSE_FO", "NFO"}
                or str(
                    position.get("instrument_token")
                    or position.get("instrument_key")
                    or ""
                ).startswith("NSE_FO|")
            )
        ]
        if untracked_derivative_positions:
            allow_manual_overlap = configured_bool(
                "ALLOW_BOT_WITH_UNTRACKED_DERIVATIVE_POSITIONS", False
            )
            if not allow_manual_overlap:
                log(
                    "An untracked NSE derivatives position exists; no bot entry. Set "
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
        occupied_contracts = active_index_instrument_keys(symbol)
        try:
            daily_block = daily_index_entry_block_reason(symbol)
        except Exception as error:
            daily_block = "daily guard unavailable"
            log(
                f"{symbol} daily first-outcome guard could not read trade history; "
                f"selective entry disabled for safety: {error}"
            )
        if daily_block:
            expire_watch(symbol, daily_block)
            verbose_log(f"{symbol} no selective trade: {daily_block}")

        try:
            candidate = evaluate_symbol_buy_or_sell(
                symbol,
                allow_option_sell=False,
                include_rejected=(
                    watch_mode_enabled() or bool(read_watch_state(symbol))
                ),
                excluded_instrument_keys=occupied_contracts,
            )
            had_watch = bool(read_watch_state(symbol))
            if (
                had_watch
                and not active_selective_index
                and not daily_block
                and watch_mode_enabled()
            ):
                watched = process_watch(symbol, candidate)
                if watched:
                    qualified.append(watched)
                continue
            if (
                not active_selective_index
                and not daily_block
                and candidate
                and candidate.get("allowed", True)
            ):
                clear_watch_state(symbol)
                qualified.append(candidate)
                continue
            if not active_selective_index and not daily_block and watch_mode_enabled():
                if candidate and candidate.get("watch_eligible"):
                    start_watch(symbol, candidate)
            elif read_watch_state(symbol):
                expire_watch(symbol, "watch mode disabled or selective lane occupied")
        except Exception as error:
            log(f"{symbol} ERROR: {error}")

    if not qualified:
        verbose_log("No new qualified NIFTY or BANKNIFTY selective BUY structure.")
    else:
        ordered = sorted(
            qualified,
            key=lambda item: (
                candidate_weighted_score(item),
                1 if item.get("transaction_type") == "BUY" else 0,
            ),
            reverse=True,
        )
        chosen = ordered[0]
        log_scan_decision(
            chosen["symbol"],
            candidate_weighted_score(chosen),
            "buy",
            score_version=candidate_score_version(chosen),
        )
        try:
            execute_selected_candidate(chosen)
        except Exception as error:
            log(f"{chosen['symbol']} order execution ERROR: {error}")



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
