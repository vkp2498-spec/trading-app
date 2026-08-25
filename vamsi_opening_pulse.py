#!/usr/bin/env python3
"""One-shot 09:20 NIFTY opening-pulse GTT strategy for Vamsi.

The completed 09:15-09:20 market path always resolves to BULLISH or BEARISH.
There are no score, breadth, option-flow, regime, or structure entry gates.
The only possible failures are operational ones that make a broker order
impossible or unsafe: unavailable/faulty quotes, no tradable contract, no whole
lot of buying capital, invalid credentials, or an unsuccessful broker request.
"""

from __future__ import annotations

import argparse
import math
import os
import time
from pathlib import Path

import requests

import trade_bot
from safe_storage import atomic_write_json, file_lock
from strategy_core import (
    choose_expiry,
    fetch_upstox_option_chain,
    get_expiries_from_upstox,
    now_ist,
)


ENGINE = "VAMSI_OPENING_PULSE_V1"
NIFTY_STATE_SLOT = "NIFTY"
NIFTY_KEY = "NSE_INDEX|Nifty 50"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "vamsi_opening_pulse"
CLAIM_FILE = DATA_DIR / "daily_entry_claim.json"
LOCK_FILE = BASE_DIR / ".vamsi_opening_pulse.lock"
GTT_PLACE_URL = "https://api.upstox.com/v3/order/gtt/place"
GTT_CANCEL_URL = "https://api.upstox.com/v3/order/gtt/cancel"


def log(message: str) -> None:
    print(
        f"{now_ist().strftime('%Y-%m-%d %H:%M:%S')} | {ENGINE} | {message}",
        flush=True,
    )


def configured_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {
        "1", "true", "yes", "on"
    }


def configured_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be numeric") from error


def configured_minutes(name: str, default: str) -> int:
    raw = str(os.getenv(name, default)).strip()
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must use HH:MM") from error
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise RuntimeError(f"{name} must use HH:MM")
    return hour * 60 + minute


def round_tick(value: float) -> float:
    return round(round(float(value) / 0.05) * 0.05, 2)


def sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def opening_pulse(snapshot: dict) -> dict:
    """Always resolve the completed opening path to CALL or PUT."""
    five = snapshot.get("latest_completed_5m") or {}
    spot = float(snapshot["spot"])
    today_open = float(snapshot["today_open"])
    previous_close = float(snapshot["previous_close"])
    five_open = float(five.get("open") or today_open)
    five_close = float(five.get("close") or spot)
    pivots = snapshot.get("pivots") or {}
    bands = snapshot.get("bollinger") or {}
    components = {
        "opening_5m_body": {
            "points": five_close - five_open,
            "weight": 3,
        },
        "session_move": {
            "points": spot - today_open,
            "weight": 2,
        },
        "overnight_gap": {
            "points": today_open - previous_close,
            "weight": 1,
        },
        "previous_close_position": {
            "points": spot - previous_close,
            "weight": 1,
        },
        "pivot_position": {
            "points": spot - float(pivots.get("P") or spot),
            "weight": 1,
        },
        "bollinger_middle_position": {
            "points": spot - float(bands.get("middle") or spot),
            "weight": 1,
        },
    }
    vote = sum(sign(item["points"]) * item["weight"] for item in components.values())
    if vote == 0:
        tie_breakers = (
            five_close - five_open,
            spot - today_open,
            spot - previous_close,
            spot - float(pivots.get("P") or spot),
        )
        vote = next((sign(value) for value in tie_breakers if sign(value)), 1)
    direction = "BULLISH" if vote > 0 else "BEARISH"
    maximum_vote = sum(item["weight"] for item in components.values())
    return {
        "direction": direction,
        "option_direction": "CALL" if direction == "BULLISH" else "PUT",
        "vote": vote,
        "strength": round(abs(vote) / maximum_vote * 100, 1),
        "components": {
            name: {
                "points": round(item["points"], 2),
                "vote": sign(item["points"]) * item["weight"],
            }
            for name, item in components.items()
        },
    }


