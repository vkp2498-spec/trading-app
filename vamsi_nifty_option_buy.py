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
import time
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import trade_bot
import market_technicals
import llm_trade_judge
from institutional_flow import nearest_index_future
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
        minute=current.minute - current.minute % 15,
        second=0,
        microsecond=0,
    )
    return boundary.isoformat()


def entry_window_ok(current=None) -> bool:
    current = current or trade_bot.now_ist()
    first = trade_bot.configured_clock("NIFTY_OPTION_BUY_FIRST_ENTRY_TIME", "09:15")
    last = trade_bot.configured_clock("NIFTY_OPTION_BUY_LAST_ENTRY_TIME", "14:45")
    # The entire final scheduled minute is valid, not only 14:45:00 exactly.
    return current.weekday() < 5 and first <= current.time().replace(second=0, microsecond=0) <= last


def scan_due(current=None) -> bool:
    current = current or trade_bot.now_ist()
    return entry_window_ok(current) and current.minute % 15 == 0


def wait_for_candle_publication(current):
    """Keep scans on quarter hours while allowing completed candles to publish."""
    if not scan_due(current):
        return
    grace = configured_float("COMPLETED_CANDLE_GRACE_SECONDS", 8)
    if not 0 <= grace <= 30:
        raise RuntimeError("COMPLETED_CANDLE_GRACE_SECONDS must be between 0 and 30")
    delay = max(grace + 2 - current.second - current.microsecond / 1_000_000, 0)
    if delay:
        time.sleep(delay)


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
            "source": five.get("participation_source") or "UNDERLYING",
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
            "source": five.get("participation_source") or "UNDERLYING",
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
        "option_direction": "CALL" if direction == "BULLISH" else "PUT" if direction == "BEARISH" else "NONE",
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
    if direction not in {"BULLISH", "BEARISH"}:
        return {"allowed": False, "reason": "no directional target/stop plan for a neutral signal"}
    if entry <= 0 or atr <= 0:
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
            (None if five.get("target_is_fallback") else five.get("target"), "5M target"),
            (None if fifteen.get("target_is_fallback") else fifteen.get("target"), "15M target"),
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
            (None if five.get("target_is_fallback") else five.get("target"), "5M target"),
            (None if fifteen.get("target_is_fallback") else fifteen.get("target"), "15M target"),
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
            else (
                f"underlying reward/risk {reward_risk:.2f} is below {minimum_rr:.2f}; "
                f"nearest target {target_name}={target:.2f}, reward={reward:.2f}, risk={risk:.2f}"
            )
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


def evaluate_candidate(candidate: dict, current=None, *, check_contract=True) -> dict:
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

    blockers.extend(technicals.get("data_blockers") or [])
    if not plan.get("allowed"):
        blockers.append(plan.get("reason") or "underlying target/stop plan failed")
    market_result = {
        **signal, "allowed": not blockers, "blockers": blockers,
        "plan": plan, "regime": regime, "option_quality": quality,
        "five_minute": five, "fifteen_minute": fifteen,
    }
    if not check_contract:
        return market_result

    delta = abs(number(quality.get("delta")))
    spread = quality.get("spread_percent")
    limits = trade_bot.nifty_option_buy_contract_limits(quality.get("contract_role"))
    minimum_delta = limits["minimum_delta"]
    maximum_delta = limits["maximum_delta"]
    maximum_spread = limits["maximum_spread_percent"]
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
        cutoff = trade_bot.configured_clock(
            "NIFTY_OPTION_BUY_SAME_EXPIRY_ITM_LAST_ENTRY_TIME", "11:30"
        )
        if current.time() > cutoff:
            blockers.append("same-expiry ITM entry window ended at 11:30")
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
        DATA_DIR / "decision_details.csv",
        ("scan_time", "scan_slot", "action", "details"),
        {
            "scan_time": trade_bot.now_ist().isoformat(), "scan_slot": slot,
            "action": action,
            "details": json.dumps({
                "decision": decision,
                "participation": (candidate.get("technicals") or {}).get("participation"),
            }, default=str, sort_keys=True),
        },
    )
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


