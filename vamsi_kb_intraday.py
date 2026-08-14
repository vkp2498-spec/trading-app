"""Deterministic NIFTY option-buying engine built from independent evidence.

The engine deliberately does not treat a weighted score as a probability.  A
live entry needs every evidence family to pass: completed-candle structure,
market regime, NIFTY breadth, option-chain direction, bought-option
VWAP/volume, and executable contract quality.  It reuses ``trade_bot`` only
for broker reconciliation, capital sizing, order placement, protection,
monitoring, and idempotent finalization.
"""

from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from safe_storage import atomic_write_json, file_lock, locked_append_csv
import trade_bot


ENGINE = "VAMSI_KB_INTRADAY_V1"
SCORE_VERSION = "VAMSI_KB_INTRADAY_V1_ALL_GATES"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "vamsi_kb_intraday"
SCAN_STATE_FILE = DATA_DIR / "scan_state.json"
SCAN_FILE = DATA_DIR / "scans.csv"
SCAN_LOCK_FILE = BASE_DIR / ".vamsi_kb_scan.lock"
SCAN_COLUMNS = [
    "scan_time",
    "scan_slot",
    "action",
    "direction",
    "setup",
    "knowledge_score",
    "instrument",
    "entry_price",
    "target_price",
    "stop_loss_price",
    "quantity",
    "blockers",
    "evidence",
]


def log(message: str) -> None:
    trade_bot.log(f"{ENGINE} | {message}")


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _configured_bool(name: str, default=False) -> bool:
    raw = os.getenv(name)
    return bool(default) if raw is None else raw.strip().lower() in {
        "1", "true", "yes", "on",
    }


def _configured_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be numeric") from error


def _opposite(direction: str) -> str:
    return "BEARISH" if direction == "BULLISH" else "BULLISH"


