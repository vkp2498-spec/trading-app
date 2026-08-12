#!/usr/bin/env python3
"""Normalize the shared NIFTY strategy block without touching account secrets."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


BLOCK_START = "# BEGIN CODEX CORE NIFTY STRATEGY"
BLOCK_END = "# END CODEX CORE NIFTY STRATEGY"
ACTIVE_STATUSES = {
    "POSITION_OPEN",
    "EXIT_PENDING",
    "BUY_PLACED_NOT_COMPLETE",
    "SELL_PLACED_NOT_COMPLETE",
}

CORE_VALUES = {
    "TRADING_ENGINE": "VAMSI",
    "ENABLE_LIVE_TRADING": "true",
    "TRADE_BANK_NIFTY": "false",
    "DEFAULT_TRADING_PROFILE": "1_LOT",
    "OPTION_CAPITAL_PER_ENTRY": "1",
    "ACCOUNT_MAX_OPTION_CAPITAL": "0",
    "ACCOUNT_MAX_LOTS_PER_ENTRY": "1",
    "MAX_LOTS_PER_ENTRY": "1",
    "SCORE_CUTOFF_MODE_ENABLED": "true",
    "VAMSI_ADAPTIVE_SCORE_ENABLED": "false",
    "VAMSI_UNIFIED_SCORE_RANGES": "80-89",
    "VAMSI_UNIFIED_SCORE_FALLBACK": "80",
    "VAMSI_UNIFIED_SCORE_MAXIMUM": "89",
    "VAMSI_FIRST_ENTRY_TIME": "09:15",
    "VAMSI_LAST_ENTRY_TIME": "15:15",
    "VAMSI_STATIC_COLLECTION_FIRST_ENTRY_TIME": "09:15",
    "VAMSI_STATIC_COLLECTION_LAST_ENTRY_TIME": "15:15",
    "PAPER_OBSERVATION_MODE_ENABLED": "true",
    "PAPER_OBSERVATION_FIRST_ENTRY_TIME": "09:15",
    "PAPER_OBSERVATION_LAST_ENTRY_TIME": "15:15",
    "MAX_SIMULTANEOUS_PAPER_OBSERVATIONS": "10",
    "NIFTY_TARGET_POINTS": "30",
    "NIFTY_STOP_POINTS": "30",
    "OPTION_DELTA_APPROXIMATION": "0.50",
    "EXTREME_SETUP_MIN_SCORE": "101",
    "PROFIT_BOOKING_TARGET_PERCENT": "80",
    "PROFIT_BOOKING_MODE": "runner",
    "PROFIT_RUNNER_LOCK_PERCENT": "55",
    "PROFIT_PROTECTION_ENABLED": "true",
    "PROFIT_PROTECTION_STAGE_ONE_TRIGGER_PERCENT": "60",
    "PROFIT_PROTECTION_STAGE_ONE_LOCK_PERCENT": "20",
    "PROFIT_PROTECTION_STAGE_TWO_TRIGGER_PERCENT": "70",
    "PROFIT_PROTECTION_STAGE_TWO_LOCK_PERCENT": "35",
    "VAMSI_THESIS_REVERSAL_EXIT_ENABLED": "true",
    "VAMSI_THESIS_REVERSAL_GRACE_MINUTES": "5",
    "VAMSI_THESIS_REVERSAL_MIN_COMPONENTS": "3",
    "VAMSI_THESIS_REVERSAL_CONFIRMATION_SCANS": "2",
    "VAMSI_THESIS_REVERSAL_SKIP_AFTER_TARGET_PROGRESS_PERCENT": "70",
    "STOP_AFTER_FIRST_PROFIT_OR_LOSS": "true",
    "STOP_AFTER_FIRST_PROFIT": "true",
    "STOP_AFTER_FIRST_LOSS": "true",
    "AFTER_FIRST_OUTCOME_MODE": "paper",
    "AFTER_FIRST_PROFIT_MODE": "paper",
    "AFTER_FIRST_LOSS_MODE": "paper",
    "MAX_INDEX_TRADES_PER_DAY": "1",
    "LOSS_REENTRY_MODE": "off",
    "MIN_REENTRY_MINUTES": "0",
    "REQUIRE_SIGNAL_RESET_FOR_SAME_INDEX_REENTRY": "false",
    "SECOND_INDEX_TRADE_SCORE_BONUS": "0",
    "SECOND_TRADE_AFTER_LOSS_SCORE_BONUS": "0",
    "ALLOW_SIMULTANEOUS_INDEX_POSITIONS": "false",
    "INDEX_WATCH_MODE_ENABLED": "false",
    "DYNAMIC_CAPITAL_RISK_ENABLED": "false",
    "DAILY_PROFIT_TARGET": "5000",
    "DAILY_MAX_LOSS": "5000",
    "DAILY_SOFT_LOSS": "0",
    "MAX_CONSECUTIVE_LOSSES": "0",
    "PEAK_PROFIT_GIVEBACK_TRIGGER": "0",
    "MAX_OPEN_PORTFOLIO_RISK": "0",
    "MAX_DAILY_INDEX_RISK": "0",
    "INDEX_RISK_PER_TRADE": "0",
    "DAILY_PROFIT_TARGET_ABSOLUTE_CAP": "0",
    "DAILY_MAX_LOSS_ABSOLUTE_CAP": "0",
    "DAILY_SOFT_LOSS_ABSOLUTE_CAP": "0",
    "PEAK_PROFIT_GIVEBACK_ABSOLUTE_CAP": "0",
    "INDEX_RISK_PER_TRADE_ABSOLUTE_CAP": "0",
    "MAX_DAILY_INDEX_RISK_ABSOLUTE_CAP": "0",
    "MAX_OPEN_PORTFOLIO_RISK_ABSOLUTE_CAP": "0",
    "VAMSI_ADAPTIVE_EXIT_MODE": "shadow",
    "VAMSI_ADAPTIVE_EXIT_LOOKBACK_DAYS": "90",
    "VAMSI_ADAPTIVE_EXIT_HORIZON_MINUTES": "60",
    "VAMSI_ADAPTIVE_EXIT_MIN_SAMPLES": "40",
    "VAMSI_ADAPTIVE_EXIT_MIN_TRADING_DAYS": "10",
    "VAMSI_ADAPTIVE_EXIT_VALIDATION_FRACTION": "0.30",
    "VAMSI_ADAPTIVE_EXIT_MIN_VALIDATION_SAMPLES": "10",
    "VAMSI_ADAPTIVE_EXIT_MIN_VALIDATION_PROFIT_FACTOR": "1.10",
    "VAMSI_ADAPTIVE_EXIT_MIN_REWARD_RISK": "0.80",
    "VAMSI_ADAPTIVE_EXIT_MAX_DAILY_CHANGE_PERCENT": "10",
    "VAMSI_ADAPTIVE_EXIT_TARGET_GRID": "10,15,20,25,30,35,40,45",
    "VAMSI_ADAPTIVE_EXIT_STOP_GRID": "10,15,20,25,30,35,40,45",
    "VAMSI_AUTO_ADAPTIVE_LIVE_ENABLED": "true",
    "VAMSI_ADAPTIVE_LIVE_LOOKBACK_DAYS": "90",
    "VAMSI_ADAPTIVE_LIVE_HORIZON_MINUTES": "60",
    "VAMSI_ADAPTIVE_LIVE_MIN_SAMPLES_PER_CELL": "40",
    "VAMSI_ADAPTIVE_LIVE_MIN_TRADING_DAYS": "10",
    "VAMSI_ADAPTIVE_LIVE_VALIDATION_FRACTION": "0.30",
    "VAMSI_ADAPTIVE_LIVE_MIN_VALIDATION_SAMPLES": "12",
    "VAMSI_ADAPTIVE_LIVE_MIN_VALIDATION_PROFIT_FACTOR": "1.20",
    "VAMSI_ADAPTIVE_LIVE_MIN_REWARD_RISK": "0.80",
    "VAMSI_ADAPTIVE_LIVE_MAX_DAILY_EXIT_CHANGE_PERCENT": "10",
    "VAMSI_ADAPTIVE_LIVE_REQUIRED_CALIBRATIONS": "3",
    "VAMSI_ADAPTIVE_MAX_LIVE_TRADES_CAP": "1",
    "VAMSI_ADAPTIVE_LIVE_TARGET_GRID": "10,15,20,25,30,35,40,45",
    "VAMSI_ADAPTIVE_LIVE_STOP_GRID": "10,15,20,25,30,35,40,45",
}

INSTANCE_OVERRIDE_KEYS = set(CORE_VALUES) | {"DAILY_PNL_GUARDS_ENABLED"}

DEPRECATED_KEYS = {
    "VAMSI_MIN_WEIGHTED_SCORE",
    "VAMSI_ENTRY_MIN_SCORE",
    "VAMSI_ADAPTIVE_LOOKBACK_DAYS",
    "VAMSI_ADAPTIVE_MIN_SAMPLES",
    "VAMSI_ADAPTIVE_MIN_TRADING_DAYS",
    "VAMSI_ADAPTIVE_MIN_SUCCESS_RATE",
    "VAMSI_ADAPTIVE_RANGE_ADVANTAGE",
    "VAMSI_ADAPTIVE_NIFTY_FAVORABLE_POINTS",
    "VAMSI_ADAPTIVE_BANKNIFTY_FAVORABLE_POINTS",
    "BANKNIFTY_TARGET_POINTS",
    "BANKNIFTY_STOP_POINTS",
    "BANKNIFTY_EXTREME_TARGET_POINTS",
}


def env_key(line):
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    return key if key.replace("_", "").isalnum() else None


def is_deprecated(key):
    sensitive_terms = ("API", "TOKEN", "SECRET", "PASSWORD", "CLIENT", "WEBHOOK")
    if any(term in key.upper() for term in sensitive_terms):
        return False
    return key in DEPRECATED_KEYS or key.startswith(("T20_", "GANESH_"))


def active_state_files(app_dir):
    active = []
    for path in sorted(Path(app_dir).glob("trade_state_*.json")):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            continue
        if state.get("instrument_key") and state.get("status") in ACTIVE_STATUSES:
            active.append(path.name)
    return active


def parse_instance_overrides(text):
    overrides = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key = env_key(stripped)
        if not key or "=" not in stripped:
            raise ValueError(f"Invalid instance override line: {line!r}")
        if key not in INSTANCE_OVERRIDE_KEYS:
            raise ValueError(f"Unsupported instance core override: {key}")
        overrides[key] = stripped.split("=", 1)[1].strip()
    return overrides


def normalized_lines(existing, overrides=None):
    values = dict(CORE_VALUES)
    values.update(overrides or {})
    result = []
    inside_old_block = False
    source_lines = existing.splitlines()
    complete_old_block = BLOCK_START in source_lines and BLOCK_END in source_lines
    for line in source_lines:
        if complete_old_block and line.strip() == BLOCK_START:
            inside_old_block = True
            continue
        if complete_old_block and line.strip() == BLOCK_END:
            inside_old_block = False
            continue
        if inside_old_block:
            continue
        key = env_key(line)
        if key in values or (key and is_deprecated(key)):
            continue
        result.append(line.rstrip())
    while result and not result[-1]:
        result.pop()
    result.extend(
        [
            "",
            BLOCK_START,
            "# Account-specific API keys, access tokens, and notification secrets above are preserved.",
            "# One real NIFTY trade; later qualified trades are paper observations.",
            *[f"{key}={value}" for key, value in values.items()],
            BLOCK_END,
            "",
        ]
    )
    return result


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, path.stat().st_mode if path.exists() else 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main():
    parser = argparse.ArgumentParser(
        description="Make the core NIFTY strategy env values canonical and deduplicated"
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--overrides-file",
        default=".core_env_overrides",
        help="Optional non-secret per-instance overrides, resolved beside the env file.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refuse-active-state", action="store_true")
    args = parser.parse_args()

    env_path = Path(args.env_file).expanduser().resolve()
    overrides_path = Path(args.overrides_file).expanduser()
    if not overrides_path.is_absolute():
        overrides_path = env_path.parent / overrides_path
    overrides = parse_instance_overrides(
        overrides_path.read_text() if overrides_path.exists() else ""
    )
    active = active_state_files(env_path.parent)
    if args.refuse_active_state and active:
        raise SystemExit(
            "Refusing configuration/restart while active bot state exists: "
            + ", ".join(active)
        )

    existing = env_path.read_text() if env_path.exists() else ""
    updated = "\n".join(normalized_lines(existing, overrides=overrides))
    if args.dry_run:
        print(
            f"Preflight OK: {len(CORE_VALUES)} canonical core values; "
            f"{len(overrides)} instance overrides; "
            "account secrets remain untouched"
        )
        return
    if updated == existing:
        print("Core env already canonical; no file change needed")
        return

    if env_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = env_path.with_name(f"{env_path.name}.backup-{timestamp}")
        shutil.copy2(env_path, backup)
        print(f"Backup created: {backup.name}")
    write_atomic(env_path, updated)
    print(
        f"Updated {env_path.name}: {len(CORE_VALUES)} canonical core values; "
        "duplicates/deprecated strategy values removed; secrets preserved"
    )


if __name__ == "__main__":
    main()
