"""Isolated overnight NIFTY option gap lane.

Entry is evaluated around 15:15 IST and, when the deterministic closing
evidence is strong, one ATM NIFTY CE or PE is bought for the next session.
The position is force-closed at 09:16 IST. Its state is separate from the
intraday bot, so the regular 15:25 square-off cannot close it accidentally.
"""

import os
import sys
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None

from analysis_journal import record_analysis
from market_technicals import get_technical_analysis
from option_chain_trend import get_option_chain_trend
from strategy_core import get_index_recommendation, now_ist
from trade_bot import (
    BASE_DIR,
    build_trade_candidate,
    collect_institutional_footprint,
    get_open_positions,
    get_order_details,
    load_env,
    log as trade_log,
    order_is_complete,
    order_is_rejected,
    place_market_order,
    position_avg_price,
    position_ltp,
    position_quantity,
    read_json,
    wait_for_order_complete,
    write_json,
)
from trade_journal import record_closed_trade


STATE_FILE = BASE_DIR / "data" / "overnight_gap_state.json"
LOCK_FILE = BASE_DIR / ".overnight_gap.lock"


def log(message):
    trade_log(f"OVERNIGHT_GAP {message}")


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be numeric") from error


def env_positive(name, default):
    value = env_float(name, default)
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than 0")
    return value


def live_trading_enabled():
    return (
        env_bool("ENABLE_LIVE_TRADING", False)
        and env_bool("OVERNIGHT_GAP_LIVE_TRADING", False)
    )


def target_stop_points():
    return (
        env_positive("OVERNIGHT_GAP_TARGET_POINTS", 30.0),
        env_positive("OVERNIGHT_GAP_STOP_POINTS", 20.0),
        env_positive("OVERNIGHT_GAP_OPTION_DELTA", 0.50),
    )


def premium_levels(entry_price, target_points, stop_points, delta):
    entry = float(entry_price)
    if entry <= 0:
        raise RuntimeError("Overnight option entry price must be positive")
    return {
        "target_price": round(entry + target_points * delta, 2),
        "stop_loss_price": round(max(entry - stop_points * delta, 0.05), 2),
    }


def read_state():
    return read_json(STATE_FILE, {})


def write_state(state):
    write_json(STATE_FILE, state)


def clear_state():
    write_state({})


@contextmanager
def lane_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def entry_window_ok():
    current = now_ist().time()
    return current >= current.replace(hour=15, minute=14, second=0, microsecond=0) and current <= current.replace(hour=15, minute=16, second=59, microsecond=0)


def build_candidate():
    recommendation = get_index_recommendation("NIFTY")
    direction = recommendation.get("direction")
    confidence = recommendation.get("confidence")
    score = float(recommendation.get("score") or 0)
    min_score = env_float("OVERNIGHT_GAP_MIN_SCORE", 85.0)
    atm = recommendation.get("atm") or {}

    log(
        f"signal: direction={direction} confidence={confidence} score={score:.1f} "
        f"strike={atm.get('strike')} expiry={atm.get('expiry')}"
    )
    if direction not in {"BULLISH", "BEARISH"}:
        log("no trade: closing option-chain signal is not directional")
        return None
    if env_bool("OVERNIGHT_GAP_REQUIRE_HIGH_CONFIDENCE", True) and confidence != "HIGH":
        log("no trade: closing option-chain confidence is not HIGH")
        return None

    technicals = get_technical_analysis("NIFTY")
    institutional = collect_institutional_footprint("NIFTY", recommendation)
    option_trend = get_option_chain_trend("NIFTY", direction, expiry=atm.get("expiry"))
    candidate, _ = build_trade_candidate(
        "NIFTY", recommendation, technicals, institutional, option_trend, "BUY"
    )
    if not candidate or not candidate.get("allowed"):
        reason = candidate.get("reason") if candidate else "unavailable"
        log(f"no trade: deterministic candidate rejected: {reason}")
        return None

    weighted = candidate.get("weighted") or {}
    weighted_score = float(weighted.get("score") or 0)
    if weighted_score < min_score:
        log(
            f"no trade: weighted score {weighted_score:.1f} is below "
            f"OVERNIGHT_GAP_MIN_SCORE={min_score:.1f}"
        )
        return None
    if weighted.get("grade") not in {"TRADE", "CAUTIOUS_TRADE"}:
        log(f"no trade: weighted grade is {weighted.get('grade')}")
        return None

    # build_trade_candidate is shared with the live engine and intentionally
    # returns only execution fields. Preserve the signal metadata needed by
    # this separate overnight state file.
    candidate.update(
        {
            "direction": direction,
            "confidence": confidence,
            "signal_score": score,
        }
    )
    option_summary = candidate.get("option_summary") or {}
    analysis = candidate.get("technicals") or {}
    analysis["overnight_gap"] = {
        "entry_score": weighted_score,
        "entry_grade": weighted.get("grade"),
        "entry_time": now_ist().isoformat(),
        "direction": direction,
    }
    record_analysis(
        "NIFTY_OVERNIGHT_GAP",
        option_summary,
        analysis,
        {
            "execute_trade": True,
            "decision": direction,
            "confidence": confidence,
            "target_price": None,
            "stop_loss_price": None,
            "reason": "Overnight gap candidate passed deterministic gates.",
        },
    )
    candidate["technicals"] = analysis
    return candidate