def _confidence_rank(value) -> int:
    return {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(str(value or "").upper(), 0)


def _read_scan_state() -> dict:
    try:
        value = json.loads(SCAN_STATE_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def completed_scan_slot(current=None) -> str:
    """Return the most recent completed five-minute boundary in IST."""
    current = current or trade_bot.now_ist()
    boundary = current.replace(
        minute=current.minute - current.minute % 5,
        second=0,
        microsecond=0,
    )
    grace = max(_configured_float("VAMSI_KB_CANDLE_GRACE_SECONDS", 8.0), 0.0)
    if current < boundary + timedelta(seconds=grace):
        boundary -= timedelta(minutes=5)
    return boundary.isoformat()


def _candle_is_fresh(five: dict, current=None) -> bool:
    raw = five.get("candle_time")
    if not raw:
        return False
    try:
        candle = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    current = current or trade_bot.now_ist()
    if candle.tzinfo is None:
        candle = candle.replace(tzinfo=current.tzinfo)
    maximum_age = max(_configured_float("VAMSI_KB_MAX_CANDLE_AGE_MINUTES", 7.0), 5.0)
    age = (current - candle).total_seconds() / 60.0
    # Upstox timestamps a candle at its start.  A just-completed 5M candle is
    # therefore normally five to seven minutes old when this scan runs.
    return candle.date() == current.date() and 4.5 <= age <= maximum_age


def evaluate_knowledge_setup(candidate: dict, current=None) -> dict:
    """Apply all option-buying gates without using the legacy unified score."""
    current = current or trade_bot.now_ist()
    candidate = candidate or {}
    direction = str(candidate.get("direction") or "").upper()
    technicals = candidate.get("technicals") or {}
    summary = candidate.get("option_summary") or {}
    five = technicals.get("five_min") or {}
    fifteen = technicals.get("fifteen_min") or {}
    regime = technicals.get("market_regime") or {}
    structure = technicals.get("entry_structure") or {}
    reversal = technicals.get("bollinger_reversal") or {}
    breadth = technicals.get("nifty_breadth") or {}
    flow = (
        technicals.get("execution_atm_option_flow")
        or technicals.get("atm_option_flow")
        or {}
    )
    quality = summary.get("option_market_quality") or technicals.get(
        "option_market_quality", {}
    )
    feasibility = technicals.get("trade_feasibility") or {}
    blockers: list[str] = []
    evidence: dict[str, dict] = {}

    if direction not in {"BULLISH", "BEARISH"}:
        blockers.append("direction is not bullish or bearish")
    opposite = _opposite(direction) if direction in {"BULLISH", "BEARISH"} else ""

    reversal_setup = bool(
        reversal.get("confirmed") and reversal.get("direction") == direction
    )
    regime_name = str(regime.get("regime") or "").upper()
    regime_direction = str(regime.get("direction") or "NEUTRAL").upper()
    structure_type = str(structure.get("type") or "NONE").upper()
    trend_setup = bool(
        regime_name in {"TREND", "VOLATILITY_EXPANSION"}
        and regime_direction == direction
        and structure.get("qualified")
        and structure_type in {
            "BREAKOUT", "RETEST_HOLD", "PULLBACK_HOLD", "TREND_CONTINUATION",
        }
    )
    setup_passed = trend_setup or (
        _configured_bool("VAMSI_KB_BOLLINGER_REVERSAL_ENABLED", True)
        and reversal_setup
    )
    evidence["setup"] = {
        "passed": setup_passed,
        "type": "BOLLINGER_REVERSAL" if reversal_setup else structure_type,
        "regime": regime_name,
        "regime_direction": regime_direction,
    }
    if not setup_passed:
        blockers.append(
            "no qualified trend breakout/retest/pullback or confirmed Bollinger reversal"
        )

    candle_passed = bool(
        _candle_is_fresh(five, current)
        and fifteen.get("bias") == direction
        and (
            five.get("bias") == direction
            or (
                reversal_setup
                and _number(structure.get("signed_momentum")) >= 2.0
            )
        )
    )
    evidence["completed_candles"] = {
        "passed": candle_passed,
        "five_bias": five.get("bias"),
        "five_confidence": five.get("confidence"),
        "fifteen_bias": fifteen.get("bias"),
        "candle_time": five.get("candle_time"),
    }
    if not candle_passed:
        blockers.append("fresh completed 5M and 15M direction are not aligned")

    breadth_score = _number(breadth.get("score"))
    oriented_breadth = breadth_score if direction == "BULLISH" else -breadth_score
    breadth_passed = bool(
        breadth.get("coverage", 0) >= int(_configured_float("VAMSI_KB_MIN_BREADTH_COVERAGE", 30))
        and breadth.get("bias") == direction
        and oriented_breadth >= _configured_float("VAMSI_KB_MIN_BREADTH_SCORE", 18.0)
    )
    evidence["breadth"] = {
        "passed": breadth_passed,
        "bias": breadth.get("bias"),
        "confidence": breadth.get("confidence"),
        "score": breadth_score,
        "coverage": breadth.get("coverage"),
    }
    if not breadth_passed:
        blockers.append("NIFTY constituent breadth does not confirm direction")

    chain_bias = str(summary.get("chain_bias") or "NEUTRAL").upper()
    chain_confidence = str(summary.get("chain_confidence") or "LOW").upper()
    neutral_chain_allowed = _configured_bool("VAMSI_KB_ALLOW_NEUTRAL_CHAIN", False)
    chain_passed = bool(
        (chain_bias == direction and _confidence_rank(chain_confidence) >= 1)
        or (
            neutral_chain_allowed
            and chain_bias == "NEUTRAL"
            and oriented_breadth >= 35
        )
    )
    evidence["option_chain"] = {
        "passed": chain_passed,
        "bias": chain_bias,
        "confidence": chain_confidence,
        "opposite": chain_bias == opposite,
    }
    if not chain_passed:
        blockers.append("option-chain direction is not a medium/high confirmation")

    flow_close = _number(flow.get("close"))
    flow_vwap = _number(flow.get("vwap"))
    volume_ratio = _number(flow.get("volume_ratio"))
    minimum_volume = _configured_float("VAMSI_KB_MIN_OPTION_VOLUME_RATIO", 1.0)
    flow_passed = bool(
        flow.get("bias") == "BULLISH"
        and flow_close > 0
        and flow_vwap > 0
        and flow_close >= flow_vwap
        and volume_ratio >= minimum_volume
    )
    evidence["option_flow"] = {
        "passed": flow_passed,
        "bias": flow.get("bias"),
        "confidence": flow.get("confidence"),
        "close": flow_close,
        "vwap": flow_vwap,
        "volume_ratio": volume_ratio,
    }
    if not flow_passed:
        blockers.append("bought option premium lacks VWAP and volume confirmation")

    spread = quality.get("spread_percent")
    delta = quality.get("delta")
    absolute_delta = abs(_number(delta))
    minimum_delta = _configured_float("VAMSI_KB_MIN_OPTION_DELTA", 0.40)
    maximum_delta = _configured_float("VAMSI_KB_MAX_OPTION_DELTA", 0.70)
    maximum_spread = _configured_float("VAMSI_KB_MAX_OPTION_SPREAD_PERCENT", 2.0)
    quality_passed = bool(
        quality.get("entry_allowed", True)
        and spread is not None
        and _number(spread, 999) <= maximum_spread
        and minimum_delta <= absolute_delta <= maximum_delta
        and _number(quality.get("ltp"), candidate.get("entry_price")) > 0
    )
    evidence["contract_quality"] = {
        "passed": quality_passed,
        "spread_percent": spread,
        "delta": delta,
        "depth_bias": quality.get("depth_bias"),
    }
    if not quality_passed:
        blockers.append("ATM/near-ATM contract spread, delta or quote quality failed")

    extension = feasibility.get("entry_extension_percent")
    maximum_extension = _configured_float("VAMSI_KB_MAX_ENTRY_EXTENSION_PERCENT", 1.5)
    freshness_passed = bool(
        extension is None or _number(extension) <= maximum_extension
    )
    evidence["entry_freshness"] = {
        "passed": freshness_passed,
        "extension_percent": extension,
        "maximum_percent": maximum_extension,
    }
    if not freshness_passed:
        blockers.append("option premium is already extended beyond the completed candle")

    passed_count = sum(bool(item.get("passed")) for item in evidence.values())
    score = round(passed_count / max(len(evidence), 1) * 100, 1)
    return {
        "allowed": not blockers,
        "direction": direction,
        "setup": evidence["setup"]["type"],
        "score": score,
        "score_version": SCORE_VERSION,
        "score_is_probability": False,
        "blockers": blockers,
        "evidence": evidence,
    }


def prepare_candidate(candidate: dict, decision: dict) -> dict:
    prepared = deepcopy(candidate)
    quality = (
        (prepared.get("option_summary") or {}).get("option_market_quality")
        or (prepared.get("technicals") or {}).get("option_market_quality")
        or {}
    )
    delta = abs(_number(quality.get("delta"), _configured_float(
        "OPTION_DELTA_APPROXIMATION", 0.50
    )))
    target_points = _configured_float("VAMSI_KB_TARGET_POINTS", 30.0)
    stop_points = _configured_float("VAMSI_KB_STOP_POINTS", 30.0)
    levels = trade_bot.option_levels_from_index_points(
        "NIFTY",
        prepared["entry_price"],
        target_points=target_points,
        stop_points=stop_points,
        delta=delta,
    )
    prepared.update(
        {
            "allowed": True,
            "strategy": ENGINE,
            "symbol": "NIFTY",
            "confidence": "HIGH",
            "signal_score": decision["score"],
            "target_price": levels["target_price"],
            "stop_loss_price": levels["stop_loss_price"],
            "target_points": target_points,
            "stop_points": stop_points,
            "option_delta_used": delta,
            "target_percent": None,
            "stop_percent": None,
            "target_profile": "KB_FIXED_NIFTY_POINTS",
            "profit_protection_enabled_for_trade": True,
            "score_cutoff_approved": True,
            "score_rule_source": ENGINE,
            "entry_score": {
                "score": decision["score"],
                "score_version": SCORE_VERSION,
                "score_kind": "ALL_INDEPENDENT_KNOWLEDGE_GATES",
                "probability_calibrated": False,
                "components": {
                    name: 1 if value.get("passed") else 0
                    for name, value in decision["evidence"].items()
                },
            },
            "knowledge_decision": decision,
        }
    )
    prepared.setdefault("option_summary", {})["strategy"] = ENGINE
    prepared["option_summary"]["knowledge_decision"] = decision
    return prepared


def _record_scan(slot: str, action: str, decision=None, candidate=None, quantity=None) -> None:
    decision = decision or {}
    candidate = candidate or {}
    instrument = candidate.get("instrument") or {}
    locked_append_csv(
        SCAN_FILE,
        SCAN_COLUMNS,
        {
            "scan_time": trade_bot.now_ist().isoformat(),
            "scan_slot": slot,
            "action": action,
            "direction": decision.get("direction") or candidate.get("direction") or "",
            "setup": decision.get("setup") or "",
            "knowledge_score": decision.get("score") if decision else "",
            "instrument": instrument.get("trading_symbol") or "",
            "entry_price": candidate.get("entry_price") or "",
            "target_price": candidate.get("target_price") or "",
            "stop_loss_price": candidate.get("stop_loss_price") or "",
            "quantity": quantity if quantity is not None else "",
            "blockers": " | ".join(decision.get("blockers") or []),
            "evidence": json.dumps(decision.get("evidence") or {}, sort_keys=True),
        },
    )


def _entry_window_ok(current=None) -> bool:
    current = current or trade_bot.now_ist()
    first = trade_bot.configured_clock("VAMSI_KB_FIRST_ENTRY_TIME", "09:20")
    last = trade_bot.configured_clock("VAMSI_KB_LAST_ENTRY_TIME", "15:15")
    return first <= current.time() <= last


def scan() -> dict:
    trade_bot.load_env()
    if trade_bot.trading_engine() != ENGINE:
        raise RuntimeError(f"TRADING_ENGINE must be {ENGINE}")
    if not _entry_window_ok():
        raise RuntimeError("Outside VAMSI knowledge-engine entry window")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    slot = completed_scan_slot()
    with file_lock(SCAN_LOCK_FILE):
        state = _read_scan_state()
        if state.get("date") == trade_bot.now_ist().date().isoformat() and state.get(
            "last_scan_slot"
        ) == slot:
            log(f"duplicate completed-candle scan skipped: {slot}")
            return {"action": "DUPLICATE", "scan_slot": slot}

        # Claim the completed-candle slot before network work.  A broker or
        # quote failure is recorded and the next five-minute candle may retry;
        # the same signal can never submit twice.
        atomic_write_json(
            SCAN_STATE_FILE,
            {
                "date": trade_bot.now_ist().date().isoformat(),
                "last_scan_slot": slot,
                "claimed_at": trade_bot.now_ist().isoformat(),
            },
            sort_keys=True,
        )

        if trade_bot.state_is_active(trade_bot.read_state("NIFTY")):
            _record_scan(slot, "ACTIVE_POSITION")
            return {"action": "ACTIVE_POSITION", "scan_slot": slot}
        daily_block = trade_bot.daily_index_entry_block_reason("NIFTY")
        if daily_block:
            _record_scan(slot, "DAILY_STOP")
            log(daily_block)
            return {"action": "DAILY_STOP", "reason": daily_block, "scan_slot": slot}

        candidate = trade_bot.evaluate_symbol_buy_or_sell(
            "NIFTY",
            allow_option_sell=False,
            include_rejected=True,
            paper_observation=True,
        )
        if not candidate:
            _record_scan(slot, "NO_CANDIDATE")
            log("no complete option-buying candidate was available")
            return {"action": "NO_CANDIDATE", "scan_slot": slot}

        decision = evaluate_knowledge_setup(candidate)
        if not decision["allowed"]:
            _record_scan(slot, "REJECT", decision, candidate)
            short_reasons = decision["blockers"][:2]
            omitted = len(decision["blockers"]) - len(short_reasons)
            log(
                f"{decision['direction']} {decision['setup']} reject "
                f"knowledge={decision['score']:.1f}: "
                + "; ".join(short_reasons)
                + (f" (+{omitted} audit reasons)" if omitted > 0 else "")
            )
            return {"action": "REJECT", "scan_slot": slot, **decision}

        prepared = prepare_candidate(candidate, decision)
        quantity = trade_bot.order_quantity_for(
            "NIFTY",
            prepared["instrument"],
            prepared["entry_price"],
            prepared["stop_loss_price"],
            transaction_type="BUY",
        )
        if quantity <= 0:
            decision["blockers"] = ["available capital cannot fund one whole lot"]
            decision["allowed"] = False
            _record_scan(slot, "CAPITAL_REJECT", decision, prepared, 0)
            return {"action": "CAPITAL_REJECT", "scan_slot": slot, **decision}

        _record_scan(slot, "ENTRY_SELECTED", decision, prepared, quantity)
        log(
            f"{decision['direction']} {decision['setup']} selected; "
            f"knowledge={decision['score']:.1f} qty={quantity} "
            f"target/stop={prepared['target_points']:.0f}/{prepared['stop_points']:.0f} NIFTY points"
        )
        placed = trade_bot.execute_selected_candidate(prepared)
        action = "LIVE_ENTRY" if placed else "EXECUTION_REJECT"
        return {
            "action": action,
            "scan_slot": slot,
            "quantity": quantity,
            **decision,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true")
    args = parser.parse_args()
    if not args.scan:
        parser.error("choose --scan")
    scan()


if __name__ == "__main__":
    main()
