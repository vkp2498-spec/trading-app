"""Evidence-gated daily NIFTY live policy.

The policy is generated once before market from prior-day observations.  It
starts as static data collection, promotes individual time/score cells only
after chronological validation is stable, and never changes intraday.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from adaptive_exit_shadow import (
    DEFAULT_STOP_GRID,
    DEFAULT_TARGET_GRID,
    SCORE_BANDS,
    calibrate_band,
    prepare_episodes,
)
from unified_entry_score import UNIFIED_SCORE_VERSION


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LIVE_POLICY_FILE = DATA_DIR / "vamsi_adaptive_live_policy.json"
IST = ZoneInfo("Asia/Kolkata")
TIME_CELLS = (
    ("09:15-09:59", 9 * 60 + 15, 9 * 60 + 59),
    ("10:00-10:59", 10 * 60, 10 * 60 + 59),
    ("11:00-12:59", 11 * 60, 12 * 60 + 59),
    ("13:00-13:59", 13 * 60, 13 * 60 + 59),
    ("14:00-15:25", 14 * 60, 15 * 60 + 25),
)


def time_cell(value):
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(IST)
    else:
        timestamp = timestamp.tz_convert(IST)
    minutes = timestamp.hour * 60 + timestamp.minute
    for label, start, end in TIME_CELLS:
        if start <= minutes <= end:
            return label
    return None


def cell_id(time_label, score_label):
    return f"{time_label}|{score_label}"


def _purge_within_cell(episodes, horizon_minutes):
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


def read_policy(path=LIVE_POLICY_FILE):
    try:
        payload = json.loads(Path(path).read_text())
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _prior_streak(
    previous,
    identifier,
    effective_date,
    passed,
    latest_observation_date,
):
    if not passed:
        return 0
    prior = (previous.get("cells") or {}).get(identifier) or {}
    prior_effective = str(previous.get("effective_date") or "")
    if prior_effective == effective_date.isoformat():
        return max(int(prior.get("consecutive_valid_calibrations") or 0), 1)
    if str(prior.get("latest_observation_date") or "") == str(latest_observation_date or ""):
        return max(int(prior.get("consecutive_valid_calibrations") or 0), 1)
    return max(int(prior.get("consecutive_valid_calibrations") or 0), 0) + 1


def build_live_policy(
    frame,
    effective_date: date,
    *,
    previous=None,
    current_target=30.0,
    current_stop=30.0,
    lookback_days=90,
    horizon_minutes=60,
    target_grid=DEFAULT_TARGET_GRID,
    stop_grid=DEFAULT_STOP_GRID,
    minimum_samples=40,
    minimum_trading_days=10,
    validation_fraction=0.30,
    minimum_validation_samples=12,
    minimum_validation_profit_factor=1.20,
    minimum_reward_risk=0.80,
    maximum_daily_change_percent=10.0,
    required_consecutive_calibrations=3,
    maximum_live_trades_cap=2,
    generated_at=None,
):
    previous = previous or {}
    generated_at = generated_at or datetime.now(IST)
    episodes = prepare_episodes(
        frame,
        effective_date,
        lookback_days=lookback_days,
        horizon_minutes=horizon_minutes,
        entry_start_time="09:15",
        entry_end_time="15:25",
        qualified_only=False,
        purge_overlaps=False,
    )
    for episode in episodes:
        episode["time_cell"] = time_cell(episode["signal_time"])

    cells = {}
    live_time_cells = set()
    for time_label, _start, _end in TIME_CELLS:
        for score_label, _lower, _upper in SCORE_BANDS:
            identifier = cell_id(time_label, score_label)
            prior_cell = (previous.get("cells") or {}).get(identifier) or {}
            prior_target = prior_cell.get("proposed_target_points")
            prior_stop = prior_cell.get("proposed_stop_points")
            observations = _purge_within_cell(
                [
                    episode
                    for episode in episodes
                    if episode.get("time_cell") == time_label
                    and episode.get("score_band") == score_label
                ],
                horizon_minutes,
            )
            result = calibrate_band(
                observations,
                current_target=float(prior_target or current_target),
                current_stop=float(prior_stop or current_stop),
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
            passed = result.get("status") == "SHADOW_VALIDATED"
            latest_observation_date = max(
                (str(item.get("trading_date") or "") for item in observations),
                default="",
            )
            streak = _prior_streak(
                previous,
                identifier,
                effective_date,
                passed,
                latest_observation_date,
            )
            if passed and streak >= int(required_consecutive_calibrations):
                status = "LIVE_ENABLED"
                live_time_cells.add(time_label)
            elif passed:
                status = "VALIDATING"
            elif prior_cell.get("status") == "LIVE_ENABLED":
                status = "SUSPENDED"
            else:
                status = "BUILDING" if result.get("status") == "BUILDING" else "VALIDATION_FAILED"
            cells[identifier] = {
                **result,
                "cell_id": identifier,
                "time_cell": time_label,
                "score_band": score_label,
                "latest_observation_date": latest_observation_date or None,
                "status": status,
                "consecutive_valid_calibrations": streak,
                "required_consecutive_calibrations": int(required_consecutive_calibrations),
                "execution_applied": status == "LIVE_ENABLED",
            }

    previously_live = bool(previous.get("ever_live_enabled"))
    live_cells = [cell for cell in cells.values() if cell["status"] == "LIVE_ENABLED"]
    ever_live_enabled = previously_live or bool(live_cells)
    if live_cells:
        global_status = "LIVE_ENABLED"
    elif ever_live_enabled:
        global_status = "SUSPENDED"
    else:
        global_status = "BUILDING_STATIC_COLLECTION"
    maximum_live_trades = (
        min(len(live_time_cells), max(int(maximum_live_trades_cap), 0))
        if live_cells
        else 0
    )
    return {
        "version": 1,
        "mode": "AUTO_ADAPTIVE_LIVE",
        "score_version": UNIFIED_SCORE_VERSION,
        "global_status": global_status,
        "execution_applied": global_status in {"LIVE_ENABLED", "SUSPENDED"},
        "ever_live_enabled": ever_live_enabled,
        "effective_date": effective_date.isoformat(),
        "history_through_date": (effective_date - timedelta(days=1)).isoformat(),
        "generated_at": generated_at.isoformat(),
        "lookback_days": int(lookback_days),
        "horizon_minutes": int(horizon_minutes),
        "minimum_samples_per_cell": int(minimum_samples),
        "minimum_trading_days": int(minimum_trading_days),
        "validation_fraction": float(validation_fraction),
        "minimum_validation_samples": int(minimum_validation_samples),
        "minimum_validation_profit_factor": float(minimum_validation_profit_factor),
        "required_consecutive_calibrations": int(required_consecutive_calibrations),
        "maximum_live_trades_cap": int(maximum_live_trades_cap),
        "maximum_live_trades": int(maximum_live_trades),
        "independent_cell_episodes": sum(
            int(cell.get("samples") or 0) for cell in cells.values()
        ),
        "live_cell_count": len(live_cells),
        "cells": cells,
    }


def effective_policy(effective_date, path=LIVE_POLICY_FILE):
    payload = read_policy(path)
    date_text = (
        effective_date.isoformat()
        if hasattr(effective_date, "isoformat")
        else str(effective_date)
    )
    if payload.get("effective_date") != date_text:
        return None
    if payload.get("score_version") != UNIFIED_SCORE_VERSION:
        return None
    if payload.get("mode") != "AUTO_ADAPTIVE_LIVE":
        return None
    if payload.get("global_status") not in {
        "BUILDING_STATIC_COLLECTION",
        "LIVE_ENABLED",
        "SUSPENDED",
    }:
        return None
    return payload


def runtime_policy(effective_date, path=LIVE_POLICY_FILE):
    """Return today's policy, or a fail-closed suspension after first promotion."""
    payload = read_policy(path)
    current = effective_policy(effective_date, path)
    if current:
        return current
    if payload.get("ever_live_enabled"):
        return {
            **payload,
            "global_status": "SUSPENDED",
            "execution_applied": True,
            "maximum_live_trades": 0,
            "cells": {},
            "suspension_reason": "adaptive policy is missing, stale, or incompatible",
        }
    return None


def policy_decision(payload, score, timestamp):
    if not payload or payload.get("global_status") == "BUILDING_STATIC_COLLECTION":
        return {"adaptive_active": False, "allowed": None}
    time_label = time_cell(timestamp)
    numeric_score = float(score)
    for score_label, lower, upper in SCORE_BANDS:
        if lower <= numeric_score < upper:
            identifier = cell_id(time_label, score_label) if time_label else ""
            cell = (payload.get("cells") or {}).get(identifier) or {}
            if cell.get("status") == "LIVE_ENABLED":
                return {
                    "adaptive_active": True,
                    "allowed": True,
                    "cell": cell,
                    "reason": f"adaptive live cell {identifier}",
                }
            return {
                "adaptive_active": True,
                "allowed": False,
                "cell": cell or None,
                "reason": f"no live-enabled adaptive cell for {identifier or 'current time/score'}",
            }
    return {
        "adaptive_active": True,
        "allowed": False,
        "cell": None,
        "reason": "score is outside adaptive research bands",
    }