def regular_nifty_position_exists():
    """Avoid stacking this lane on an existing regular/manual NIFTY position."""
    for position in get_open_positions():
        key = position.get("instrument_token") or position.get("instrument_key") or ""
        if not str(key).startswith("NSE_FO|") or position_quantity(position) == 0:
            continue
        symbol = str(position.get("trading_symbol") or "").upper()
        if symbol.startswith("NIFTY ") and "BANKNIFTY" not in symbol:
            return True
    return False


def live_position(instrument_key):
    """Read positions uncached so entry/exit confirmation uses fresh broker data."""
    for position in get_open_positions(force=True):
        key = position.get("instrument_token") or position.get("instrument_key")
        if key == instrument_key and position_quantity(position) > 0:
            return position
    return None


def enter():
    if not env_bool("OVERNIGHT_GAP_ENABLED", False):
        log("disabled by OVERNIGHT_GAP_ENABLED")
        return False
    if not entry_window_ok():
        log("outside the 15:14-15:17 IST entry window")
        return False
    if read_state().get("instrument_key"):
        log("existing overnight state found; entry skipped")
        return False
    if regular_nifty_position_exists():
        log("regular/manual NIFTY derivatives position exists; entry skipped")
        return False

    candidate = build_candidate()
    if not candidate:
        return False

    instrument = candidate["instrument"]
    quantity = int(instrument.get("lot_size") or 0)
    if quantity <= 0:
        log("no trade: selected contract has no valid lot size")
        return False

    target_points, stop_points, delta = target_stop_points()
    entry_estimate = float(candidate["entry_price"])
    levels = premium_levels(entry_estimate, target_points, stop_points, delta)
    order_product = os.getenv("OVERNIGHT_GAP_PRODUCT", "D").strip().upper()
    if order_product not in {"D", "I"}:
        raise RuntimeError("OVERNIGHT_GAP_PRODUCT must be D or I")
    state = {
        "date": now_ist().strftime("%Y-%m-%d"),
        "symbol": "OVERNIGHT_GAP",
        "underlying_symbol": "NIFTY",
        "instrument_class": "OVERNIGHT_GAP",
        "instrument_key": instrument["instrument_key"],
        "trading_symbol": instrument["trading_symbol"],
        "direction": candidate["direction"],
        "option_type": (candidate.get("option_summary") or {}).get("option_type"),
        "quantity": quantity,
        "lot_size": quantity,
        "entry_price": entry_estimate,
        "target_price": levels["target_price"],
        "stop_loss_price": levels["stop_loss_price"],
        "original_stop_loss_price": levels["stop_loss_price"],
        "target_points": target_points,
        "stop_points": stop_points,
        "option_delta_used": delta,
        "confidence": candidate["confidence"],
        "score": candidate["signal_score"],
        "weighted_score": float((candidate.get("weighted") or {}).get("score") or 0),
        "entry_transaction_type": "BUY",
        "exit_transaction_type": "SELL",
        "order_product": order_product,
        "status": "PAPER_ONLY" if not live_trading_enabled() else "ENTRY_PENDING",
        "created_at": now_ist().isoformat(),
        "paper": not live_trading_enabled(),
    }

    log(
        f"candidate: {state['trading_symbol']} qty={quantity} direction={state['direction']} "
        f"entry={entry_estimate:.2f} target={state['target_price']:.2f} "
        f"stop_loss={state['stop_loss_price']:.2f} weighted={state['weighted_score']:.1f} "
        f"live={live_trading_enabled()}"
    )
    if not live_trading_enabled():
        log("paper only: no overnight order placed")
        return True

    with lane_lock():
        if read_state().get("instrument_key"):
            log("another overnight process created state; entry skipped")
            return False
        result, payload = place_market_order(instrument, "BUY", quantity, product=order_product)
        order_id = (result.get("data") or {}).get("order_id")
        if not order_id:
            raise RuntimeError(f"overnight BUY returned no order_id: {result}")
        state["entry_order_id"] = order_id
        state["buy_order_id"] = order_id
        write_state(state)

    log(f"MARKET BUY placed: order_id={order_id} payload={payload}")
    details = wait_for_order_complete(order_id)
    if order_is_rejected(details):
        log(f"BUY rejected: order_id={order_id} details={details}")
        clear_state()
        return False
    if not order_is_complete(details):
        state["status"] = "ENTRY_PENDING"
        write_state(state)
        log(f"BUY still pending; state retained: order_id={order_id} status={details.get('status')}")
        return True

    position = live_position(instrument["instrument_key"])
    fill = position_avg_price(position, "BUY") if position else None
    fill = fill or float(details.get("average_price") or 0) or entry_estimate
    state["entry_price"] = round(float(fill), 2)
    levels = premium_levels(fill, target_points, stop_points, delta)
    state.update(levels)
    state["original_stop_loss_price"] = levels["stop_loss_price"]
    state["status"] = "POSITION_OPEN"
    write_state(state)
    log(
        f"POSITION OPEN: {state['trading_symbol']} qty={quantity} entry={state['entry_price']} "
        f"target={state['target_price']} stop_loss={state['stop_loss_price']}"
    )
    return True