def named_levels(snapshot: dict) -> list[dict]:
    pivots = snapshot.get("pivots") or {}
    bands = snapshot.get("bollinger") or {}
    raw = [
        ("R1", pivots.get("R1")),
        ("R2", pivots.get("R2")),
        ("S1", pivots.get("S1")),
        ("S2", pivots.get("S2")),
        ("BB_MIDDLE", bands.get("middle")),
        ("BB_UPPER", bands.get("upper")),
        ("BB_LOWER", bands.get("lower")),
    ]
    return [
        {"name": name, "level": round(float(value), 2)}
        for name, value in raw
        if value is not None and math.isfinite(float(value))
    ]


def balanced_level_pair(
    direction: str,
    spot: float,
    levels: list[dict],
    *,
    minimum_ratio: float = 0.80,
    maximum_ratio: float = 1.25,
    minimum_distance: float = 5.0,
    fallback_distance: float = 30.0,
) -> dict:
    """Choose the nearest named pair that is approximately one-to-one."""
    bullish = str(direction).upper() == "BULLISH"
    targets = []
    stops = []
    for item in levels:
        level = float(item["level"])
        target_distance = level - spot if bullish else spot - level
        stop_distance = spot - level if bullish else level - spot
        if target_distance >= minimum_distance:
            targets.append({**item, "distance": target_distance})
        if stop_distance >= minimum_distance:
            stops.append({**item, "distance": stop_distance})

    pairs = []
    for target in targets:
        for stop in stops:
            ratio = target["distance"] / stop["distance"]
            balanced = minimum_ratio <= ratio <= maximum_ratio
            pairs.append(
                {
                    "target": target,
                    "stop": stop,
                    "ratio": ratio,
                    "balanced": balanced,
                    "ratio_error": abs(math.log(max(ratio, 1e-9))),
                    "proximity": max(target["distance"], stop["distance"]),
                    "total_distance": target["distance"] + stop["distance"],
                }
            )
    if pairs:
        balanced_pairs = [pair for pair in pairs if pair["balanced"]]
        selected = min(
            balanced_pairs,
            key=lambda pair: (pair["proximity"], pair["ratio_error"], pair["total_distance"]),
        ) if balanced_pairs else min(
            pairs,
            key=lambda pair: (pair["ratio_error"], pair["proximity"], pair["total_distance"]),
        )
        target = selected["target"]
        stop = selected["stop"]
        return {
            "target_name": target["name"],
            "target_level": round(target["level"], 2),
            "target_distance": round(target["distance"], 2),
            "stop_name": stop["name"],
            "stop_level": round(stop["level"], 2),
            "stop_distance": round(stop["distance"], 2),
            "reward_risk": round(selected["ratio"], 3),
            "balanced_named_pair": selected["balanced"],
        }

    available = targets or stops
    distance = min(
        (float(item["distance"]) for item in available),
        default=max(float(fallback_distance), float(minimum_distance)),
    )
    distance = max(distance, minimum_distance)
    if bullish:
        target_level, stop_level = spot + distance, spot - distance
    else:
        target_level, stop_level = spot - distance, spot + distance
    return {
        "target_name": "BALANCED_EXTENSION",
        "target_level": round(target_level, 2),
        "target_distance": round(distance, 2),
        "stop_name": "BALANCED_EXTENSION",
        "stop_level": round(stop_level, 2),
        "stop_distance": round(distance, 2),
        "reward_risk": 1.0,
        "balanced_named_pair": False,
    }


