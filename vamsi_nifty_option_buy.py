#!/usr/bin/env python3
"""Selective NIFTY intraday option buyer driven by the underlying index.

The option chain never chooses direction on its own.  A completed 15-minute
trend and completed 5-minute entry structure lead the decision; VWAP, key
levels, volume, NIFTY breadth, and the option chain contribute a transparent
100-point alignment score.  Entries additionally require sufficient realised
volatility, a liquid ATM/one-strike-ITM contract, a structure/ATR stop, and a
nearby underlying target offering the configured reward/risk.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import trade_bot
from safe_storage import atomic_write_json, file_lock, locked_append_csv


ENGINE = "VAMSI_NIFTY_OPTION_BUY_V1"
SYMBOL = "NIFTY"
STATE_SLOT = "NIFTY"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "vamsi_nifty_option_buy"
SCAN_STATE_FILE = DATA_DIR / "scan_state.json"
SCAN_FILE = DATA_DIR / "scans.csv"
SCAN_LOCK_FILE = BASE_DIR / ".vamsi_nifty_option_buy.lock"
SCAN_COLUMNS = (
    "scan_time",
    "scan_slot",
    "action",
    "direction",
    "signed_score",
    "contract",
    "entry_price",
    "underlying_entry",
    "underlying_target",
    "underlying_stop",
    "reward_risk",
    "target_points",
    "stop_points",
    "blockers",
    "components",
)


def log(message: str) -> None:
    trade_bot.log(f"{ENGINE} | {message}")


def number(value, default=0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except (TypeError, ValueError):
        return float(default)


def configured_float(name: str, default: float) -> float:
    return number(os.getenv(name), default)


def configured_bool(name: str, default=False) -> bool:
    raw = os.getenv(name)
    return bool(default) if raw is None else raw.strip().lower() in {
        "1", "true", "yes", "on",
    }


def opposite(direction: str) -> str:
    return "BEARISH" if direction == "BULLISH" else "BULLISH"


def completed_scan_slot(current=None) -> str:
    current = current or trade_bot.now_ist()
    boundary = current.replace(
        minute=current.minute - current.minute % 5,
        second=0,
        microsecond=0,
    )
    return boundary.isoformat()


def entry_window_ok(current=None) -> bool:
    current = current or trade_bot.now_ist()
    first = trade_bot.configured_clock("NIFTY_OPTION_BUY_FIRST_ENTRY_TIME", "09:30")
    last = trade_bot.configured_clock("NIFTY_OPTION_BUY_LAST_ENTRY_TIME", "14:30")
    return first <= current.time() <= last


def _aligned(value, direction: str) -> bool:
    return str(value or "NEUTRAL").upper() == direction


def weighted_signal(candidate: dict) -> dict:
    """Return the documented 100-point NIFTY-first alignment score."""
    direction = str(candidate.get("direction") or "NEUTRAL").upper()
    technicals = candidate.get("technicals") or {}
    five = technicals.get("five_min") or {}
    fifteen = technicals.get("fifteen_min") or {}
    structure = technicals.get("entry_structure") or {}
    breadth = technicals.get("nifty_breadth") or {}
    summary = candidate.get("option_summary") or {}

    components = {
        "15_min_trend": {
            "weight": 25.0,
            "earned": 25.0 if _aligned(fifteen.get("bias"), direction) else 0.0,
            "detail": fifteen.get("bias") or "UNAVAILABLE",
        },
        "5_min_structure": {
            "weight": 20.0,
            "earned": 20.0 if bool(structure.get("qualified")) else 0.0,
            "detail": structure.get("type") or "NONE",
        },
        "vwap": {
            "weight": 15.0,
            "earned": 15.0 if _aligned(five.get("vwap_bias"), direction) else 0.0,
            "detail": five.get("vwap_bias") or "UNAVAILABLE",
        },
        "key_levels": {
            "weight": 15.0,
            "earned": 15.0 if structure.get("reference") is not None else 0.0,
            "detail": structure.get("reference"),
        },
        "volume": {
            "weight": 10.0,
            "earned": 10.0
            if number(five.get("volume_ratio"))
            >= configured_float("NIFTY_OPTION_BUY_MIN_VOLUME_RATIO", 1.5)
            else 0.0,
            "detail": number(five.get("volume_ratio")),
        },
        "breadth": {
            "weight": 10.0,
            "earned": (
                10.0 if _aligned(breadth.get("bias"), direction)
                else 5.0 if str(breadth.get("bias") or "NEUTRAL").upper() == "NEUTRAL"
                else 0.0
            ),
            "detail": breadth.get("bias") or "UNAVAILABLE",
        },
        "option_chain": {
            "weight": 5.0,
            "earned": (
                5.0 if _aligned(summary.get("chain_bias"), direction)
                and str(summary.get("chain_confidence") or "LOW").upper()
                in {"MEDIUM", "HIGH"}
                else 2.5
                if str(summary.get("chain_bias") or "NEUTRAL").upper() == "NEUTRAL"
                else 0.0
            ),
            "detail": (
                f"{summary.get('chain_bias') or 'UNAVAILABLE'}/"
                f"{summary.get('chain_confidence') or 'LOW'}"
            ),
        },
    }
    magnitude = round(sum(item["earned"] for item in components.values()), 1)
    signed = (
        magnitude if direction == "BULLISH"
        else -magnitude if direction == "BEARISH"
        else 0.0
    )
    return {
        "direction": direction,
        "option_direction": "CALL" if direction == "BULLISH" else "PUT",
        "score": signed,
        "magnitude": magnitude,
        "components": components,
    }


def underlying_trade_plan(candidate: dict) -> dict:
    """Choose a structure/ATR stop and the next nearby underlying level."""
    direction = str(candidate.get("direction") or "").upper()
    technicals = candidate.get("technicals") or {}
    five = technicals.get("five_min") or {}
    fifteen = technicals.get("fifteen_min") or {}
    entry = number(five.get("close"))
    atr = number(five.get("atr14"))
    if direction not in {"BULLISH", "BEARISH"} or entry <= 0 or atr <= 0:
        return {"allowed": False, "reason": "underlying close or 5M ATR is unavailable"}

    if direction == "BULLISH":
        stop_candidates = [
            (five.get("recent_swing_low"), "5M swing low"),
            (five.get("pivot"), "5M pivot"),
            (five.get("vwap"), "VWAP"),
            (five.get("middle_band"), "5M middle band"),
        ]
        target_candidates = [
            (five.get("recent_swing_high"), "5M swing high"),
            (fifteen.get("recent_swing_high"), "15M swing high"),
            (five.get("upper_band"), "5M upper band"),
            (fifteen.get("upper_band"), "15M upper band"),
            (five.get("target"), "5M target"),
            (fifteen.get("target"), "15M target"),
        ]
        valid_stops = [(number(v), n) for v, n in stop_candidates if 0 < number(v) < entry]
        valid_targets = [(number(v), n) for v, n in target_candidates if number(v) > entry]
        if not valid_stops or not valid_targets:
            return {"allowed": False, "reason": "nearby bullish stop/target levels are unavailable"}
        reference_stop, stop_name = max(valid_stops)
        target, target_name = min(valid_targets)
        raw_risk = entry - reference_stop
    else:
        stop_candidates = [
            (five.get("recent_swing_high"), "5M swing high"),
            (five.get("pivot"), "5M pivot"),
            (five.get("vwap"), "VWAP"),
            (five.get("middle_band"), "5M middle band"),
        ]
        target_candidates = [
            (five.get("recent_swing_low"), "5M swing low"),
            (fifteen.get("recent_swing_low"), "15M swing low"),
            (five.get("lower_band"), "5M lower band"),
            (fifteen.get("lower_band"), "15M lower band"),
            (five.get("target"), "5M target"),
            (fifteen.get("target"), "15M target"),
        ]
        valid_stops = [(number(v), n) for v, n in stop_candidates if number(v) > entry]
        valid_targets = [(number(v), n) for v, n in target_candidates if 0 < number(v) < entry]
        if not valid_stops or not valid_targets:
            return {"allowed": False, "reason": "nearby bearish stop/target levels are unavailable"}
        reference_stop, stop_name = min(valid_stops)
        target, target_name = max(valid_targets)
        raw_risk = reference_stop - entry

    minimum_risk = atr * configured_float("NIFTY_OPTION_BUY_MIN_STOP_ATR", 0.5)
    maximum_risk = atr * configured_float("NIFTY_OPTION_BUY_MAX_STOP_ATR", 1.0)
    if raw_risk > maximum_risk:
        return {
            "allowed": False,
            "reason": (
                f"structure stop is too far: {raw_risk:.1f} points exceeds "
                f"{maximum_risk:.1f} ({maximum_risk / atr:.2f} ATR)"
            ),
        }
    risk = max(raw_risk, minimum_risk)
    stop = entry - risk if direction == "BULLISH" else entry + risk
    reward = target - entry if direction == "BULLISH" else entry - target
    reward_risk = reward / risk if risk > 0 else 0.0
    minimum_rr = configured_float("NIFTY_OPTION_BUY_MIN_REWARD_RISK", 1.5)
    return {
        "allowed": reward_risk >= minimum_rr,
        "reason": (
            "underlying target and structure/ATR stop qualify"
            if reward_risk >= minimum_rr
            else f"underlying reward/risk {reward_risk:.2f} is below {minimum_rr:.2f}"
        ),
        "entry": round(entry, 2),
        "target": round(target, 2),
        "stop": round(stop, 2),
        "target_name": target_name,
        "stop_name": stop_name,
        "stop_reference": round(reference_stop, 2),
        "target_points": round(reward, 2),
        "stop_points": round(risk, 2),
        "atr": round(atr, 2),
        "reward_risk": round(reward_risk, 3),
    }


def evaluate_candidate(candidate: dict, current=None) -> dict:
    current = current or trade_bot.now_ist()
    signal = weighted_signal(candidate)
    direction = signal["direction"]
    technicals = candidate.get("technicals") or {}
    five = technicals.get("five_min") or {}
    fifteen = technicals.get("fifteen_min") or {}
    structure = technicals.get("entry_structure") or {}
    regime = technicals.get("market_regime") or {}
    quality = (candidate.get("option_summary") or {}).get("option_market_quality") or {}
    plan = underlying_trade_plan(candidate)
    blockers = []

    if direction not in {"BULLISH", "BEARISH"}:
        blockers.append("NIFTY direction is neutral")
    if not _aligned(fifteen.get("bias"), direction):
        blockers.append("completed 15M trend does not control the proposed direction")
    if not structure.get("qualified"):
        blockers.append("no completed 5M breakout/retest/pullback/continuation entry")
    minimum_score = configured_float("NIFTY_OPTION_BUY_MIN_SCORE", 60.0)
    if signal["magnitude"] < minimum_score:
        blockers.append(
            f"weighted alignment {signal['magnitude']:.1f} is below {minimum_score:.1f}"
        )

    atr_percent = number(regime.get("atr_percent"))
    if regime.get("regime") in {"COMPRESSION", "EXTREME_VOLATILITY"}:
        blockers.append(f"{regime.get('regime')} regime is unsuitable for option buying")
    minimum_atr_percent = configured_float("NIFTY_OPTION_BUY_MIN_ATR_PERCENT", 0.04)
    if atr_percent < minimum_atr_percent:
        blockers.append(
            f"realised volatility {atr_percent:.3f}% is below {minimum_atr_percent:.3f}%"
        )

    delta = abs(number(quality.get("delta")))
    spread = quality.get("spread_percent")
    minimum_delta = number(
        quality.get("minimum_delta"),
        configured_float("NIFTY_OPTION_BUY_MIN_DELTA", 0.45),
    )
    maximum_delta = number(
        quality.get("maximum_delta"),
        configured_float("NIFTY_OPTION_BUY_MAX_DELTA", 0.65),
    )
    maximum_spread = number(
        quality.get("max_spread_percent"),
        configured_float("NIFTY_OPTION_BUY_MAX_SPREAD_PERCENT", 2.0),
    )
    if not minimum_delta <= delta <= maximum_delta:
        blockers.append(
            f"option delta {delta:.3f} is outside {minimum_delta:.2f}-{maximum_delta:.2f}"
        )
    if spread is None or number(spread, 999) > maximum_spread:
        blockers.append(f"option spread is unavailable or above {maximum_spread:.2f}%")
    if number(quality.get("ltp"), candidate.get("entry_price")) <= 0:
        blockers.append("option quote is not executable")
    if quality.get("entry_allowed") is False:
        blockers.append("option contract quality rejected execution")
    if str(quality.get("contract_role") or "").upper() == "SAME_EXPIRY_ITM":
        lot_size = max(int(number((candidate.get("instrument") or {}).get("lot_size"), 1)), 1)
        if (
            number(quality.get("bid_qty")) < lot_size
            or number(quality.get("ask_qty")) < lot_size
        ):
            blockers.append("same-expiry ITM depth is below one complete lot")
    expected_option_type = "CE" if direction == "BULLISH" else "PE"
    actual_option_type = str(
        (candidate.get("option_summary") or {}).get("option_type") or ""
    ).upper()
    if direction in {"BULLISH", "BEARISH"} and actual_option_type != expected_option_type:
        blockers.append(
            f"contract {actual_option_type or 'UNKNOWN'} does not express {direction}"
        )
    maximum_iv = configured_float("NIFTY_OPTION_BUY_MAX_IV", 35.0)
    if quality.get("iv") is not None and number(quality.get("iv")) > maximum_iv:
        blockers.append(f"option IV {number(quality.get('iv')):.2f} exceeds {maximum_iv:.2f}")

    if not plan.get("allowed"):
        blockers.append(plan.get("reason") or "underlying target/stop plan failed")

    days_to_expiry = int(number(regime.get("days_to_expiry"), 99))
    expiry_cutoff = trade_bot.configured_clock(
        "NIFTY_OPTION_BUY_EXPIRY_DAY_LAST_ENTRY_TIME", "13:00"
    )
    if days_to_expiry == 0 and current.time() > expiry_cutoff:
        blockers.append("late expiry-day option buying is disabled")
    expiry_score = configured_float("NIFTY_OPTION_BUY_EXPIRY_DAY_MIN_SCORE", 70.0)
    if days_to_expiry == 0 and signal["magnitude"] < expiry_score:
        blockers.append(
            f"expiry-day alignment {signal['magnitude']:.1f} is below {expiry_score:.1f}"
        )

    return {
        **signal,
        "allowed": not blockers,
        "blockers": blockers,
        "plan": plan,
        "regime": regime,
        "option_quality": quality,
        "five_minute": five,
        "fifteen_minute": fifteen,
    }


def prepare_candidate(candidate: dict, decision: dict) -> dict:
    prepared = deepcopy(candidate)
    plan = decision["plan"]
    quality = decision["option_quality"]
    delta = abs(number(quality.get("delta"), 0.5))
    levels = trade_bot.option_levels_from_index_points(
        SYMBOL,
        prepared["entry_price"],
        target_points=plan["target_points"],
        stop_points=plan["stop_points"],
        delta=delta,
    )
    prepared.update(
        {
            "allowed": True,
            "strategy": ENGINE,
            "symbol": SYMBOL,
            "direction": decision["direction"],
            "confidence": "HIGH" if decision["magnitude"] >= 75 else "MEDIUM",
            "signal_score": decision["magnitude"],
            "target_price": levels["target_price"],
            "stop_loss_price": levels["stop_loss_price"],
            "target_points": plan["target_points"],
            "stop_points": plan["stop_points"],
            "option_delta_used": delta,
            "target_percent": None,
            "stop_percent": None,
            "target_profile": "NIFTY_UNDERLYING_STRUCTURE_ATR",
            "profit_protection_enabled_for_trade": True,
            "score_cutoff_approved": True,
            "entry_minimum_score": configured_float("NIFTY_OPTION_BUY_MIN_SCORE", 60.0),
            "entry_maximum_score": None,
            "score_rule_source": ENGINE,
            "capital_override": "MAX",
            "structural_invalidation": {
                "entry_underlying": plan["entry"],
                "stop_underlying": plan["stop"],
                "reference": plan["stop_name"],
                "reference_value": plan["stop_reference"],
                "atr": plan["atr"],
            },
            "entry_score": {
                "score": decision["magnitude"],
                "score_version": ENGINE,
                "score_kind": "NIFTY_UNDERLYING_WEIGHTED_ALIGNMENT",
                "probability_calibrated": False,
                "components": {
                    name: value["earned"] for name, value in decision["components"].items()
                },
            },
            "knowledge_decision": decision,
        }
    )
    prepared.setdefault("option_summary", {})["strategy"] = ENGINE
    return prepared


def record_scan(slot: str, action: str, decision=None, candidate=None) -> None:
    decision = decision or {}
    candidate = candidate or {}
    plan = decision.get("plan") or {}
    instrument = candidate.get("instrument") or {}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    locked_append_csv(
        SCAN_FILE,
        SCAN_COLUMNS,
        {
            "scan_time": trade_bot.now_ist().isoformat(),
            "scan_slot": slot,
            "action": action,
            "direction": decision.get("direction") or candidate.get("direction") or "",
            "signed_score": decision.get("score", ""),
            "contract": instrument.get("trading_symbol") or "",
            "entry_price": candidate.get("entry_price") or "",
            "underlying_entry": plan.get("entry", ""),
            "underlying_target": plan.get("target", ""),
            "underlying_stop": plan.get("stop", ""),
            "reward_risk": plan.get("reward_risk", ""),
            "target_points": plan.get("target_points", ""),
            "stop_points": plan.get("stop_points", ""),
            "blockers": " | ".join(decision.get("blockers") or []),
            "components": json.dumps(decision.get("components") or {}, sort_keys=True),
        },
    )


def live_entry_block_reason() -> str:
    """Block overlapping capital use while allowing unlimited sequential entries."""
    if trade_bot.state_is_active(trade_bot.read_state(STATE_SLOT)):
        return "NIFTY bot position is already active"
    return trade_bot.daily_index_entry_block_reason(SYMBOL) or ""


def scan() -> dict:
    trade_bot.load_env()
    if trade_bot.trading_engine() != ENGINE:
        raise RuntimeError(f"TRADING_ENGINE must be {ENGINE}")
    current = trade_bot.now_ist()
    if not entry_window_ok(current):
        raise RuntimeError("Outside NIFTY option-buying entry window")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    slot = completed_scan_slot(current)
    with file_lock(SCAN_LOCK_FILE):
        state = trade_bot.read_json(SCAN_STATE_FILE, {})
        if state.get("date") == current.date().isoformat() and state.get("slot") == slot:
            log(f"duplicate completed-candle scan skipped: {slot}")
            return {"action": "DUPLICATE", "scan_slot": slot}
        atomic_write_json(
            SCAN_STATE_FILE,
            {"date": current.date().isoformat(), "slot": slot, "claimed_at": current.isoformat()},
            sort_keys=True,
        )

        entry_block = live_entry_block_reason()

        try:
            candidate = trade_bot.evaluate_symbol_buy_or_sell(
                SYMBOL,
                allow_option_sell=False,
                include_rejected=True,
                paper_observation=True,
            )
        except Exception as error:
            decision = {"allowed": False, "blockers": [f"market scan unavailable: {error}"]}
            record_scan(slot, "ERROR", decision)
            log(decision["blockers"][0])
            return {"action": "ERROR", "scan_slot": slot, **decision}
        if not candidate:
            record_scan(slot, "NO_CANDIDATE")
            log("no complete NIFTY CE/PE candidate was available")
            return {"action": "NO_CANDIDATE", "scan_slot": slot}

        decision = evaluate_candidate(candidate, current=current)
        if not decision["allowed"]:
            record_scan(slot, "REJECT", decision, candidate)
            log(
                f"{decision['option_direction']} reject score={decision['score']:+.1f}: "
                + "; ".join(decision["blockers"][:3])
            )
            return {"action": "REJECT", "scan_slot": slot, **decision}
        if entry_block:
            observed = deepcopy(decision)
            observed["blockers"] = [entry_block]
            record_scan(slot, "QUALIFIED_OBSERVATION", observed, candidate)
            log(f"qualified {decision['option_direction']} blocked: {entry_block}")
            return {
                "action": "QUALIFIED_OBSERVATION",
                "scan_slot": slot,
                **observed,
            }

        prepared = prepare_candidate(candidate, decision)
        record_scan(slot, "ENTRY_SELECTED", decision, prepared)
        plan = decision["plan"]
        log(
            f"{decision['option_direction']} selected score={decision['score']:+.1f} "
            f"underlying={plan['entry']:.2f} target={plan['target']:.2f} "
            f"stop={plan['stop']:.2f} rr={plan['reward_risk']:.2f} "
            f"contract={(prepared.get('instrument') or {}).get('trading_symbol')} MAX"
        )
        placed = trade_bot.execute_selected_candidate(prepared)
        action = "LIVE_ENTRY" if placed else "EXECUTION_REJECT"
        record_scan(slot, action, decision, prepared)
        return {"action": action, "scan_slot": slot, **decision}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true")
    args = parser.parse_args()
    if not args.scan:
        parser.error("choose --scan")
    scan()


if __name__ == "__main__":
    main()
