"""Daily evidence-based score calibration for the Vamsi engine."""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from adaptive_exit_shadow import (
    SHADOW_CONFIG_FILE,
    build_shadow_config,
    parse_grid,
)
from adaptive_live_policy import (
    LIVE_POLICY_FILE,
    build_live_policy,
    read_policy,
)
from post_market_score_audit import AUDIT_FILE, read_audit
from safe_storage import atomic_write_json
from unified_entry_score import UNIFIED_SCORE_VERSION


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ENV_FILE = BASE_DIR / ".env"
ADAPTIVE_CONFIG_FILE = DATA_DIR / "vamsi_adaptive_score_config.json"
IST = ZoneInfo("Asia/Kolkata")
SYMBOLS = ("NIFTY", "BANKNIFTY")
DEFAULT_FAVORABLE_POINTS = {"NIFTY": 10.0, "BANKNIFTY": 20.0}
DEFAULT_EXIT_POINTS = {
    "NIFTY": {"target": 30.0, "stop": 30.0},
    "BANKNIFTY": {"target": 40.0, "stop": 40.0},
}


def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_int(value, default):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _clamp(value, minimum, maximum):
    return max(float(minimum), min(float(maximum), float(value)))


def load_env_file(path=ENV_FILE):
    path = Path(path)
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def wilson_lower_bound(successes, samples, z=1.2815515655446004):
    """Return the one-sided 90% Wilson lower confidence bound."""
    if samples <= 0:
        return 0.0
    proportion = float(successes) / float(samples)
    denominator = 1.0 + (z * z / samples)
    centre = proportion + (z * z / (2.0 * samples))
    adjustment = z * math.sqrt(
        (proportion * (1.0 - proportion) / samples)
        + (z * z / (4.0 * samples * samples))
    )
    return max((centre - adjustment) / denominator, 0.0)


def prepare_history(frame, symbol, effective_date, lookback_days, favorable_points):
    if frame.empty:
        return pd.DataFrame()
    working = frame.copy()
    working["symbol"] = working.get("symbol", "").astype(str).str.upper()
    working["trading_date_dt"] = pd.to_datetime(
        working.get("trading_date"), errors="coerce"
    )
    working["score"] = pd.to_numeric(working.get("score"), errors="coerce")
    score_versions = working.get(
        "score_version",
        pd.Series(index=working.index, dtype=object),
    ).astype(str)
    working["favorable_points"] = pd.to_numeric(
        working.get("favorable_points"), errors="coerce"
    )
    working["adverse_points"] = pd.to_numeric(
        working.get("adverse_points"), errors="coerce"
    )
    direction_correct = (
        working.get("direction_correct", pd.Series(index=working.index, dtype=object))
        .astype(str)
        .str.lower()
        .map({"true": True, "false": False})
    )
    start_date = effective_date - timedelta(days=max(int(lookback_days), 1))
    dates = working["trading_date_dt"].dt.date
    working = working[
        (working["symbol"] == str(symbol).upper())
        & dates.ge(start_date)
        & dates.lt(effective_date)
        & working["score"].between(0, 100, inclusive="both")
        & score_versions.eq(UNIFIED_SCORE_VERSION)
        & working["favorable_points"].notna()
        & working["adverse_points"].notna()
        & direction_correct.notna()
    ].copy()
    if working.empty:
        return working
    working["direction_correct_numeric"] = direction_correct.loc[working.index].astype(bool)
    working["success"] = (
        working["direction_correct_numeric"]
        & (working["favorable_points"] >= float(favorable_points))
        & (working["favorable_points"] > working["adverse_points"])
    )
    working["normalized_net_excursion"] = (
        working["favorable_points"] - working["adverse_points"]
    ) / float(favorable_points)
    return working