def select_atm_option(option_direction: str) -> dict:
    option_type = "CE" if option_direction == "CALL" else "PE"
    expiry = choose_expiry("NIFTY", get_expiries_from_upstox("NIFTY"))
    atm, _nearby, _chain = fetch_upstox_option_chain(
        "NIFTY",
        nearby=1,
        expiry_role="execution",
        expiry=expiry,
    )
    if atm.empty:
        raise RuntimeError("NIFTY ATM option chain is unavailable")
    row = atm.iloc[0].to_dict()
    prefix = option_type
    strike = float(row.get("strike") or 0)
    instrument = trade_bot.find_index_option_instrument(
        "NIFTY", expiry, strike, option_type
    )
    instrument_key = str(
        row.get(f"{prefix}_instrument_key")
        or instrument.get("instrument_key")
        or ""
    )
    ltp = float(row.get(f"{prefix}_ltp") or 0)
    ask = float(row.get(f"{prefix}_ask_price") or 0)
    bid = float(row.get(f"{prefix}_bid_price") or 0)
    if not instrument_key or max(ask, ltp, bid) <= 0:
        raise RuntimeError(f"ATM NIFTY {option_type} has no executable quote")
    return {
        "instrument_key": instrument_key,
        "trading_symbol": instrument.get("trading_symbol") or (
            f"NIFTY {int(strike)} {option_type} {expiry}"
        ),
        "underlying_symbol": "NIFTY",
        "option_type": option_type,
        "strike": strike,
        "expiry": str(expiry),
        "entry_price": round_tick(ask if ask > 0 else ltp if ltp > 0 else bid),
        "delta": max(min(abs(float(row.get(f"{prefix}_delta") or 0.50)), 1.0), 0.05),
        "lot_size": max(int(instrument.get("lot_size") or 1), 1),
    }


def option_price_levels(option: dict, level_pair: dict) -> dict:
    entry = float(option["entry_price"])
    delta = float(option["delta"])
    target_distance = max(float(level_pair["target_distance"]) * delta, 0.05)
    stop_distance = max(float(level_pair["stop_distance"]) * delta, 0.05)
    target = round_tick(entry + target_distance)
    stop = round_tick(max(entry - stop_distance, 0.05))
    return {
        "target_price": target,
        "stop_loss_price": stop,
        "target_option_points": round(target - entry, 2),
        "stop_option_points": round(entry - stop, 2),
        "option_reward_risk": round(
            (target - entry) / max(entry - stop, 0.05), 3
        ),
    }