def exit_position():
    state = read_state()
    if not state.get("instrument_key"):
        log("no overnight position to exit")
        return False
    if state.get("paper"):
        log(f"paper exit: {state.get('trading_symbol')} entry={state.get('entry_price')}")
        clear_state()
        return True

    with lane_lock():
        state = read_state()
        if not state.get("instrument_key"):
            log("another process already cleared overnight state")
            return False
        position = live_position(state["instrument_key"])
        if not position:
            entry_order_id = state.get("entry_order_id")
            if entry_order_id:
                details = get_order_details(entry_order_id, force=True)
                if not order_is_complete(details):
                    log(f"entry is not complete at exit time: order_id={entry_order_id} status={details.get('status')}")
                    return False
            log("no matching broker position found; clearing stale overnight state")
            clear_state()
            return False

        quantity = abs(position_quantity(position))
        instrument = {
            "instrument_key": state["instrument_key"],
            "trading_symbol": state.get("trading_symbol"),
        }
        fallback = position_ltp(position) or state.get("entry_price")
        result, payload = place_market_order(
            instrument, "SELL", quantity, product=state.get("order_product", "D")
        )
        order_id = (result.get("data") or {}).get("order_id")
        if not order_id:
            raise RuntimeError(f"overnight SELL returned no order_id: {result}")
        state["exit_order_id"] = order_id
        state["exit_reason"] = "OVERNIGHT_EXIT"
        state["status"] = "EXIT_PENDING"
        write_state(state)

    log(f"MARKET SELL placed: order_id={order_id} payload={payload}")
    details = wait_for_order_complete(order_id)
    if order_is_complete(details):
        exit_price = float(details.get("average_price") or details.get("price") or fallback or 0)
        row = record_closed_trade(state, exit_price, "OVERNIGHT_EXIT")
        log(f"EXIT confirmed COMPLETE: journal={row}")
        clear_state()
        return True
    if order_is_rejected(details):
        state["status"] = "EXIT_PENDING"
        write_state(state)
        log(f"SELL rejected; state retained for retry: order_id={order_id} details={details}")
        return False
    log(f"SELL still pending; state retained: order_id={order_id} status={details.get('status')}")
    return True


def main():
    load_env()
    if "--exit" in sys.argv:
        exit_position()
    else:
        enter()


if __name__ == "__main__":
    main()