def candle_data_problem(analysis, minutes, current):
    """Validate a completed same-session candle, using its end time for age."""
    try:
        start = datetime.fromisoformat(str(analysis.get("candle_time")))
        if start.tzinfo is None:
            start = start.replace(tzinfo=current.tzinfo)
        age = (current - start).total_seconds() - minutes * 60
        if start.date() != current.date() or age < 0 or age > minutes * 60 + 90:
            return f"{minutes}M candle is stale or incomplete"
        if number(analysis.get("close")) <= 0:
            return f"{minutes}M close is unavailable"
    except (TypeError, ValueError):
        return f"{minutes}M candle timestamp is unavailable"
    return ""


def add_futures_participation(technicals, current):
    """Use traded futures for volume/VWAP, projecting VWAP into spot units."""
    future = nearest_index_future(SYMBOL)
    key = future["instrument_key"]
    history = market_technicals.fetch_v3_historical_minutes(key, minutes=5, lookback_days=5)
    intraday = market_technicals.fetch_v3_intraday_minutes(key, minutes=5)
    frame = market_technicals.completed_candles(
        market_technicals.merge_candles(history, intraday), 5,
        grace_seconds=configured_float("COMPLETED_CANDLE_GRACE_SECONDS", 8),
    )
    futures = market_technicals.analyze_latest(frame, "5M")
    problem = candle_data_problem(futures, 5, current)
    spot = technicals["five_min"]
    if problem or futures.get("candle_time") != spot.get("candle_time"):
        raise ValueError(problem or "futures and spot candles are not time-aligned")
    if (
        number(futures.get("volume")) <= 0
        or number(futures.get("volume_ma20")) <= 0
        or number(futures.get("vwap")) <= 0
    ):
        raise ValueError("futures volume or session VWAP is unavailable")
    basis = number(futures["close"]) - number(spot["close"])
    spot.update({
        "vwap": round(number(futures["vwap"]) - basis, 2),
        "vwap_bias": futures["vwap_bias"],
        "volume_ratio": futures["volume_ratio"],
        "participation_source": "NIFTY_FUTURES",
    })
    technicals["participation"] = {
        **future, "candle_time": futures["candle_time"],
        "futures_close": futures["close"], "futures_vwap": futures["vwap"],
        "futures_volume": futures["volume"], "volume_ratio": futures["volume_ratio"],
        "basis": round(basis, 2), "spot_equivalent_vwap": spot["vwap"],
    }


def market_candidate(recommendation, technicals, current):
    """Let completed 15M price structure choose direction; chain earns five points."""
    direction = (technicals.get("fifteen_min") or {}).get("bias") or "NEUTRAL"
    value = {
        "symbol": SYMBOL, "direction": direction, "transaction_type": "BUY",
        "technicals": deepcopy(technicals),
        "option_summary": {
            "chain_bias": recommendation.get("direction"),
            "chain_confidence": recommendation.get("confidence"),
            "option_type": "CE" if direction == "BULLISH" else "PE",
        },
    }
    t = value["technicals"]
    t["entry_structure"] = trade_bot.entry_structure_for_direction(
        t, direction, retest_buffer_atr=configured_float("ENTRY_RETEST_BUFFER_ATR", 0.25)
    )
    t["market_regime"] = trade_bot.classify_market_regime(
        t, compression_width_percent=configured_float("REGIME_COMPRESSION_BB_WIDTH_PERCENT", 0.18),
        extreme_atr_percent=configured_float("NIFTY_REGIME_EXTREME_ATR_PERCENT", 0.35),
    )
    return value


