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

from analysis_journal import record_analysis
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


def allowed_gai_family():
    return socket.AF_INET


urllib3_cn.allowed_gai_family = allowed_gai_family

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
INSTRUMENT_CACHE = BASE_DIR / "upstox_complete.json.gz"
TRADE_COUNT_FILE = BASE_DIR / "daily_trade_count.json"

SYMBOLS = ["NIFTY", "BANKNIFTY"]

# Change only these values next time.
LOT_MULTIPLIERS = {
    "NIFTY": 8,
    "BANKNIFTY": 2,
}

MAX_TRADES_PER_SYMBOL_PER_DAY = 2

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


def max_trades_reached(symbol):
    return trade_count_for(symbol) >= MAX_TRADES_PER_SYMBOL_PER_DAY


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
    return int(LOT_MULTIPLIERS.get(symbol, 1))


def order_quantity_for(symbol, instrument):
    return int(instrument["lot_size"]) * lot_multiplier_for(symbol)


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
    return time(9, 30) <= now <= time(15, 15)


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
):
    target_price = round(float(target_price), 0) if target_price else round(float(entry_price) * 1.10, 0)
    stop_loss_price = round(float(stop_loss_price), 0) if stop_loss_price else round(float(entry_price) * 0.925, 0)

    state = {
        "date": now_ist().strftime("%Y-%m-%d"),
        "symbol": symbol,
        "buy_order_id": order_id,
        "instrument_key": instrument["instrument_key"],
        "trading_symbol": instrument["trading_symbol"],
        "quantity": int(quantity),
        "lot_size": int(instrument["lot_size"]),
        "lot_multiplier": lot_multiplier_for(symbol),
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "entry_price": round(float(entry_price), 0),
        "target_price": target_price,
        "stop_loss_price": stop_loss_price,
        "status": "POSITION_OPEN",
        "created_at": now_ist().isoformat(),
        "highest_ltp": round(float(entry_price), 2),
        "trailing_stop_active": False,
        "trailing_stop_reason": "",
    }

    write_state(symbol, state)

    log(
        f"{symbol} POSITION OPEN: symbol={instrument['trading_symbol']} "
        f"qty={quantity} lots={lot_multiplier_for(symbol)} entry={state['entry_price']} "
        f"target={target_price} stop_loss={stop_loss_price}"
    )

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
            exit_price = ltp

            if sell_order_id:
                sell_details = wait_for_order_complete(sell_order_id)
                exit_price = (
                    to_float(sell_details.get("average_price"))
                    or to_float(sell_details.get("price"))
                    or ltp
                )

            journal_row = record_closed_trade(state, exit_price, exit_reason)
            log(
                f"{symbol} {exit_reason} exit MARKET SELL placed: result={result} "
                f"payload={payload} journal={journal_row}"
            )
            clear_state(symbol)

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
            )
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

        if df_atm is None or df_atm.empty:
            return False, "sentiment check skipped: no ATM data"

        atm = df_atm.iloc[0]

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
                exit_price = position_ltp(position)

                if sell_order_id:
                    sell_details = wait_for_order_complete(sell_order_id)
                    exit_price = (
                        to_float(sell_details.get("average_price"))
                        or to_float(sell_details.get("price"))
                        or exit_price
                    )

                journal_row = record_closed_trade(state, exit_price, "SQUAREOFF")
                log(
                    f"{symbol} bot squareoff MARKET SELL placed: result={result} "
                    f"payload={payload} journal={journal_row}"
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

    atm_close = float(atm_flow.get("close") or 0)
    atm_vwap = float(atm_flow.get("vwap") or 999999)

    return (
        score_value >= 55
        and fifteen.get("bias") != opposite_direction(direction)
        and five.get("bias") != opposite_direction(direction)
        and atm_flow.get("bias") in {"BULLISH", "NEUTRAL"}
        and atm_close >= atm_vwap
    )


def process_symbol(symbol):
    state = read_state(symbol)

    if state and handle_existing_state(symbol, state):
        return

    # if max_trades_reached(symbol):
    #     log(f"{symbol} daily trade limit reached: {trade_count_for(symbol)}/{MAX_TRADES_PER_SYMBOL_PER_DAY}. No new order.")
    #     return

    rec = get_index_recommendation(symbol)
    record_option_chain_snapshot(symbol, rec)
    direction = rec["direction"]
    confidence = rec["confidence"]
    score = rec["score"]
    atm = rec["atm"]
    prices = rec["prices"]

    log(f"{symbol} signal: {direction}, confidence={confidence}, score={score}, strike={atm['strike']}, expiry={atm['expiry']}")

    if direction not in {"BULLISH", "BEARISH"}:
        log(f"{symbol} no trade: neutral signal.")
        return

    if confidence != "HIGH" or abs(score) < 4:
        log(f"{symbol} no trade: signal is not strong HIGH confidence.")
        return

    option_type = "CE" if direction == "BULLISH" else "PE"
    expected_entry_price = prices.get("entry_price")

    if not expected_entry_price:
        log(f"{symbol} no trade: missing expected entry price.")
        return

    instrument = find_index_option_instrument(symbol, atm["expiry"], atm["strike"], option_type)
    order_quantity = order_quantity_for(symbol, instrument)

    live = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"

    expected_target = round(float(expected_entry_price) * 1.1, 0)
    expected_stop_loss = round(float(expected_entry_price) * 0.925, 0)

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

        technicals["four_hour"] = convert_index_levels_to_option_premium(
        technicals.get("four_hour", {}),
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
    except Exception as e:
        log(f"{symbol} technical analysis failed: {e}")
        technicals = {
            "four_hour": {"bias": "NEUTRAL", "confidence": "LOW", "reasons": [str(e)]},
            "fifteen_min": {"bias": "NEUTRAL", "confidence": "LOW", "reasons": [str(e)]},
        }

    atm_option_flow = get_option_volume_vwap_analysis(
    instrument["instrument_key"],
    side_label=instrument["trading_symbol"],
)
    technicals["atm_option_flow"] = atm_option_flow

    option_trend = get_option_chain_trend(symbol, direction)
    weighted_score = weighted_alignment_score(option_summary, technicals, option_trend)

    option_summary["option_chain_trend"] = option_trend
    option_summary["weighted_alignment"] = weighted_score

    cautious_override = False

    if weighted_score["grade"] == "SKIP":
        if cautious_override_allowed(direction, weighted_score, technicals):
            cautious_override = True
            expected_target = round(float(expected_entry_price) * 1.06, 0)
            expected_stop_loss = round(float(expected_entry_price) * 0.95, 0)

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
        expected_target = round(float(expected_entry_price) * 1.06, 0)
        expected_stop_loss = round(float(expected_entry_price) * 0.95, 0)
        option_summary["target_price"] = expected_target
        option_summary["stop_loss_price"] = expected_stop_loss
        option_summary["cautious_trade"] = True

    llm_decision = get_llm_decision(symbol, option_summary, technicals)
    record_analysis(symbol, option_summary, technicals, llm_decision)

    log(
        f"{symbol} analysis: option={option_summary} "
        f"4h={technicals.get('four_hour')} "
        f"15m={technicals.get('fifteen_min')} "
        f"5m={technicals.get('five_min')} "
        f"weighted={weighted_score} "
        f"llm={llm_decision} "
        f"atm_option_flow={technicals.get('atm_option_flow')} "
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

    if llm_target and llm_stop_loss:
        expected_target = round(float(llm_target), 0)
        expected_stop_loss = round(float(llm_stop_loss), 0)

    if expected_target <= float(expected_entry_price) or expected_stop_loss >= float(expected_entry_price):
        log(
            f"{symbol} no trade: invalid target/stop from LLM. "
            f"entry={expected_entry_price} target={expected_target} stop={expected_stop_loss}"
        )
        return

    log(
        f"{symbol} prepared MARKET BUY: {instrument['trading_symbol']} qty={order_quantity} lots={lot_multiplier_for(symbol)} "
        f"expected_entry={expected_entry_price} expected_target={expected_target} "
        f"expected_stop_loss={expected_stop_loss} live={live}"
    )

    # if weighted_score["grade"] == "SKIP":
    #     score_value = float(weighted_score.get("score") or 0)
    #     fifteen = technicals.get("fifteen_min", {}) or {}
    #     five = technicals.get("five_min", {}) or {}
    #     atm_flow = technicals.get("atm_option_flow", {}) or {}

    #     cautious_override = (
    #         score_value >= 55
    #         and fifteen.get("bias") != opposite_direction(direction)
    #         and five.get("bias") != opposite_direction(direction)
    #         and atm_flow.get("bias") in {"BULLISH", "NEUTRAL"}
    #         and float(atm_flow.get("close") or 0) >= float(atm_flow.get("vwap") or 999999)
    #     )

    #     if cautious_override:
    #         expected_target = round(float(expected_entry_price) * 1.06, 0)
    #         expected_stop_loss = round(float(expected_entry_price) * 0.95, 0)
    #         log(
    #             f"{symbol} cautious override allowed despite SKIP: "
    #             f"score={score_value} target={expected_target} stop_loss={expected_stop_loss}"
    #         )
    #     else:
    #         log(f"{symbol} no trade: weighted score too low: {weighted_score}")
    #         return

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
    log(f"{symbol} daily trade count updated: {count}/{MAX_TRADES_PER_SYMBOL_PER_DAY}")
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
            "lot_multiplier": lot_multiplier_for(symbol),
            "target_price": expected_target,
            "stop_loss_price": expected_stop_loss,
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
    )


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