def evaluate_candidate(frame, mode, minimum, maximum, total_samples):
    selected = frame[frame["score"] >= float(minimum)]
    if maximum is not None:
        selected = selected[selected["score"] <= float(maximum)]
    samples = len(selected)
    successes = int(selected["success"].sum()) if samples else 0
    success_rate = successes / samples if samples else 0.0
    direction_accuracy = (
        float(selected["direction_correct_numeric"].mean()) if samples else 0.0
    )
    average_net = (
        float(selected["normalized_net_excursion"].mean()) if samples else 0.0
    )
    average_favorable = float(selected["favorable_points"].mean()) if samples else 0.0
    average_adverse = float(selected["adverse_points"].mean()) if samples else 0.0
    coverage = samples / total_samples if total_samples else 0.0
    confidence_floor = wilson_lower_bound(successes, samples)
    quality = (
        confidence_floor
        + 0.04 * math.tanh(average_net)
        + 0.03 * math.sqrt(coverage)
        - (0.01 if mode == "RANGE" else 0.0)
    )
    return {
        "mode": mode,
        "min_score": float(minimum),
        "max_score": float(maximum) if maximum is not None else None,
        "samples": samples,
        "trading_days": int(selected["trading_date_dt"].dt.date.nunique()) if samples else 0,
        "successes": successes,
        "success_rate": round(success_rate, 4),
        "direction_accuracy": round(direction_accuracy, 4),
        "average_normalized_net_excursion": round(average_net, 4),
        "average_favorable_points": round(average_favorable, 2),
        "average_adverse_points": round(average_adverse, 2),
        "coverage": round(coverage, 4),
        "confidence_floor": round(confidence_floor, 4),
        "quality_score": round(quality, 6),
    }


