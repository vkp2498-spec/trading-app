"""Live V2 opening model with probability-adjusted payoff filtering.

V2 makes one opening forecast from prior-session features and today's opening
price.  It remains separate from the post-09:20 V3 shadow model so their
predictions and evidence can be compared without sharing a prediction ledger.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from calendar import monthrange
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import ml_shadow_v1 as execution
from backtest_data import UpstoxBacktestData
from market_technicals import completed_candles, fetch_v3_intraday_minutes
from safe_storage import atomic_write_json, file_lock, locked_append_csv
from strategy_core import now_ist
from upstox_streams import read_market_cache


IST = execution.IST
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "ml_shadow_4h_v2"
MODEL_FILE = DATA_DIR / "model.joblib"
METADATA_FILE = DATA_DIR / "metadata.json"
PREDICTIONS_FILE = DATA_DIR / "predictions.csv"
MODEL_VERSION = "ML_SHADOW_4H_PERCENT_V2"
LOG_PREFIX = "ML_SHADOW_4H_V2_LIVE"
NIFTY_KEY = execution.NIFTY_KEY

FEATURE_COLUMNS = [
    "previous_return_percent", "previous_range_percent", "previous_body_percent",
    "previous_up_percent", "previous_down_percent", "previous_close_location",
    "opening_gap_percent", "previous_volume_ratio_20", "trend_5_percent",
    "trend_20_percent", "return_mean_5", "return_std_5", "range_mean_5",
    "up_mean_5", "down_mean_5", "return_mean_10", "return_std_10",
    "range_mean_10", "up_mean_10", "down_mean_10", "return_mean_20",
    "return_std_20", "range_mean_20", "up_mean_20", "down_mean_20",
    "return_mean_60", "return_std_60", "range_mean_60", "up_mean_60",
    "down_mean_60", "day_of_week", "month_sin", "month_cos",
]

PREDICTION_COLUMNS = [
    "scan_time", "candle_time", "model_trained_through", "model_hash",
    "underlying_open", "underlying_entry_price", "call_probability",
    "call_target_percent", "call_stop_percent", "call_reward_risk",
    "call_expected_value_r", "call_action", "call_reason", "put_probability",
    "put_target_percent", "put_stop_percent", "put_reward_risk",
    "put_expected_value_r", "put_action", "put_reason", "overall_action",
    "execution_mode", "future_up_percent", "future_down_percent",
    "call_outcome", "call_realized_percent", "put_outcome",
    "put_realized_percent", "resolved_at",
]


def log(message: str) -> None:
    print(f"{now_ist().strftime('%Y-%m-%d %H:%M:%S')} | {LOG_PREFIX} | {message}", flush=True)


def first_candle_per_day(candles: pd.DataFrame) -> pd.DataFrame:
    frame = execution._as_ist(candles)
    if frame.empty:
        return frame
    times = frame.index.time
    frame = frame[(times >= clock_time(9, 15)) & (times < clock_time(13, 15))]
    return frame.groupby(frame.index.date, sort=True).head(1).copy()


def fetch_training_candles() -> pd.DataFrame:
    calendar_days = max(execution.configured_int("ML_SHADOW_HISTORY_CALENDAR_DAYS", 800), 730)
    trading_days = max(execution.configured_int("ML_SHADOW_TRAINING_DAYS", 504), 300)
    end = now_ist().date() - timedelta(days=1)
    start = end - timedelta(days=calendar_days)
    source = UpstoxBacktestData(DATA_DIR / "history_cache", progress=log, pause_seconds=0.15)
    frames = []
    cursor = start.replace(day=1)
    while cursor <= end:
        chunk_start = max(start, cursor)
        chunk_end = min(end, cursor.replace(day=monthrange(cursor.year, cursor.month)[1]))
        frame = source.candles(NIFTY_KEY, "4hour", chunk_start, chunk_end, expired=False)
        if not frame.empty:
            frames.append(frame)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    candles = first_candle_per_day(
        pd.concat(frames).sort_index() if frames else pd.DataFrame()
    )
    available = sorted(set(candles.index.date))
    if len(available) > trading_days:
        candles = candles[candles.index.date >= available[-trading_days]]
    return candles


def _labeled_features(candles: pd.DataFrame) -> pd.DataFrame:
    features = execution.build_feature_frame(candles)
    labeled = features.copy()
    opening = labeled["open"].replace(0, np.nan)
    labeled["future_up_percent"] = ((labeled["high"] - opening) / opening * 100).clip(lower=0)
    labeled["future_down_percent"] = ((opening - labeled["low"]) / opening * 100).clip(lower=0)
    event_move = max(execution.configured_float("ML_SHADOW_EVENT_MOVE_PERCENT", 0.10), 0.01)
    labeled["call_label"] = (labeled["future_up_percent"] >= event_move).astype(int)
    labeled["put_label"] = (labeled["future_down_percent"] >= event_move).astype(int)
    return labeled


def expected_value_r(probability, reward_risk):
    return probability * reward_risk - (1 - probability)


def train() -> dict:
    execution.load_env()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    candles = fetch_training_candles()
    usable = _labeled_features(candles).dropna(
        subset=FEATURE_COLUMNS + ["call_label", "put_label"]
    )
    minimum_rows = max(execution.configured_int("ML_SHADOW_MIN_TRAINING_ROWS", 300), 240)
    if len(usable) < minimum_rows:
        raise RuntimeError(f"Need {minimum_rows} first-4H rows; found {len(usable)}")
    for label in ("call_label", "put_label"):
        if usable[label].nunique() < 2:
            raise RuntimeError(f"{label} requires both classes")

    sk = execution._sklearn_imports()
    validation_rows = min(max(int(len(usable) * 0.20), 60), len(usable) - 180)
    training = usable.iloc[:-validation_rows]
    validation = usable.iloc[-validation_rows:]
    X_train = training[FEATURE_COLUMNS]
    X_validation = validation[FEATURE_COLUMNS]
    threshold = execution.configured_float("ML_SHADOW_MIN_PROBABILITY", 0.50)
    minimum_ev = execution.configured_float("ML_SHADOW_MIN_EXPECTED_VALUE_R", 0.10)

    call_classifier = execution._classifier(sk).fit(X_train, training["call_label"])
    put_classifier = execution._classifier(sk).fit(X_train, training["put_label"])
    call_probability = execution._positive_probability(call_classifier, X_validation)
    put_probability = execution._positive_probability(put_classifier, X_validation)
    call_target_model = execution._regressor(sk, 0.50).fit(X_train, training["future_up_percent"])
    put_target_model = execution._regressor(sk, 0.50).fit(X_train, training["future_down_percent"])
    call_stop_model = execution._regressor(sk, 0.75).fit(X_train, training["future_down_percent"])
    put_stop_model = execution._regressor(sk, 0.75).fit(X_train, training["future_up_percent"])
    call_target = call_target_model.predict(X_validation)
    put_target = put_target_model.predict(X_validation)
    call_stop = call_stop_model.predict(X_validation)
    put_stop = put_stop_model.predict(X_validation)
    call_rr = call_target / np.maximum(call_stop, 0.001)
    put_rr = put_target / np.maximum(put_stop, 0.001)
    call_ev = expected_value_r(call_probability, call_rr)
    put_ev = expected_value_r(put_probability, put_rr)
    qualified = int(
        ((call_probability > threshold) & (call_ev >= minimum_ev)).sum()
        + ((put_probability > threshold) & (put_ev >= minimum_ev)).sum()
    )
    call_metrics = execution._binary_metrics(
        sk, validation["call_label"], call_probability, threshold
    )
    put_metrics = execution._binary_metrics(
        sk, validation["put_label"], put_probability, threshold
    )
    metrics = {
        "accuracy": round((call_metrics["accuracy"] + put_metrics["accuracy"]) / 2, 4),
        "majority_baseline_accuracy": round(
            (call_metrics["baseline_accuracy"] + put_metrics["baseline_accuracy"]) / 2, 4
        ),
        "rows": validation_rows,
        "qualified_direction_count": qualified,
        "qualified_direction_coverage": round(qualified / (validation_rows * 2), 4),
        "call": call_metrics,
        "put": put_metrics,
        "start": validation.index.min().isoformat(),
        "end": validation.index.max().isoformat(),
    }

    X = usable[FEATURE_COLUMNS]
    artifact = {
        "version": MODEL_VERSION,
        "feature_columns": FEATURE_COLUMNS,
        "call_classifier": execution._classifier(sk).fit(X, usable["call_label"]),
        "put_classifier": execution._classifier(sk).fit(X, usable["put_label"]),
        "call_target": execution._regressor(sk, 0.50).fit(X, usable["future_up_percent"]),
        "put_target": execution._regressor(sk, 0.50).fit(X, usable["future_down_percent"]),
        "call_stop": execution._regressor(sk, 0.75).fit(X, usable["future_down_percent"]),
        "put_stop": execution._regressor(sk, 0.75).fit(X, usable["future_up_percent"]),
        "trained_through": max(candles.index.date).isoformat(),
        "history_tail": candles.tail(90),
    }
    sk["joblib"].dump(artifact, MODEL_FILE)
    model_hash = hashlib.sha256(MODEL_FILE.read_bytes()).hexdigest()[:16]
    metadata = {
        "version": MODEL_VERSION,
        "status": "READY_LIVE",
        "timeframe": "FIRST_4H_FROM_OPEN",
        "generated_at": now_ist().isoformat(),
        "trained_through": artifact["trained_through"],
        "training_rows": len(usable),
        "training_days": len(set(usable.index.date)),
        "minimum_probability": threshold,
        "minimum_expected_value_r": minimum_ev,
        "validation": metrics,
        "model_hash": model_hash,
        "feature_columns": FEATURE_COLUMNS,
    }
    atomic_write_json(METADATA_FILE, metadata, sort_keys=True)
    log(
        f"trained through {artifact['trained_through']}; rows={len(usable)} "
        f"validation_accuracy={metrics['accuracy']} ev_qualified={qualified}"
    )
    return metadata


def load_artifact():
    sk = execution._sklearn_imports()
    if not MODEL_FILE.exists() or not METADATA_FILE.exists():
        raise RuntimeError("V2 model is missing; run --train")
    artifact = sk["joblib"].load(MODEL_FILE)
    metadata = json.loads(METADATA_FILE.read_text())
    if artifact.get("version") != MODEL_VERSION or metadata.get("status") not in {
        "READY_SHADOW", "READY_LIVE"
    }:
        raise RuntimeError("V2 model is not ready")
    trained_date = date.fromisoformat(str(artifact["trained_through"]))
    maximum_age = max(execution.configured_int("ML_SHADOW_MAX_MODEL_AGE_DAYS", 4), 1)
    if (now_ist().date() - trained_date).days > maximum_age:
        raise RuntimeError(f"V2 model is stale: trained through {trained_date}")
    return artifact, metadata


def fetch_today_open():
    candles = execution._as_ist(fetch_v3_intraday_minutes(NIFTY_KEY, minutes=1))
    candles = completed_candles(
        candles, 1, current_time=now_ist(),
        grace_seconds=max(execution.configured_float("ML_SHADOW_CANDLE_GRACE_SECONDS", 8), 5),
    )
    today = candles[candles.index.date == now_ist().date()]
    if today.empty:
        raise RuntimeError("The first completed NIFTY minute is not available")
    opening = float(today.iloc[0]["open"])
    quote = read_market_cache(NIFTY_KEY) or {}
    current_price = float(quote.get("ltp") or today.iloc[-1]["close"])
    timestamp = pd.Timestamp.combine(now_ist().date(), clock_time(9, 15)).tz_localize(IST)
    return timestamp, opening, current_price


def score_latest(artifact, metadata, live_data=None):
    candle_time, opening, current_price = live_data or fetch_today_open()
    history = first_candle_per_day(artifact["history_tail"])
    synthetic = pd.DataFrame(
        [{"open": opening, "high": opening, "low": opening, "close": opening, "volume": 0.0}],
        index=pd.DatetimeIndex([candle_time]),
    )
    features = execution.build_feature_frame(pd.concat([history, synthetic]).sort_index())
    row = features.loc[[candle_time], artifact["feature_columns"]]
    if float(row.isna().mean(axis=1).iloc[0]) > 0.10:
        raise RuntimeError("V2 opening feature row is incomplete")
    result = {
        "candle_time": candle_time.isoformat(),
        "model_trained_through": artifact["trained_through"],
        "model_hash": metadata["model_hash"],
        "model_version": MODEL_VERSION,
        "underlying_open": opening,
        "underlying_entry_price": current_price,
        "minimum_execution_reward_risk": 0.0,
        "minimum_expected_value_r": execution.configured_float(
            "ML_SHADOW_MIN_EXPECTED_VALUE_R", 0.10
        ),
    }
    for prefix in ("call", "put"):
        probability = float(execution._positive_probability(artifact[f"{prefix}_classifier"], row)[0])
        target = max(float(artifact[f"{prefix}_target"].predict(row)[0]), 0.001)
        stop = max(float(artifact[f"{prefix}_stop"].predict(row)[0]), 0.001)
        reward_risk = target / stop
        result.update({
            f"{prefix}_probability": probability,
            f"{prefix}_target_percent": target,
            f"{prefix}_stop_percent": stop,
            f"{prefix}_reward_risk": reward_risk,
            f"{prefix}_expected_value_r": expected_value_r(probability, reward_risk),
        })
    return result


def choose_actions(prediction):
    threshold = execution.configured_float("ML_SHADOW_MIN_PROBABILITY", 0.50)
    minimum_ev = execution.configured_float("ML_SHADOW_MIN_EXPECTED_VALUE_R", 0.10)
    decisions = {}
    for direction, prefix in (("CALL", "call"), ("PUT", "put")):
        probability = float(prediction[f"{prefix}_probability"])
        reward_risk = float(prediction[f"{prefix}_reward_risk"])
        ev = expected_value_r(probability, reward_risk)
        qualified = probability > threshold and ev >= minimum_ev
        decisions[direction] = {
            "direction": direction,
            "qualified": qualified,
            "probability": probability,
            "target_percent": float(prediction[f"{prefix}_target_percent"]),
            "stop_percent": float(prediction[f"{prefix}_stop_percent"]),
            "reward_risk": reward_risk,
            "expected_value_r": ev,
            "reason": (
                f"probability and EV proxy qualified at {ev:+.3f}R"
                if qualified else
                f"requires probability > {threshold:.0%} and EV proxy >= {minimum_ev:+.2f}R; got {ev:+.3f}R"
            ),
        }
    return decisions


def append_prediction(prediction):
    if PREDICTIONS_FILE.exists():
        with file_lock(PREDICTIONS_FILE.with_suffix(".csv.lock")):
            with PREDICTIONS_FILE.open(newline="") as handle:
                reader = csv.DictReader(handle)
                existing_fields = reader.fieldnames or []
                existing_rows = list(reader)
            if existing_fields != PREDICTION_COLUMNS:
                temporary = PREDICTIONS_FILE.with_suffix(".csv.schema.tmp")
                with temporary.open("w", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=PREDICTION_COLUMNS)
                    writer.writeheader()
                    writer.writerows(
                        {column: row.get(column, "") for column in PREDICTION_COLUMNS}
                        for row in existing_rows
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(PREDICTIONS_FILE)
    locked_append_csv(
        PREDICTIONS_FILE, PREDICTION_COLUMNS,
        {column: prediction.get(column, "") for column in PREDICTION_COLUMNS},
    )


def prediction_already_recorded(candle_time):
    if not PREDICTIONS_FILE.exists():
        return False
    with PREDICTIONS_FILE.open(newline="") as handle:
        return any(row.get("candle_time") == candle_time for row in csv.DictReader(handle))


def resolve_prediction_outcomes(candles):
    if not PREDICTIONS_FILE.exists() or candles.empty:
        return 0
    by_date = {timestamp.date(): row for timestamp, row in first_candle_per_day(candles).iterrows()}
    changed = 0
    with file_lock(PREDICTIONS_FILE.with_suffix(".csv.lock")):
        with PREDICTIONS_FILE.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            if row.get("resolved_at") or not row.get("candle_time"):
                continue
            try:
                candle = by_date[pd.Timestamp(row["candle_time"]).date()]
                opening = float(candle["open"])
            except (KeyError, TypeError, ValueError):
                continue
            up = max((float(candle["high"]) - opening) / opening * 100, 0)
            down = max((opening - float(candle["low"])) / opening * 100, 0)
            close_change = (float(candle["close"]) - opening) / opening * 100
            row["future_up_percent"] = round(up, 4)
            row["future_down_percent"] = round(down, 4)
            for direction, prefix in (("CALL", "call"), ("PUT", "put")):
                outcome, realized = execution._direction_outcome(
                    direction, up, down, close_change,
                    float(row.get(f"{prefix}_target_percent") or 0),
                    float(row.get(f"{prefix}_stop_percent") or 0),
                )
                row[f"{prefix}_outcome"] = outcome
                row[f"{prefix}_realized_percent"] = round(realized, 4)
            row["resolved_at"] = now_ist().isoformat()
            changed += 1
        if changed:
            temporary = PREDICTIONS_FILE.with_suffix(".csv.tmp")
            with temporary.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=PREDICTION_COLUMNS)
                writer.writeheader()
                writer.writerows(
                    {column: row.get(column, "") for column in PREDICTION_COLUMNS}
                    for row in rows
                )
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(PREDICTIONS_FILE)
    return changed


def fetch_resolution_candles():
    end = now_ist().date()
    source = UpstoxBacktestData(DATA_DIR / "history_cache", progress=log, pause_seconds=0.1)
    historical = source.candles(NIFTY_KEY, "4hour", end - timedelta(days=10), end - timedelta(days=1), expired=False)
    intraday = source.candles(NIFTY_KEY, "4hour", end, end, expired=False)
    valid = [frame for frame in (historical, intraday) if not frame.empty]
    return pd.concat(valid).sort_index() if valid else pd.DataFrame()


def live_enabled():
    return (
        execution.configured_bool("ENABLE_LIVE_TRADING", False)
        and execution.configured_bool("ML_SHADOW_LIVE_TRADING_ENABLED", False)
        and execution.configured_bool("ML_SHADOW_V2_LIVE_ENABLED", False)
    )


def scan():
    execution.load_env()
    if not live_enabled():
        raise RuntimeError("V2 live requires all three live switches")
    current_minutes = now_ist().hour * 60 + now_ist().minute
    first_entry = execution._configured_minutes("ML_SHADOW_V2_FIRST_ENTRY_TIME", "09:17")
    last_entry = execution._configured_minutes("ML_SHADOW_V2_LAST_ENTRY_TIME", "09:30")
    if current_minutes >= 13 * 60 + 16:
        resolved = resolve_prediction_outcomes(fetch_resolution_candles())
        log(f"resolution pass; resolved={resolved}")
        return {"overall_action": "RESOLVE_ONLY", "resolved": resolved}
    if not first_entry <= current_minutes <= last_entry:
        raise RuntimeError("Outside V2 opening entry window")
    artifact, metadata = load_artifact()
    prediction = score_latest(artifact, metadata)
    if prediction_already_recorded(prediction["candle_time"]):
        log(f"forecast for {prediction['candle_time']} already recorded")
        return {"overall_action": "DUPLICATE", **prediction}
    decisions = choose_actions(prediction)
    actions = []
    for direction, prefix in (("CALL", "call"), ("PUT", "put")):
        decision = decisions[direction]
        action = "NO_TRADE"
        reason = decision["reason"]
        if decision["qualified"]:
            if execution.read_state(direction).get("instrument_key"):
                reason = f"{direction} lane already has active state"
            else:
                try:
                    execution.place_live_gtt(prediction, decision)
                    action = "LIVE_GTT"
                    reason = decision["reason"]
                except Exception as error:
                    action = "ERROR"
                    reason = str(error)
        prediction[f"{prefix}_action"] = action
        prediction[f"{prefix}_reason"] = reason
        actions.append(action)
    entries = [action for action in actions if action == "LIVE_GTT"]
    prediction.update({
        "scan_time": now_ist().isoformat(),
        "execution_mode": "LIVE_GTT",
        "overall_action": "BOTH" if len(entries) == 2 else entries[0] if entries else "NO_TRADE",
    })
    append_prediction(prediction)
    log(
        f"call={prediction['call_probability']:.3f}/rr{prediction['call_reward_risk']:.2f}/"
        f"ev{prediction['call_expected_value_r']:+.3f}/{prediction['call_action']} "
        f"put={prediction['put_probability']:.3f}/rr{prediction['put_reward_risk']:.2f}/"
        f"ev{prediction['put_expected_value_r']:+.3f}/{prediction['put_action']}"
    )
    return prediction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--scan", action="store_true")
    args = parser.parse_args()
    if args.train:
        train()
    elif args.scan:
        scan()
    else:
        parser.error("choose --train or --scan")


if __name__ == "__main__":
    main()