def contract_candidate(base, row, current):
    value = deepcopy(base)
    option_type = value["option_summary"]["option_type"]
    instrument = trade_bot.find_index_option_instrument(
        SYMBOL, row["expiry"], row["strike"], option_type
    )
    # All contract fields come from the same fresh REST chain snapshot. A stream
    # receipt timestamp alone cannot establish that its Greeks are current.
    quality = trade_bot.option_contract_quality(row, option_type)
    quality["contract_role"] = row["_contract_selection_role"]
    quality["entry_allowed"] = True
    quote_problems = []
    fatal_problems = []
    bid, ask = number(quality.get("bid_price")), number(quality.get("ask_price"))
    if bid <= 0 or ask < bid or number(quality.get("ltp")) <= 0:
        quote_problems.append("positive uncrossed bid/ask and LTP required")
    if min(number(quality.get("bid_qty")), number(quality.get("ask_qty"))) <= 0:
        quote_problems.append("two-sided option depth is unavailable")
    try:
        stamp = datetime.fromisoformat(str(row.get("timestamp")))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=current.tzinfo)
        age = (current - stamp).total_seconds()
        if not 0 <= age <= 30:
            fatal_problems.append("option-chain snapshot is stale")
    except (TypeError, ValueError):
        fatal_problems.append("option-chain timestamp is unavailable")
    delta = number(quality.get("delta"))
    signed_delta = delta if option_type == "CE" else -delta
    if not 0 < signed_delta <= 1 or number(quality.get("iv")) <= 0:
        fatal_problems.append("option Greeks are missing or invalid")
    quote_problems.extend(fatal_problems)
    quality["data_problems"] = quote_problems
    quality["fatal_data_problems"] = fatal_problems
    quality["entry_allowed"] = not quote_problems
    value.update(instrument=instrument, entry_price=ask)
    value["option_summary"].update({
        "strike": row["strike"], "expiry": row["expiry"],
        "trading_symbol": instrument["trading_symbol"],
        "option_market_quality": quality,
        "contract_selection_role": row["_contract_selection_role"],
    })
    value["technicals"]["market_regime"]["days_to_expiry"] = (
        trade_bot.parse_expiry(row["expiry"]) - current.date()
    ).days
    return value


def select_complete_contract(base, recommendation, current):
    """Apply every final gate before choosing a tier; refresh bad data once."""
    attempts = []
    rows = trade_bot.index_contract_rows(recommendation, base["direction"], current=current)
    selected = base
    decision = evaluate_candidate(base, current, check_contract=False)
    if not decision["allowed"]:
        return base, decision
    for row in rows:
        try:
            current = trade_bot.now_ist()
            selected = contract_candidate(base, row, current)
            if selected["option_summary"]["option_market_quality"]["data_problems"]:
                atm, nearby, _ = trade_bot.fetch_upstox_option_chain(
                    SYMBOL, nearby=5, expiry=row["expiry"]
                )
                refreshed = atm.iloc[0].to_dict()
                if row["_contract_selection_role"] != "NEXT_EXPIRY_ATM":
                    refreshed = trade_bot._nearest_itm_contract(
                        nearby.to_dict("records"), number(refreshed["strike"]),
                        row["expiry"], base["direction"],
                    )
                if not refreshed:
                    raise ValueError("one-strike ITM unavailable after refresh")
                row = trade_bot._contract_ladder_row(
                    refreshed, row["_contract_selection_role"], row["_contract_selection_tier"]
                )
                current = trade_bot.now_ist()
                selected = contract_candidate(base, row, current)
            quality = selected["option_summary"]["option_market_quality"]
            decision = evaluate_candidate(selected, current)
            attempts.append({
                "role": row["_contract_selection_role"], "strike": row["strike"],
                "expiry": row["expiry"], "blockers": decision["blockers"] + quality["data_problems"],
            })
            if decision["allowed"]:
                decision["contract_attempts"] = attempts
                return selected, decision
            if quality["fatal_data_problems"]:
                # Missing data must not send the account into a riskier expiry.
                decision["blockers"].extend(quality["data_problems"])
                break
        except Exception as error:
            decision = evaluate_candidate(base, current, check_contract=False)
            decision.update(allowed=False, blockers=[f"contract data unavailable: {error}"])
            attempts.append({"role": row["_contract_selection_role"], "blockers": decision["blockers"]})
            break
    decision["allowed"] = False
    decision["contract_attempts"] = attempts
    decision["blockers"] = [
        f"{attempt['role']}: {'; '.join(attempt['blockers'])}" for attempt in attempts
    ] or ["no execution contracts available"]
    return selected, decision