def calibrate_symbol(
    frame,
    symbol,
    effective_date,
    *,
    fallback_minimum=55.0,
    lookback_days=90,
    minimum_samples=20,
    minimum_trading_days=5,
    minimum_success_rate=0.50,
    range_advantage=0.03,
    favorable_points=None,
    default_target_points=None,
    default_stop_points=None,
):
    symbol = str(symbol).upper()
    if symbol not in SYMBOLS:
        raise ValueError(f"Unsupported symbol: {symbol}")
    favorable_points = float(
        favorable_points
        if favorable_points is not None
        else DEFAULT_FAVORABLE_POINTS[symbol]
    )
    defaults = DEFAULT_EXIT_POINTS[symbol]
    default_target_points = float(default_target_points or defaults["target"])
    default_stop_points = float(default_stop_points or defaults["stop"])
    if not 0 <= float(fallback_minimum) <= 100:
        raise ValueError("fallback_minimum must be between 0 and 100")
    if int(lookback_days) < 1:
        raise ValueError("lookback_days must be at least 1")
    if int(minimum_samples) < 1 or int(minimum_trading_days) < 1:
        raise ValueError("minimum sample and trading-day requirements must be at least 1")
    if not 0 <= float(minimum_success_rate) <= 1:
        raise ValueError("minimum_success_rate must be between 0 and 1")
    if float(range_advantage) < 0:
        raise ValueError("range_advantage must be non-negative")
    if favorable_points <= 0:
        raise ValueError("favorable_points must be greater than 0")
    if default_target_points <= 0 or default_stop_points <= 0:
        raise ValueError("default target and stop points must be greater than 0")
    history = prepare_history(
        frame,
        symbol,
        effective_date,
        lookback_days,
        favorable_points,
    )
    fallback = {
        "status": "FALLBACK_INSUFFICIENT_HISTORY",
        "mode": "MIN",
        "min_score": float(fallback_minimum),
        "max_score": None,
        "samples": len(history),
        "trading_days": (
            int(history["trading_date_dt"].dt.date.nunique()) if not history.empty else 0
        ),
        "success_rate": None,
        "direction_accuracy": None,
        "average_normalized_net_excursion": None,
        "average_favorable_points": None,
        "average_adverse_points": None,
        "confidence_floor": None,
        "quality_score": None,
        "favorable_points_required": favorable_points,
        "exit_levels": {
            "source": "STATIC_FALLBACK",
            "target_points": default_target_points,
            "stop_points": default_stop_points,
        },
        "reason": (
            f"Need at least {int(minimum_samples)} observations across "
            f"{int(minimum_trading_days)} trading days"
        ),
    }
    if (
        len(history) < int(minimum_samples)
        or history["trading_date_dt"].dt.date.nunique() < int(minimum_trading_days)
    ):
        return fallback

    boundaries = sorted(
        {
            min(int(float(score) // 5) * 5, 95)
            for score in history["score"].dropna().tolist()
        }
    )
    candidates = []
    for minimum in boundaries:
        candidates.append(
            evaluate_candidate(history, "MIN", minimum, None, len(history))
        )
        for upper_bucket in boundaries:
            # Live unified scores are rounded to one decimal; include the
            # complete five-point audit bucket (for example, 60.0 through 64.9).
            upper = 100 if upper_bucket == 95 else upper_bucket + 4.9
            if upper - minimum < 14:
                continue
            candidates.append(
                evaluate_candidate(history, "RANGE", minimum, upper, len(history))
            )

    usable = [
        candidate
        for candidate in candidates
        if candidate["samples"] >= int(minimum_samples)
        and candidate["trading_days"] >= int(minimum_trading_days)
        and candidate["success_rate"] >= float(minimum_success_rate)
        and candidate["average_normalized_net_excursion"] > 0
    ]
    if not usable:
        fallback["status"] = "FALLBACK_NO_POSITIVE_WINDOW"
        fallback["reason"] = (
            "No score window met the configured sample, success-rate, and net-movement rules"
        )
        return fallback

    minimum_candidates = [item for item in usable if item["mode"] == "MIN"]
    range_candidates = [item for item in usable if item["mode"] == "RANGE"]
    best_minimum = max(
        minimum_candidates,
        key=lambda item: (item["quality_score"], item["samples"], -item["min_score"]),
        default=None,
    )
    best_range = max(
        range_candidates,
        key=lambda item: (item["quality_score"], item["samples"], -item["min_score"]),
        default=None,
    )
    if best_range and (
        best_minimum is None
        or best_range["quality_score"]
        >= best_minimum["quality_score"] + float(range_advantage)
    ):
        chosen = best_range
        reason = "A bounded score window materially outperformed every minimum-only rule"
    else:
        chosen = best_minimum or best_range
        reason = "A minimum-only rule was as reliable as the best bounded score window"

    return {
        "status": "ADAPTIVE",
        **chosen,
        "favorable_points_required": favorable_points,
        # Score calibration and exit calibration are intentionally isolated.
        # Exit research is written to a separate shadow-only file and can never
        # alter live or paper execution settings.
        "exit_levels": fallback["exit_levels"],
        "reason": reason,
    }


def build_daily_config(
    frame,
    effective_date,
    *,
    fallback_minimum=55.0,
    lookback_days=90,
    minimum_samples=20,
    minimum_trading_days=5,
    minimum_success_rate=0.50,
    range_advantage=0.03,
    favorable_points_by_symbol=None,
    default_exit_points_by_symbol=None,
    generated_at=None,
):
    favorable_points_by_symbol = favorable_points_by_symbol or DEFAULT_FAVORABLE_POINTS
    default_exit_points_by_symbol = default_exit_points_by_symbol or DEFAULT_EXIT_POINTS
    generated_at = generated_at or datetime.now(IST)
    rules = {
        symbol: calibrate_symbol(
            frame,
            symbol,
            effective_date,
            fallback_minimum=fallback_minimum,
            lookback_days=lookback_days,
            minimum_samples=minimum_samples,
            minimum_trading_days=minimum_trading_days,
            minimum_success_rate=minimum_success_rate,
            range_advantage=range_advantage,
            favorable_points=favorable_points_by_symbol[symbol],
            default_target_points=default_exit_points_by_symbol[symbol]["target"],
            default_stop_points=default_exit_points_by_symbol[symbol]["stop"],
        )
        for symbol in SYMBOLS
    }
    adaptive_count = sum(rule["status"] == "ADAPTIVE" for rule in rules.values())
    return {
        "version": 2,
        "score_version": UNIFIED_SCORE_VERSION,
        "status": "COMPLETE" if adaptive_count == len(SYMBOLS) else "PARTIAL_FALLBACK",
        "generated_at": generated_at.isoformat(),
        "effective_date": effective_date.isoformat(),
        "history_through_date": (effective_date - timedelta(days=1)).isoformat(),
        "lookback_days": int(lookback_days),
        "minimum_samples": int(minimum_samples),
        "minimum_trading_days": int(minimum_trading_days),
        "minimum_success_rate": float(minimum_success_rate),
        "range_advantage": float(range_advantage),
        "fallback_minimum": float(fallback_minimum),
        "symbols": rules,
    }


def read_effective_score_rule(symbol, effective_date, path=ADAPTIVE_CONFIG_FILE):
    """Return today's validated adaptive rule, or None for the static fallback."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    date_text = effective_date.isoformat() if hasattr(effective_date, "isoformat") else str(effective_date)
    if payload.get("effective_date") != date_text:
        return None
    if payload.get("score_version") != UNIFIED_SCORE_VERSION:
        return None
    rule = (payload.get("symbols") or {}).get(str(symbol).upper()) or {}
    if rule.get("status") != "ADAPTIVE":
        return None
    try:
        minimum = float(rule["min_score"])
        maximum = rule.get("max_score")
        maximum = float(maximum) if maximum is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= minimum <= 100 or (maximum is not None and not minimum <= maximum <= 100):
        return None
    mode = str(rule.get("mode") or "MIN").upper()
    if mode not in {"MIN", "RANGE"} or (mode == "RANGE" and maximum is None):
        return None
    return {**rule, "mode": mode, "min_score": minimum, "max_score": maximum}


def main():
    parser = argparse.ArgumentParser(
        description="Calibrate today's Vamsi unified-score rule from post-market history"
    )
    parser.add_argument("--date", help="Effective trading date in YYYY-MM-DD format")
    parser.add_argument("--audit-file", default=str(AUDIT_FILE))
    parser.add_argument("--output", default=str(ADAPTIVE_CONFIG_FILE))
    parser.add_argument("--shadow-output", default=str(SHADOW_CONFIG_FILE))
    parser.add_argument("--live-policy-output", default=str(LIVE_POLICY_FILE))
    args = parser.parse_args()
    load_env_file()
    effective_date = date.fromisoformat(args.date) if args.date else datetime.now(IST).date()
    frame = read_audit(args.audit_file)
    fallback_minimum = _to_float(os.getenv("VAMSI_UNIFIED_SCORE_FALLBACK"), 55.0)
    config = build_daily_config(
        frame,
        effective_date,
        fallback_minimum=fallback_minimum,
        lookback_days=_to_int(os.getenv("VAMSI_ADAPTIVE_LOOKBACK_DAYS"), 90),
        minimum_samples=_to_int(os.getenv("VAMSI_ADAPTIVE_MIN_SAMPLES"), 20),
        minimum_trading_days=_to_int(
            os.getenv("VAMSI_ADAPTIVE_MIN_TRADING_DAYS"), 5
        ),
        minimum_success_rate=_to_float(
            os.getenv("VAMSI_ADAPTIVE_MIN_SUCCESS_RATE"), 0.50
        ),
        range_advantage=_to_float(os.getenv("VAMSI_ADAPTIVE_RANGE_ADVANTAGE"), 0.03),
        favorable_points_by_symbol={
            "NIFTY": _to_float(
                os.getenv("VAMSI_ADAPTIVE_NIFTY_FAVORABLE_POINTS"), 10.0
            ),
            "BANKNIFTY": _to_float(
                os.getenv("VAMSI_ADAPTIVE_BANKNIFTY_FAVORABLE_POINTS"), 20.0
            ),
        },
        default_exit_points_by_symbol={
            "NIFTY": {
                "target": _to_float(os.getenv("NIFTY_TARGET_POINTS"), 30.0),
                "stop": _to_float(os.getenv("NIFTY_STOP_POINTS"), 30.0),
            },
            "BANKNIFTY": {
                "target": _to_float(os.getenv("BANKNIFTY_TARGET_POINTS"), 90.0),
                "stop": _to_float(os.getenv("BANKNIFTY_STOP_POINTS"), 90.0),
            },
        },
    )
    atomic_write_json(args.output, config, sort_keys=True)

    shadow_config = None
    shadow_mode = str(os.getenv("VAMSI_ADAPTIVE_EXIT_MODE", "shadow")).strip().lower()
    if shadow_mode == "shadow":
        shadow_config = build_shadow_config(
            frame,
            effective_date,
            current_target=_to_float(os.getenv("NIFTY_TARGET_POINTS"), 30.0),
            current_stop=_to_float(os.getenv("NIFTY_STOP_POINTS"), 30.0),
            lookback_days=_to_int(
                os.getenv("VAMSI_ADAPTIVE_EXIT_LOOKBACK_DAYS"), 90
            ),
            horizon_minutes=_to_int(
                os.getenv("VAMSI_ADAPTIVE_EXIT_HORIZON_MINUTES"), 60
            ),
            entry_start_time=os.getenv("VAMSI_FIRST_ENTRY_TIME", "11:00"),
            entry_end_time=os.getenv("VAMSI_LAST_ENTRY_TIME", "13:55"),
            target_grid=parse_grid(
                os.getenv("VAMSI_ADAPTIVE_EXIT_TARGET_GRID"),
                (10, 15, 20, 25, 30, 35, 40, 45),
            ),
            stop_grid=parse_grid(
                os.getenv("VAMSI_ADAPTIVE_EXIT_STOP_GRID"),
                (10, 15, 20, 25, 30, 35, 40, 45),
            ),
            minimum_samples=_to_int(
                os.getenv("VAMSI_ADAPTIVE_EXIT_MIN_SAMPLES"), 40
            ),
            minimum_trading_days=_to_int(
                os.getenv("VAMSI_ADAPTIVE_EXIT_MIN_TRADING_DAYS"), 10
            ),
            validation_fraction=_to_float(
                os.getenv("VAMSI_ADAPTIVE_EXIT_VALIDATION_FRACTION"), 0.30
            ),
            minimum_validation_samples=_to_int(
                os.getenv("VAMSI_ADAPTIVE_EXIT_MIN_VALIDATION_SAMPLES"), 10
            ),
            minimum_validation_profit_factor=_to_float(
                os.getenv("VAMSI_ADAPTIVE_EXIT_MIN_VALIDATION_PROFIT_FACTOR"),
                1.10,
            ),
            minimum_reward_risk=_to_float(
                os.getenv("VAMSI_ADAPTIVE_EXIT_MIN_REWARD_RISK"), 0.80
            ),
            maximum_daily_change_percent=_to_float(
                os.getenv("VAMSI_ADAPTIVE_EXIT_MAX_DAILY_CHANGE_PERCENT"), 10.0
            ),
        )
        atomic_write_json(args.shadow_output, shadow_config, sort_keys=True)

    live_policy = build_live_policy(
        frame,
        effective_date,
        previous=read_policy(args.live_policy_output),
        current_target=_to_float(os.getenv("NIFTY_TARGET_POINTS"), 30.0),
        current_stop=_to_float(os.getenv("NIFTY_STOP_POINTS"), 30.0),
        lookback_days=_to_int(
            os.getenv("VAMSI_ADAPTIVE_LIVE_LOOKBACK_DAYS"), 90
        ),
        horizon_minutes=_to_int(
            os.getenv("VAMSI_ADAPTIVE_LIVE_HORIZON_MINUTES"), 60
        ),
        target_grid=parse_grid(
            os.getenv("VAMSI_ADAPTIVE_LIVE_TARGET_GRID"),
            (10, 15, 20, 25, 30, 35, 40, 45),
        ),
        stop_grid=parse_grid(
            os.getenv("VAMSI_ADAPTIVE_LIVE_STOP_GRID"),
            (10, 15, 20, 25, 30, 35, 40, 45),
        ),
        minimum_samples=_to_int(
            os.getenv("VAMSI_ADAPTIVE_LIVE_MIN_SAMPLES_PER_CELL"), 40
        ),
        minimum_trading_days=_to_int(
            os.getenv("VAMSI_ADAPTIVE_LIVE_MIN_TRADING_DAYS"), 10
        ),
        validation_fraction=_to_float(
            os.getenv("VAMSI_ADAPTIVE_LIVE_VALIDATION_FRACTION"), 0.30
        ),
        minimum_validation_samples=_to_int(
            os.getenv("VAMSI_ADAPTIVE_LIVE_MIN_VALIDATION_SAMPLES"), 12
        ),
        minimum_validation_profit_factor=_to_float(
            os.getenv("VAMSI_ADAPTIVE_LIVE_MIN_VALIDATION_PROFIT_FACTOR"), 1.20
        ),
        minimum_reward_risk=_to_float(
            os.getenv("VAMSI_ADAPTIVE_LIVE_MIN_REWARD_RISK"), 0.80
        ),
        maximum_daily_change_percent=_to_float(
            os.getenv("VAMSI_ADAPTIVE_LIVE_MAX_DAILY_EXIT_CHANGE_PERCENT"), 10.0
        ),
        required_consecutive_calibrations=_to_int(
            os.getenv("VAMSI_ADAPTIVE_LIVE_REQUIRED_CALIBRATIONS"), 3
        ),
        maximum_live_trades_cap=_to_int(
            os.getenv("VAMSI_ADAPTIVE_MAX_LIVE_TRADES_CAP"), 2
        ),
    )
    atomic_write_json(args.live_policy_output, live_policy, sort_keys=True)

    print(
        json.dumps(
            {
                "score_calibration": config,
                "shadow_exit_calibration": shadow_config,
                "adaptive_live_policy": live_policy,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