def max_allocation_quantity(option: dict) -> tuple[int, float]:
    usable = trade_bot.maximum_available_option_capital(force_refresh=True)
    lot_size = int(option["lot_size"])
    per_lot = float(option["entry_price"]) * lot_size
    lots = int(usable // per_lot) if per_lot > 0 else 0
    return lots * lot_size, round(float(usable), 2)


def auth_headers() -> dict:
    token = str(os.getenv("UPSTOX_ACCESS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN is required for live GTT execution")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    algo_name = str(os.getenv("UPSTOX_ALGO_NAME") or "").strip()
    if algo_name:
        headers["X-Algo-Name"] = algo_name
    return headers


def response_json(response: requests.Response, action: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:500]}
    if response.status_code >= 300 or payload.get("status") == "error":
        raise RuntimeError(f"Upstox {action} failed ({response.status_code}): {payload}")
    return payload


def gtt_payload(option: dict, levels: dict, quantity: int) -> dict:
    """Return an immediate-entry target/stop GTT with no trailing field."""
    return {
        "type": "MULTIPLE",
        "quantity": int(quantity),
        "product": "I",
        "instrument_token": option["instrument_key"],
        "transaction_type": "BUY",
        "rules": [
            {
                "strategy": "ENTRY",
                "trigger_type": "IMMEDIATE",
                "trigger_price": option["entry_price"],
                "market_protection": -1,
            },
            {
                "strategy": "TARGET",
                "trigger_type": "IMMEDIATE",
                "trigger_price": levels["target_price"],
                "market_protection": -1,
            },
            {
                "strategy": "STOPLOSS",
                "trigger_type": "IMMEDIATE",
                "trigger_price": levels["stop_loss_price"],
                "market_protection": -1,
            },
        ],
    }


def read_claim() -> dict:
    try:
        value = trade_bot.read_json(CLAIM_FILE, {})
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def write_claim(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(CLAIM_FILE, payload, sort_keys=True)


def _state(
    snapshot: dict,
    pulse: dict,
    level_pair: dict,
    option: dict,
    option_levels: dict,
    quantity: int,
    usable_capital: float,
) -> dict:
    direction = pulse["direction"]
    return {
        "date": now_ist().date().isoformat(),
        "symbol": "NIFTY",
        "underlying_symbol": "NIFTY",
        "underlying_instrument_key": NIFTY_KEY,
        "instrument_class": "INDEX_OPTION",
        "strategy": ENGINE,
        "status": "GTT_SUBMITTING",
        "entry_transaction_type": "BUY",
        "exit_transaction_type": "SELL",
        "position_side": "LONG_OPTION",
        "order_product": "I",
        "instrument_key": option["instrument_key"],
        "trading_symbol": option["trading_symbol"],
        "option_type": option["option_type"],
        "quantity": int(quantity),
        "lot_size": int(option["lot_size"]),
        "lot_multiplier": max(int(quantity) // int(option["lot_size"]), 1),
        "direction": direction,
        "confidence": "OPENING_PULSE",
        "score": pulse["strength"],
        "weighted_score": pulse["strength"],
        "entry_score_version": ENGINE,
        "entry_price": option["entry_price"],
        "target_price": option_levels["target_price"],
        "planned_target_price": option_levels["target_price"],
        "stop_loss_price": option_levels["stop_loss_price"],
        "original_stop_loss_price": option_levels["stop_loss_price"],
        "underlying_entry_price": snapshot["spot"],
        "underlying_target_price": level_pair["target_level"],
        "underlying_stop_price": level_pair["stop_level"],
        "underlying_target_name": level_pair["target_name"],
        "underlying_stop_name": level_pair["stop_name"],
        "target_points": level_pair["target_distance"],
        "stop_points": level_pair["stop_distance"],
        "underlying_reward_risk": level_pair["reward_risk"],
        "option_delta_used": option["delta"],
        "option_reward_risk": option_levels["option_reward_risk"],
        "max_allocation_capital": usable_capital,
        "pulse": pulse,
        "profit_protection_enabled_for_trade": False,
        "trailing_stop_active": False,
        "trailing_stop_reason": "Disabled; broker GTT has fixed target and stop",
        "created_at": now_ist().isoformat(),
        "squareoff_at": str(
            os.getenv("VAMSI_OPENING_PULSE_SQUAREOFF_TIME", "15:00")
        ).strip(),
    }


def scan() -> dict:
    trade_bot.load_env()
    if trade_bot.trading_engine() != ENGINE:
        raise RuntimeError(f"TRADING_ENGINE must be {ENGINE}")
    if not configured_bool("ENABLE_LIVE_TRADING", False) or not configured_bool(
        "VAMSI_OPENING_PULSE_LIVE_ENABLED", False
    ):
        raise RuntimeError("Both opening-pulse live switches must be enabled")
    current = now_ist()
    minute = current.hour * 60 + current.minute
    first = configured_minutes("VAMSI_OPENING_PULSE_FIRST_ENTRY_TIME", "09:20")
    last = configured_minutes("VAMSI_OPENING_PULSE_LAST_ENTRY_TIME", "09:22")
    if not first <= minute <= last:
        raise RuntimeError("Outside the 09:20 opening-pulse entry window")

    with file_lock(LOCK_FILE):
        claim = read_claim()
        today = current.date().isoformat()
        if claim.get("date") == today:
            log(f"one daily entry already claimed; status={claim.get('status')}")
            return {"action": "DUPLICATE", **claim}
        write_claim(
            {
                "date": today,
                "status": "CLAIMED",
                "claimed_at": current.isoformat(),
            }
        )

        existing = trade_bot.read_state(NIFTY_STATE_SLOT)
        if trade_bot.state_is_active(existing):
            message = "an earlier bot position state is still active"
            write_claim({"date": today, "status": "OPERATIONAL_ERROR", "reason": message})
            raise RuntimeError(message)

        try:
            snapshot = trade_bot.ganesh_gap_market_snapshot("NIFTY", current_time=current)
            pulse = opening_pulse(snapshot)
            pair = balanced_level_pair(
                pulse["direction"],
                float(snapshot["spot"]),
                named_levels(snapshot),
                minimum_ratio=configured_float(
                    "VAMSI_OPENING_PULSE_MIN_REWARD_RISK", 0.80
                ),
                maximum_ratio=configured_float(
                    "VAMSI_OPENING_PULSE_MAX_REWARD_RISK", 1.25
                ),
                minimum_distance=configured_float(
                    "VAMSI_OPENING_PULSE_MIN_LEVEL_DISTANCE_POINTS", 5.0
                ),
                fallback_distance=configured_float(
                    "VAMSI_OPENING_PULSE_FALLBACK_DISTANCE_POINTS", 30.0
                ),
            )
            option = select_atm_option(pulse["option_direction"])
            prices = option_price_levels(option, pair)
            quantity, usable = max_allocation_quantity(option)
            if quantity < int(option["lot_size"]):
                raise RuntimeError("available option capital cannot buy one whole NIFTY lot")
            state = _state(snapshot, pulse, pair, option, prices, quantity, usable)
            trade_bot.write_state(NIFTY_STATE_SLOT, state)
            payload = gtt_payload(option, prices, quantity)
            response = requests.post(
                GTT_PLACE_URL,
                headers=auth_headers(),
                json=payload,
                timeout=20,
            )
            result = response_json(response, "opening-pulse GTT placement")
            identifiers = (result.get("data") or {}).get("gtt_order_ids") or []
            if not identifiers:
                raise RuntimeError(f"Upstox GTT response has no ID: {result}")
            state.update(
                {
                    "status": "GTT_ACTIVE",
                    "gtt_order_id": str(identifiers[0]),
                    "gtt_payload": payload,
                }
            )
            trade_bot.write_state(NIFTY_STATE_SLOT, state)
            trade_bot.increment_trade_count("NIFTY")
            trade_bot.send_apple_trade_entered_alert(state)
            write_claim(
                {
                    "date": today,
                    "status": "GTT_ACTIVE",
                    "gtt_order_id": str(identifiers[0]),
                    "direction": pulse["direction"],
                    "trading_symbol": option["trading_symbol"],
                    "quantity": quantity,
                    "submitted_at": now_ist().isoformat(),
                }
            )
            log(
                f"{pulse['direction']} pulse vote={pulse['vote']:+d}; BUY "
                f"{option['trading_symbol']} qty={quantity} entry~{option['entry_price']} "
                f"target={prices['target_price']} ({pair['target_name']} "
                f"{pair['target_level']}) stop={prices['stop_loss_price']} "
                f"({pair['stop_name']} {pair['stop_level']}) "
                f"underlying_rr={pair['reward_risk']:.2f} GTT={identifiers[0]} "
                "trailing=OFF"
            )
            return {"action": "LIVE_GTT", **state}
        except Exception as error:
            state = trade_bot.read_state(NIFTY_STATE_SLOT)
            if state.get("strategy") == ENGINE:
                state["status"] = "GTT_SUBMISSION_UNKNOWN"
                state["submission_error"] = str(error)
                trade_bot.write_state(NIFTY_STATE_SLOT, state)
            write_claim(
                {
                    "date": today,
                    "status": "OPERATIONAL_ERROR",
                    "reason": str(error),
                    "failed_at": now_ist().isoformat(),
                }
            )
            log(f"CRITICAL opening order was not confirmed: {error}")
            raise


def cancel_gtt(gtt_order_id: str) -> None:
    last_error = None
    for attempt in range(1, 4):
        try:
            response = requests.delete(
                GTT_CANCEL_URL,
                headers=auth_headers(),
                json={"gtt_order_id": gtt_order_id},
                timeout=15,
            )
            response_json(response, "GTT cancellation")
            return
        except Exception as error:
            last_error = error
            if attempt < 3:
                time.sleep(1)
    raise RuntimeError(f"could not confirm GTT cancellation: {last_error}")


def squareoff() -> dict:
    trade_bot.load_env()
    state = trade_bot.read_state(NIFTY_STATE_SLOT)
    if state.get("strategy") != ENGINE or not state.get("instrument_key"):
        log("15:00 square-off: no opening-pulse state")
        return {"action": "NO_POSITION"}

    gtt_order_id = str(state.get("gtt_order_id") or "")
    if gtt_order_id:
        try:
            cancel_gtt(gtt_order_id)
            state["gtt_cancelled_at"] = now_ist().isoformat()
            trade_bot.write_state(NIFTY_STATE_SLOT, state)
        except Exception as error:
            position = trade_bot.find_matching_position_for_side(
                state["instrument_key"], "BUY", force=True
            )
            if position:
                state["squareoff_error"] = str(error)
                trade_bot.write_state(NIFTY_STATE_SLOT, state)
                log(f"CRITICAL 15:00 square-off blocked: {error}")
                raise
            log(f"GTT cancel returned after broker position had closed: {error}")

    position = trade_bot.find_matching_position_for_side(
        state["instrument_key"], "BUY", force=True
    )
    if not position:
        trade_bot.clear_state(NIFTY_STATE_SLOT)
        write_claim(
            {
                **read_claim(),
                "status": "CLOSED_BY_GTT",
                "closed_at": now_ist().isoformat(),
            }
        )
        log("15:00 square-off: GTT had already closed the broker position")
        return {"action": "ALREADY_CLOSED"}

    quantity = abs(trade_bot.position_quantity(position))
    instrument = {
        "instrument_key": state["instrument_key"],
        "trading_symbol": state.get("trading_symbol"),
    }
    state["status"] = "EXIT_PENDING"
    state["exit_reason"] = "TIME_SQUAREOFF_1500"
    trade_bot.write_state(NIFTY_STATE_SLOT, state)
    result, payload = trade_bot.place_market_order(
        instrument, "SELL", quantity, product="I"
    )
    order_id = (result.get("data") or {}).get("order_id")
    if not order_id:
        state["status"] = "POSITION_OPEN"
        trade_bot.write_state(NIFTY_STATE_SLOT, state)
        raise RuntimeError(f"15:00 SELL returned no order_id: {result}")
    details = trade_bot.wait_for_order_complete(order_id)
    if trade_bot.order_is_complete(details):
        row = trade_bot.complete_exit(
            NIFTY_STATE_SLOT,
            state,
            details,
            trade_bot.position_ltp(position),
            "TIME_SQUAREOFF_1500",
            result,
            payload,
        )
        write_claim(
            {
                **read_claim(),
                "status": "TIME_SQUAREOFF_1500",
                "closed_at": now_ist().isoformat(),
            }
        )
        log(f"15:00 square-off complete qty={quantity} pnl={row.get('gross_pnl')}")
        return {"action": "SQUAREOFF_COMPLETE", "quantity": quantity}
    state["exit_order_id"] = order_id
    state["exit_fallback_price"] = trade_bot.position_ltp(position)
    trade_bot.write_state(NIFTY_STATE_SLOT, state)
    raise RuntimeError(
        f"15:00 SELL is not complete: order_id={order_id} "
        f"status={trade_bot.order_status(details)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--scan", action="store_true")
    action.add_argument("--squareoff", action="store_true")
    args = parser.parse_args()
    if args.scan:
        scan()
    else:
        squareoff()


if __name__ == "__main__":
    main()
