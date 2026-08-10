"""Shadow-only score-band exit calibration for the Vamsi NIFTY engine.

This module deliberately has no runtime execution hook.  It evaluates target
and stop pairs from stored minute paths, writes research recommendations, and
leaves live/paper entry and exit settings unchanged.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from unified_entry_score import UNIFIED_SCORE_VERSION


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SHADOW_CONFIG_FILE = DATA_DIR / "vamsi_adaptive_exit_shadow.json"
IST = ZoneInfo("Asia/Kolkata")
SCORE_BANDS = (
    ("50-59", 50.0, 60.0),
    ("60-64", 60.0, 65.0),
    ("65-69", 65.0, 70.0),
    ("70-74", 70.0, 75.0),
    ("75-79", 75.0, 80.0),
    ("80-84", 80.0, 85.0),
    ("85-89", 85.0, 90.0),
    ("90-100", 90.0, 101.0),
)
DEFAULT_TARGET_GRID = (10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0)
DEFAULT_STOP_GRID = (10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0)


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def parse_grid(value, default):
    if value is None or str(value).strip() == "":
        return tuple(float(item) for item in default)
    result = sorted(
        {
            float(item.strip())
            for item in str(value).split(",")
            if item.strip()
        }
    )
    if not result or any(item <= 0 for item in result):
        raise ValueError("Adaptive exit grids must contain positive numbers")
    return tuple(result)


def score_band(score):
    value = _float(score, -1)
    for label, lower, upper in SCORE_BANDS:
        if lower <= value < upper:
            return label
    return None


def parse_minute_path(value):
    if not value or (isinstance(value, float) and math.isnan(value)):
        return []
    try:
        rows = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    parsed = []
    for row in rows or []:
        try:
            parsed.append(
                {
                    "timestamp": pd.Timestamp(row["t"]),
                    "open": float(row["o"]),
                    "high": float(row["h"]),
                    "low": float(row["l"]),
                    "close": float(row["c"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            return []
    return parsed


def simulate_first_touch(path, direction, reference, target_points, stop_points):
    """Return direction-adjusted points with same-minute ambiguity resolved as stop."""
    direction = str(direction or "").upper()
    if direction not in {"BULLISH", "BEARISH"} or not path:
        return None
    reference = float(reference)
    target_points = float(target_points)
    stop_points = float(stop_points)
    for candle in path:
        if direction == "BULLISH":
            target_hit = candle["high"] >= reference + target_points
            stop_hit = candle["low"] <= reference - stop_points
        else:
            target_hit = candle["low"] <= reference - target_points
            stop_hit = candle["high"] >= reference + stop_points
        if stop_hit:
            return -stop_points
        if target_hit:
            return target_points
    final_close = float(path[-1]["close"])
    return (
        final_close - reference
        if direction == "BULLISH"
        else reference - final_close
    )


def performance_stats(values):
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return {
            "samples": 0,
            "expectancy_points": 0.0,
            "conservative_expectancy_points": 0.0,
            "win_rate": 0.0,
            "profit_factor": None,
            "maximum_drawdown_points": 0.0,
        }
    mean = sum(numbers) / len(numbers)
    variance = (
        sum((value - mean) ** 2 for value in numbers) / (len(numbers) - 1)
        if len(numbers) > 1
        else 0.0
    )
    standard_error = math.sqrt(variance) / math.sqrt(len(numbers))
    gross_profit = sum(value for value in numbers if value > 0)
    gross_loss = abs(sum(value for value in numbers if value < 0))
    equity = peak = maximum_drawdown = 0.0
    for value in numbers:
        equity += value
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    return {
        "samples": len(numbers),
        "expectancy_points": round(mean, 3),
        "conservative_expectancy_points": round(mean - 1.2815516 * standard_error, 3),
        "win_rate": round(sum(value > 0 for value in numbers) / len(numbers), 4),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "maximum_drawdown_points": round(maximum_drawdown, 3),
    }


def _clock_minutes(value):
    hour, minute = str(value).strip().split(":", 1)
    return int(hour) * 60 + int(minute)


def prepare_episodes(
    frame,
    effective_date,
    lookback_days=90,
    horizon_minutes=60,
    entry_start_time="11:00",
    entry_end_time="13:55",
    qualified_only=True,
    purge_overlaps=True,
):
    if frame is None or frame.empty:
        return []
    working = frame.copy()
    working["score"] = pd.to_numeric(working.get("score"), errors="coerce")
    working["signal_time_dt"] = pd.to_datetime(
        working.get("signal_time"), errors="coerce", utc=True
    )
    working["score_version"] = working.get(
        "score_version", pd.Series(index=working.index, dtype=object)
    ).astype(str)
    working["action"] = working.get(
        "action", pd.Series(index=working.index, dtype=object)
    ).astype(str).str.lower()
    working["symbol"] = working.get(
        "symbol", pd.Series(index=working.index, dtype=object)
    ).astype(str).str.upper()
    start = pd.Timestamp(effective_date - timedelta(days=max(int(lookback_days), 1)), tz=IST)
    end = pd.Timestamp(effective_date, tz=IST)
    localized = working["signal_time_dt"].dt.tz_convert(IST)
    local_minutes = localized.dt.hour * 60 + localized.dt.minute
    entry_start_minutes = _clock_minutes(entry_start_time)
    entry_end_minutes = _clock_minutes(entry_end_time)
    eligible = (
        (working["symbol"] == "NIFTY")
        & working["score"].between(50, 100, inclusive="both")
        & working["score_version"].eq(UNIFIED_SCORE_VERSION)
        & localized.ge(start)
        & localized.lt(end)
        & local_minutes.ge(entry_start_minutes)
        & local_minutes.le(entry_end_minutes)
    )
    if qualified_only:
        eligible &= working["action"].eq("buy")
    working = working[eligible].copy()
    episodes = []
    for _, row in working.sort_values("signal_time_dt").iterrows():
        path = parse_minute_path(row.get("minute_path_json"))
        if not path:
            continue
        band = score_band(row.get("score"))
        direction = str(row.get("direction") or "").upper()
        reference = _float(row.get("reference_price"), 0)
        if not band or direction not in {"BULLISH", "BEARISH"} or reference <= 0:
            continue
        episodes.append(
            {
                "observation_id": str(row.get("observation_id") or ""),
                "signal_time": pd.Timestamp(row["signal_time_dt"]).tz_convert(IST),
                "trading_date": str(row.get("trading_date") or ""),
                "score": float(row["score"]),
                "score_band": band,
                "direction": direction,
                "reference_price": reference,
                "path": path[: max(int(horizon_minutes), 1)],
            }
        )

    if not purge_overlaps:
        return sorted(episodes, key=lambda item: item["signal_time"])

    # The audit contains scans every 15 minutes. Exit research uses a longer
    # horizon, so purge overlapping paths again to preserve independent episodes.
    selected = []
    next_allowed_by_day = {}
    spacing = pd.Timedelta(minutes=max(int(horizon_minutes), 1))
    for episode in sorted(episodes, key=lambda item: item["signal_time"]):
        day = episode["trading_date"]
        next_allowed = next_allowed_by_day.get(day)
        if next_allowed is not None and episode["signal_time"] < next_allowed:
            continue
        selected.append(episode)
        next_allowed_by_day[day] = episode["signal_time"] + spacing
    return selected


def candidate_results(episodes, target, stop):
    return [
        simulate_first_touch(
            episode["path"],
            episode["direction"],
            episode["reference_price"],
            target,
            stop,
        )
        for episode in episodes
    ]


def daily_change_cap(value, current, maximum_change_percent):
    change = float(current) * float(maximum_change_percent) / 100.0
    return round(max(float(current) - change, min(float(current) + change, float(value))), 1)


def calibrate_band(
    episodes,
    *,
    current_target=30.0,
    current_stop=30.0,
    target_grid=DEFAULT_TARGET_GRID,
    stop_grid=DEFAULT_STOP_GRID,
    minimum_samples=40,
    minimum_trading_days=10,
    validation_fraction=0.30,
    minimum_validation_samples=10,
    minimum_validation_profit_factor=1.10,
    minimum_reward_risk=0.80,
    maximum_daily_change_percent=10.0,
):
    ordered = sorted(episodes, key=lambda item: item["signal_time"])
    base = {
        "samples": len(ordered),
        "trading_days": len({item["trading_date"] for item in ordered}),
        "current_target_points": float(current_target),
        "current_stop_points": float(current_stop),
        "execution_applied": False,
    }
    if (
        len(ordered) < int(minimum_samples)
        or base["trading_days"] < int(minimum_trading_days)
    ):
        return {
            **base,
            "status": "BUILDING",
            "reason": (
                f"Need {int(minimum_samples)} independent episodes across "
                f"{int(minimum_trading_days)} trading days"
            ),
            "proposed_target_points": None,
            "proposed_stop_points": None,
        }

    validation_count = max(
        int(minimum_validation_samples),
        int(math.ceil(len(ordered) * float(validation_fraction))),
    )
    validation_count = min(validation_count, len(ordered) - 1)
    training = ordered[:-validation_count]
    validation = ordered[-validation_count:]
    candidates = []
    for target in target_grid:
        for stop in stop_grid:
            if float(target) / float(stop) < float(minimum_reward_risk):
                continue
            stats = performance_stats(candidate_results(training, target, stop))
            candidates.append({"target": float(target), "stop": float(stop), **stats})
    if not candidates:
        return {
            **base,
            "status": "BUILDING",
            "reason": "No target/stop candidates satisfied the reward/risk floor",
            "proposed_target_points": None,
            "proposed_stop_points": None,
        }
    chosen = max(
        candidates,
        key=lambda item: (
            item["conservative_expectancy_points"],
            item["expectancy_points"],
            item["profit_factor"] if item["profit_factor"] is not None else 999.0,
            -item["maximum_drawdown_points"],
        ),
    )
    validation_stats = performance_stats(
        candidate_results(validation, chosen["target"], chosen["stop"])
    )
    training_pf = chosen.get("profit_factor")
    validation_pf = validation_stats.get("profit_factor")
    validated = (
        chosen["expectancy_points"] > 0
        and chosen["conservative_expectancy_points"] > 0
        and (
            training_pf is None
            or training_pf >= float(minimum_validation_profit_factor)
        )
        and validation_stats["samples"] >= int(minimum_validation_samples)
        and validation_stats["expectancy_points"] > 0
        and validation_stats["conservative_expectancy_points"] > 0
        and (validation_pf is None or validation_pf >= float(minimum_validation_profit_factor))
    )
    status = "SHADOW_VALIDATED" if validated else "SHADOW_REVIEW"
    return {
        **base,
        "status": status,
        "reason": (
            "Positive held-out validation; recommendation remains shadow-only"
            if validated
            else "Training candidate did not pass every held-out validation rule"
        ),
        "training_samples": len(training),
        "validation_samples": len(validation),
        "raw_proposed_target_points": chosen["target"],
        "raw_proposed_stop_points": chosen["stop"],
        "proposed_target_points": daily_change_cap(
            chosen["target"], current_target, maximum_daily_change_percent
        ),
        "proposed_stop_points": daily_change_cap(
            chosen["stop"], current_stop, maximum_daily_change_percent
        ),
        "training": {key: value for key, value in chosen.items() if key not in {"target", "stop"}},
        "validation": validation_stats,
    }


def build_shadow_config(
    frame,
    effective_date: date,
    *,
    current_target=30.0,
    current_stop=30.0,
    lookback_days=90,
    horizon_minutes=60,
    entry_start_time="11:00",
    entry_end_time="13:55",
    target_grid=DEFAULT_TARGET_GRID,
    stop_grid=DEFAULT_STOP_GRID,
    minimum_samples=40,
    minimum_trading_days=10,
    validation_fraction=0.30,
    minimum_validation_samples=10,
    minimum_validation_profit_factor=1.10,
    minimum_reward_risk=0.80,
    maximum_daily_change_percent=10.0,
    generated_at=None,
):
    generated_at = generated_at or datetime.now(IST)
    episodes = prepare_episodes(
        frame,
        effective_date,
        lookback_days=lookback_days,
        horizon_minutes=horizon_minutes,
        entry_start_time=entry_start_time,
        entry_end_time=entry_end_time,
    )
    bands = {}
    for label, _lower, _upper in SCORE_BANDS:
        bands[label] = calibrate_band(
            [episode for episode in episodes if episode["score_band"] == label],
            current_target=current_target,
            current_stop=current_stop,
            target_grid=target_grid,
            stop_grid=stop_grid,
            minimum_samples=minimum_samples,
            minimum_trading_days=minimum_trading_days,
            validation_fraction=validation_fraction,
            minimum_validation_samples=minimum_validation_samples,
            minimum_validation_profit_factor=minimum_validation_profit_factor,
            minimum_reward_risk=minimum_reward_risk,
            maximum_daily_change_percent=maximum_daily_change_percent,
        )
    return {
        "version": 1,
        "mode": "SHADOW_ONLY",
        "execution_applied": False,
        "score_version": UNIFIED_SCORE_VERSION,
        "generated_at": generated_at.isoformat(),
        "effective_date": effective_date.isoformat(),
        "history_through_date": (effective_date - timedelta(days=1)).isoformat(),
        "lookback_days": int(lookback_days),
        "horizon_minutes": int(horizon_minutes),
        "entry_start_time": str(entry_start_time),
        "entry_end_time": str(entry_end_time),
        "minimum_samples": int(minimum_samples),
        "minimum_trading_days": int(minimum_trading_days),
        "validation_fraction": float(validation_fraction),
        "minimum_validation_samples": int(minimum_validation_samples),
        "minimum_validation_profit_factor": float(minimum_validation_profit_factor),
        "minimum_reward_risk": float(minimum_reward_risk),
        "maximum_daily_change_percent": float(maximum_daily_change_percent),
        "target_grid": [float(item) for item in target_grid],
        "stop_grid": [float(item) for item in stop_grid],
        "independent_episodes": len(episodes),
        "bands": bands,
    }