def collect_candidate(current):
    recommendation = trade_bot.get_index_recommendation(SYMBOL)
    trade_bot.record_option_chain_snapshot(SYMBOL, recommendation)
    trade_bot.ensure_instruments_file()
    technicals = trade_bot.get_technical_analysis(SYMBOL)
    technicals["data_blockers"] = [
        problem for name, minutes in (("five_min", 5), ("fifteen_min", 15))
        if (problem := candle_data_problem(technicals.get(name) or {}, minutes, current))
    ]
    try:
        add_futures_participation(technicals, current)
    except Exception as error:
        technicals["data_blockers"].append(f"NIFTY futures participation unavailable: {error}")
    technicals["nifty_breadth"] = trade_bot.get_nifty_breadth(
        trade_bot.INSTRUMENT_CACHE, trade_bot.upstox_request
    )
    base = market_candidate(recommendation, technicals, current)
    return select_complete_contract(base, recommendation, trade_bot.now_ist())


def live_entry_block_reason() -> str:
    """Block overlapping capital use while allowing unlimited sequential entries."""
    if trade_bot.state_is_active(trade_bot.read_state(STATE_SLOT)):
        return "NIFTY bot position is already active"
    return trade_bot.daily_index_entry_block_reason(SYMBOL) or ""


def judge_entry(candidate, decision):
    """Veto-only judge outside the position monitor/entry lock; refresh after PASS."""
    current = trade_bot.now_ist()
    evidence = llm_trade_judge.snapshot(candidate, decision, current)
    review = llm_trade_judge.review(evidence)
    decision["llm_judge"] = review
    locked_append_csv(DATA_DIR / "llm_judge.csv", ("time", "evidence", "review"), {
        "time": current.isoformat(), "evidence": json.dumps(evidence), "review": json.dumps(review),
    })
    if review["verdict"] != "PASS":
        return candidate, decision, f"LLM {review['verdict']}: {review['reason']}"
    try:
        # Never switch strikes/expiry after the model approves a specific contract.
        summary = candidate["option_summary"]
        atm, nearby, _ = trade_bot.fetch_upstox_option_chain(SYMBOL, nearby=5, expiry=summary["expiry"])
        rows = nearby.to_dict("records") + atm.to_dict("records")
        row = next(r for r in rows if number(r.get("strike")) == number(summary["strike"]))
        row = trade_bot._contract_ladder_row(row, summary["contract_selection_role"], 0)
        refreshed_at = trade_bot.now_ist()
        if not entry_window_ok(refreshed_at) or time.time() - review["started_at_epoch"] > 40:
            raise ValueError("entry window/review freshness expired")
        refreshed = contract_candidate(candidate, row, refreshed_at)
        if refreshed["instrument"]["instrument_key"] != candidate["instrument"]["instrument_key"]:
            raise ValueError("approved instrument changed")
        fresh_decision = evaluate_candidate(refreshed, refreshed_at)
        if not fresh_decision["allowed"]:
            return refreshed, {**fresh_decision, "llm_judge": review}, "post-judge safety check: " + "; ".join(fresh_decision["blockers"])
        old_ask, new_ask = number(candidate["entry_price"]), number(refreshed["entry_price"])
        spot = number(row.get("spot"))
        plan = fresh_decision["plan"]
        sign = 1 if candidate["direction"] == "BULLISH" else -1
        reward, risk = sign * (plan["target"] - spot), sign * (spot - plan["stop"])
        if (spot <= 0 or old_ask <= 0 or abs(new_ask / old_ask - 1) > 0.02
                or abs(spot - plan["entry"]) > 0.25 * plan["atr"]
                or risk <= 0 or reward / risk < configured_float("NIFTY_OPTION_BUY_MIN_REWARD_RISK", 1.5)):
            raise ValueError("price moved materially or reward/risk no longer qualifies")
        # Keep approved absolute underlying target/stop, rebase distances to the
        # refreshed underlying entry instead of using the pre-review candle close.
        plan.update(entry=spot, target_points=round(reward, 2), stop_points=round(risk, 2), reward_risk=round(reward / risk, 3))
        fresh_decision["llm_judge"] = review
        return refreshed, fresh_decision, ""
    except Exception:
        return candidate, decision, "post-judge quote refresh/freshness check failed; entry skipped"


def scan() -> dict:
    trade_bot.load_env()
    if trade_bot.trading_engine() != ENGINE:
        raise RuntimeError(f"TRADING_ENGINE must be {ENGINE}")
    current = trade_bot.now_ist()
    if not scan_due(current):
        log("scan skipped: quarter-hour scans only, 09:15 through 14:45 IST")
        return {"action": "OUTSIDE_SCAN_SCHEDULE"}

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

        if (current.hour, current.minute) == (9, 15):
            decision = {"allowed": False, "blockers": [
                "waiting for today's first completed 15M candle; first entry assessment at 09:30"
            ]}
            record_scan(slot, "WAITING_FOR_CANDLE", decision)
            log(decision["blockers"][0])
            return {"action": "WAITING_FOR_CANDLE", "scan_slot": slot, **decision}

        entry_block = live_entry_block_reason()

        try:
            candidate, decision = collect_candidate(current)
        except Exception as error:
            decision = {"allowed": False, "blockers": [f"market scan unavailable: {error}"]}
            record_scan(slot, "ERROR", decision)
            log(decision["blockers"][0])
            return {"action": "ERROR", "scan_slot": slot, **decision}
        if not candidate:
            record_scan(slot, "NO_CANDIDATE")
            log("no complete NIFTY CE/PE candidate was available")
            return {"action": "NO_CANDIDATE", "scan_slot": slot}

        entry_block = entry_block or trade_bot.reentry_block_reason(SYMBOL, candidate["direction"])

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

        if llm_trade_judge.enabled():
            candidate, decision, judge_block = judge_entry(candidate, decision)
            if judge_block:
                decision.update(allowed=False, blockers=[judge_block])
                record_scan(slot, "LLM_REJECT", decision, candidate)
                log(judge_block)
                return {"action": "LLM_REJECT", "scan_slot": slot, **decision}
        prepared = prepare_candidate(candidate, decision)
        if llm_trade_judge.enabled():
            prepared["llm_judge"] = {**decision["llm_judge"], "binding": llm_trade_judge.binding(prepared)}
        record_scan(slot, "ENTRY_SELECTED", decision, prepared)
        plan = decision["plan"]
        log(
            f"{decision['option_direction']} selected score={decision['score']:+.1f} "
            f"underlying={plan['entry']:.2f} target={plan['target']:.2f} "
            f"stop={plan['stop']:.2f} rr={plan['reward_risk']:.2f} "
            f"contract={(prepared.get('instrument') or {}).get('trading_symbol')} "
            f"tier={decision['option_quality'].get('contract_role')} MAX"
        )
        placed = trade_bot.execute_selected_candidate(prepared)
        action = "LIVE_ENTRY" if placed else "EXECUTION_REJECT"
        if not placed:
            decision["blockers"] = [prepared.get("execution_rejection_reason") or "execution declined; see sizing/broker log"]
        record_scan(slot, action, decision, prepared)
        return {"action": action, "scan_slot": slot, **decision}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true")
    args = parser.parse_args()
    if not args.scan:
        parser.error("choose --scan")
    trade_bot.load_env()
    wait_for_candle_publication(trade_bot.now_ist())
    scan()


if __name__ == "__main__":
    main()
