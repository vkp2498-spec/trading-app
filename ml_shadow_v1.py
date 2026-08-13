"""ML_SHADOW_V1: leakage-safe NIFTY 15-minute paper-trading engine.

The model is trained nightly through the previous trading day. During the
session it scores only completed 15-minute candles and may open at most one
non-overlapping one-lot NIFTY option paper position. It never places a broker
order, even if account-level live trading variables are accidentally enabled.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import time
from calendar import monthrange
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backtest_data import UpstoxBacktestData
from market_technicals import (
    completed_candles,
    fetch_v3_historical_minutes,
    fetch_v3_intraday_minutes,
    merge_candles,
)
from safe_storage import atomic_write_json, file_lock, locked_append_csv
from strategy_core import (
    choose_expiry,
    fetch_upstox_option_chain,
    get_expiries_from_upstox,
    now_ist,
)
from trade_journal import record_closed_trade
from upstox_streams import (
    read_market_cache,
    read_stream_instruments,
    write_stream_instruments,
)


IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "ml_shadow_v1"
MODEL_FILE = DATA_DIR / "model.joblib"
METADATA_FILE = DATA_DIR / "metadata.json"
PREDICTIONS_FILE = DATA_DIR / "predictions.csv"
STATE_FILE = BASE_DIR / "trade_state_ML_SHADOW_NIFTY.json"
ENV_FILE = BASE_DIR / ".env"
LOG_PREFIX = "ML_SHADOW_V1"
VERSION = "ML_SHADOW_V1"
NIFTY_KEY = "NSE_INDEX|Nifty 50"

FEATURE_COLUMNS = [
    "return_1",
    "return_2",
    "return_4",
    "return_8",
    "candle_range",
    "body",
    "body_ratio",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "close_location",
    "atr_4",
    "atr_8",
    "atr_14",
    "realized_vol_4",
    "realized_vol_8",
    "realized_vol_16",
    "distance_ema_4",
    "distance_ema_8",
    "distance_ema_16",
    "distance_vwap",
    "distance_session_open",
    "distance_session_high",
    "distance_session_low",
    "opening_gap",
    "volume_ratio_8",
    "range_ratio_8",
    "day_of_week",
    "time_sin",
    "time_cos",
    "session_candle_number",
]

PREDICTION_COLUMNS = [
    "scan_time",
    "candle_time",
    "model_trained_through",
    "model_hash",
    "call_probability",
    "put_probability",
    "none_probability",
    "call_target_points",
    "call_stop_points",
    "call_reward_risk",
    "put_target_points",
    "put_stop_points",
    "put_reward_risk",
    "direction",
    "selected_probability",
    "expected_target_points",
    "expected_stop_points",
    "reward_risk",
    "action",
    "reason",
    "underlying_entry_price",
    "future_up_points",
    "future_down_points",
    "call_target_hit",
    "call_stop_hit",
    "put_target_hit",
    "put_stop_hit",
    "selected_outcome",
    "selected_realized_points",
    "resolved_at",
]


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def configured_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def configured_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be numeric") from error


def configured_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be an integer") from error


def log(message: str) -> None:
    print(f"{now_ist().strftime('%Y-%m-%d %H:%M:%S')} | {LOG_PREFIX} | {message}", flush=True)


def _as_ist(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    index = pd.DatetimeIndex(result.index)
    if index.tz is None:
        index = index.tz_localize(IST)
    else:
        index = index.tz_convert(IST)
    result.index = index
    return result.sort_index()[~result.index.duplicated(keep="last")]


def market_candles_only(frame: pd.DataFrame) -> pd.DataFrame:
    frame = _as_ist(frame)
    if frame.empty:
        return frame
    times = frame.index.time
    return frame[(times >= clock_time(9, 15)) & (times <= clock_time(15, 15))].copy()


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.replace(0, np.nan)


def build_feature_frame(candles: pd.DataFrame) -> pd.DataFrame:
    """Create point-in-time features using current/past candles only."""
    frame = market_candles_only(candles)
    if frame.empty:
        return frame
    required = {"open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError("Missing candle fields: " + ", ".join(sorted(missing)))
    for column in ["open", "high", "low", "close", "volume"]:
        if column not in frame:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    session = pd.Series(frame.index.date, index=frame.index)
    grouped = frame.groupby(session, sort=False)
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    candle_range = frame["high"] - frame["low"]
    body = frame["close"] - frame["open"]
    upper_wick = frame["high"] - frame[["open", "close"]].max(axis=1)
    lower_wick = frame[["open", "close"]].min(axis=1) - frame["low"]
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0

    volume = frame["volume"].fillna(0.0)
    cumulative_volume = volume.groupby(session).cumsum()
    volume_vwap = (typical * volume).groupby(session).cumsum()
    fallback_vwap = typical.groupby(session).expanding().mean().reset_index(level=0, drop=True)
    session_vwap = (volume_vwap / cumulative_volume.replace(0, np.nan)).fillna(fallback_vwap)
    session_open = grouped["open"].transform("first")
    session_high = grouped["high"].cummax()
    session_low = grouped["low"].cummin()
    session_number = grouped.cumcount() + 1

    daily_last_close = frame.groupby(session)["close"].last()
    previous_daily_close = daily_last_close.shift(1)
    previous_close_by_day = session.map(previous_daily_close)
    opening_gap = _safe_divide(session_open - previous_close_by_day, previous_close_by_day)

    result = frame.copy()
    result["return_1"] = frame["close"].pct_change(1)
    result["return_2"] = frame["close"].pct_change(2)
    result["return_4"] = frame["close"].pct_change(4)
    result["return_8"] = frame["close"].pct_change(8)
    result["candle_range"] = candle_range
    result["body"] = body
    result["body_ratio"] = _safe_divide(body, candle_range)
    result["upper_wick_ratio"] = _safe_divide(upper_wick, candle_range)
    result["lower_wick_ratio"] = _safe_divide(lower_wick, candle_range)
    result["close_location"] = _safe_divide(frame["close"] - frame["low"], candle_range)
    for window in (4, 8, 14):
        result[f"atr_{window}"] = true_range.rolling(window, min_periods=window).mean()
    returns = frame["close"].pct_change()
    for window in (4, 8, 16):
        result[f"realized_vol_{window}"] = returns.rolling(window, min_periods=window).std()
        ema = frame["close"].ewm(span=window, adjust=False, min_periods=window).mean()
        result[f"distance_ema_{window}"] = _safe_divide(frame["close"] - ema, ema)
    result["distance_vwap"] = _safe_divide(frame["close"] - session_vwap, session_vwap)
    result["distance_session_open"] = _safe_divide(frame["close"] - session_open, session_open)
    result["distance_session_high"] = _safe_divide(frame["close"] - session_high, session_high)
    result["distance_session_low"] = _safe_divide(frame["close"] - session_low, session_low)
    result["opening_gap"] = opening_gap
    rolling_volume = volume.rolling(8, min_periods=4).mean()
    result["volume_ratio_8"] = _safe_divide(volume, rolling_volume).fillna(1.0)
    rolling_range = candle_range.rolling(8, min_periods=4).mean()
    result["range_ratio_8"] = _safe_divide(candle_range, rolling_range)
    result["day_of_week"] = frame.index.dayofweek.astype(float)
    minutes = frame.index.hour * 60 + frame.index.minute
    phase = (minutes - (9 * 60 + 15)) / (6.25 * 60) * 2 * math.pi
    result["time_sin"] = np.sin(phase)
    result["time_cos"] = np.cos(phase)
    result["session_candle_number"] = session_number.astype(float)
    result.replace([np.inf, -np.inf], np.nan, inplace=True)
    return result


def add_forward_labels(features: pd.DataFrame, horizon_candles: int, minimum_move: float) -> pd.DataFrame:
    """Attach same-session future excursions without exposing them as features."""
    labeled = features.copy()
    upward = pd.Series(np.nan, index=labeled.index, dtype=float)
    downward = pd.Series(np.nan, index=labeled.index, dtype=float)
    for _day, positions in labeled.groupby(labeled.index.date, sort=False).groups.items():
        day_frame = labeled.loc[positions]
        for offset in range(len(day_frame)):
            future = day_frame.iloc[offset + 1 : offset + 1 + horizon_candles]
            if len(future) != horizon_candles:
                continue
            close = float(day_frame.iloc[offset]["close"])
            upward.loc[day_frame.index[offset]] = max(float(future["high"].max()) - close, 0.0)
            downward.loc[day_frame.index[offset]] = max(close - float(future["low"].min()), 0.0)
    labeled["future_up_points"] = upward
    labeled["future_down_points"] = downward
    # Direction is a binary calibrated question: which side has the larger
    # excursion over the forecast horizon? The separate predicted-move and
    # reward/risk gates decide whether that direction is actually tradable.
    labeled["label"] = np.where(upward >= downward, "CALL", "PUT")
    return labeled.dropna(subset=["future_up_points", "future_down_points"])


def _sklearn_imports():
    try:
        import joblib
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.ensemble import GradientBoostingRegressor, HistGradientBoostingClassifier
        from sklearn.impute import SimpleImputer
        from sklearn.metrics import accuracy_score, log_loss
        from sklearn.model_selection import TimeSeriesSplit
        from sklearn.pipeline import Pipeline
    except ImportError as error:
        raise RuntimeError("Install requirements.txt before training ML_SHADOW_V1") from error
    return {
        "joblib": joblib,
        "CalibratedClassifierCV": CalibratedClassifierCV,
        "GradientBoostingRegressor": GradientBoostingRegressor,
        "HistGradientBoostingClassifier": HistGradientBoostingClassifier,
        "SimpleImputer": SimpleImputer,
        "accuracy_score": accuracy_score,
        "log_loss": log_loss,
        "TimeSeriesSplit": TimeSeriesSplit,
        "Pipeline": Pipeline,
    }


def _classifier(sk, horizon: int):
    base = sk["Pipeline"](
        [
            ("imputer", sk["SimpleImputer"](strategy="median")),
            (
                "model",
                sk["HistGradientBoostingClassifier"](
                    learning_rate=0.04,
                    max_iter=180,
                    max_leaf_nodes=15,
                    l2_regularization=1.0,
                    random_state=41,
                ),
            ),
        ]
    )
    splitter = sk["TimeSeriesSplit"](n_splits=5, gap=max(horizon, 1))
    return sk["CalibratedClassifierCV"](base, method="sigmoid", cv=splitter)


def _regressor(sk, quantile: float):
    return sk["Pipeline"](
        [
            ("imputer", sk["SimpleImputer"](strategy="median")),
            (
                "model",
                sk["GradientBoostingRegressor"](
                    loss="quantile",
                    alpha=quantile,
                    n_estimators=140,
                    learning_rate=0.04,
                    max_depth=2,
                    min_samples_leaf=12,
                    random_state=41,
                ),
            ),
        ]
    )


def _probability_map(model, values) -> dict[str, float]:
    return {str(label): float(value) for label, value in zip(model.classes_, values)}


def fetch_training_candles() -> pd.DataFrame:
    calendar_days = max(configured_int("ML_SHADOW_HISTORY_CALENDAR_DAYS", 300), 200)
    trading_days = max(configured_int("ML_SHADOW_TRAINING_DAYS", 180), 60)
    end = now_ist().date() - timedelta(days=1)
    start = end - timedelta(days=calendar_days)
    source = UpstoxBacktestData(
        DATA_DIR / "history_cache",
        progress=log,
        pause_seconds=0.15,
    )
    # Use calendar-month cache keys. Completed months remain immutable, so the
    # daily trainer normally downloads only the current partial month.
    frames = []
    cursor = start.replace(day=1)
    while cursor <= end:
        chunk_start = max(start, cursor)
        chunk_end = min(end, cursor.replace(day=monthrange(cursor.year, cursor.month)[1]))
        frame = source.candles(
            NIFTY_KEY,
            "15minute",
            chunk_start,
            chunk_end,
            expired=False,
        )
        if not frame.empty:
            frames.append(frame)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    candles = pd.concat(frames).sort_index() if frames else pd.DataFrame()
    candles = market_candles_only(candles)
    available_days = sorted(set(candles.index.date))
    if len(available_days) > trading_days:
        candles = candles[candles.index.date >= available_days[-trading_days]]
    return candles


def train() -> dict:
    load_env()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    horizon = max(configured_int("ML_SHADOW_HORIZON_CANDLES", 4), 1)
    minimum_move = max(configured_float("ML_SHADOW_MIN_EXPECTED_POINTS", 10.0), 0.1)
    minimum_rows = max(configured_int("ML_SHADOW_MIN_TRAINING_ROWS", 1500), 300)
    candles = fetch_training_candles()
    features = build_feature_frame(candles)
    labeled = add_forward_labels(features, horizon, minimum_move)
    usable = labeled.dropna(subset=FEATURE_COLUMNS + ["label"])
    if len(usable) < minimum_rows:
        metadata = {
            "version": VERSION,
            "status": "INSUFFICIENT_DATA",
            "rows": int(len(usable)),
            "minimum_rows": minimum_rows,
            "generated_at": now_ist().isoformat(),
        }
        atomic_write_json(METADATA_FILE, metadata, sort_keys=True)
        raise RuntimeError(f"Need {minimum_rows} labeled candles; found {len(usable)}")
    class_counts = usable["label"].value_counts().to_dict()
    if set(class_counts) != {"CALL", "PUT"}:
        raise RuntimeError(f"Both CALL and PUT classes are required: {class_counts}")

    sk = _sklearn_imports()
    validation_rows = max(int(len(usable) * 0.20), 200)
    validation_start = len(usable) - validation_rows
    # Forward labels span `horizon` future candles. Purge those rows from the
    # training edge so no validation candle contributes to a training label.
    train_frame = usable.iloc[: max(validation_start - horizon, 1)]
    validation = usable.iloc[-validation_rows:]
    X_train = train_frame[FEATURE_COLUMNS]
    y_train = train_frame["label"]
    validation_model = _classifier(sk, horizon)
    validation_model.fit(X_train, y_train)
    validation_probabilities = validation_model.predict_proba(validation[FEATURE_COLUMNS])
    validation_prediction = validation_model.classes_[np.argmax(validation_probabilities, axis=1)]
    validation_confidence = np.max(validation_probabilities, axis=1)
    shadow_threshold = configured_float("ML_SHADOW_MIN_PROBABILITY", 0.70)
    qualified_mask = validation_confidence >= shadow_threshold
    validation_targets = pd.get_dummies(validation["label"]).reindex(
        columns=validation_model.classes_, fill_value=0
    )
    call_target_validation = _regressor(sk, 0.50).fit(
        X_train, train_frame["future_up_points"]
    )
    put_target_validation = _regressor(sk, 0.50).fit(
        X_train, train_frame["future_down_points"]
    )
    call_stop_validation = _regressor(sk, 0.75).fit(
        X_train, train_frame["future_down_points"]
    )
    put_stop_validation = _regressor(sk, 0.75).fit(
        X_train, train_frame["future_up_points"]
    )
    predicted_call_target = call_target_validation.predict(validation[FEATURE_COLUMNS])
    predicted_put_target = put_target_validation.predict(validation[FEATURE_COLUMNS])
    predicted_call_stop = call_stop_validation.predict(validation[FEATURE_COLUMNS])
    predicted_put_stop = put_stop_validation.predict(validation[FEATURE_COLUMNS])
    metrics = {
        "accuracy": round(float(sk["accuracy_score"](validation["label"], validation_prediction)), 4),
        "majority_baseline_accuracy": round(
            float(validation["label"].value_counts(normalize=True).max()), 4
        ),
        "log_loss": round(
            float(
                sk["log_loss"](
                    validation["label"],
                    validation_probabilities,
                    labels=list(validation_model.classes_),
                )
            ),
            4,
        ),
        "multiclass_brier_score": round(
            float(
                np.mean(
                    np.sum(
                        (validation_probabilities - validation_targets.to_numpy()) ** 2,
                        axis=1,
                    )
                )
            ),
            4,
        ),
        "qualified_direction_count": int(qualified_mask.sum()),
        "qualified_direction_coverage": round(float(qualified_mask.mean()), 4),
        "qualified_direction_precision": round(
            float(
                np.mean(
                    validation_prediction[qualified_mask]
                    == validation["label"].to_numpy()[qualified_mask]
                )
            ),
            4,
        )
        if qualified_mask.any()
        else None,
        "call_target_mae_points": round(
            float(np.mean(np.abs(predicted_call_target - validation["future_up_points"]))), 2
        ),
        "put_target_mae_points": round(
            float(np.mean(np.abs(predicted_put_target - validation["future_down_points"]))), 2
        ),
        "call_stop_coverage": round(
            float(np.mean(validation["future_down_points"] <= predicted_call_stop)), 4
        ),
        "put_stop_coverage": round(
            float(np.mean(validation["future_up_points"] <= predicted_put_stop)), 4
        ),
        "rows": validation_rows,
        "start": validation.index.min().isoformat(),
        "end": validation.index.max().isoformat(),
    }

    X = usable[FEATURE_COLUMNS]
    classifier = _classifier(sk, horizon)
    classifier.fit(X, usable["label"])
    call_target = _regressor(sk, 0.50).fit(X, usable["future_up_points"])
    put_target = _regressor(sk, 0.50).fit(X, usable["future_down_points"])
    call_stop = _regressor(sk, 0.75).fit(X, usable["future_down_points"])
    put_stop = _regressor(sk, 0.75).fit(X, usable["future_up_points"])

    trained_through = max(candles.index.date).isoformat()
    artifact = {
        "version": VERSION,
        "feature_columns": FEATURE_COLUMNS,
        "classifier": classifier,
        "call_target": call_target,
        "put_target": put_target,
        "call_stop": call_stop,
        "put_stop": put_stop,
        "horizon_candles": horizon,
        "minimum_move": minimum_move,
        "trained_through": trained_through,
    }
    sk["joblib"].dump(artifact, MODEL_FILE)
    model_hash = hashlib.sha256(MODEL_FILE.read_bytes()).hexdigest()[:16]
    metadata = {
        "version": VERSION,
        "status": "READY_SHADOW",
        "generated_at": now_ist().isoformat(),
        "trained_through": trained_through,
        "training_rows": int(len(usable)),
        "training_days": int(len(set(usable.index.date))),
        "class_counts": {str(key): int(value) for key, value in class_counts.items()},
        "horizon_candles": horizon,
        "horizon_minutes": horizon * 15,
        "minimum_move": minimum_move,
        "validation": metrics,
        "model_hash": model_hash,
        "feature_columns": FEATURE_COLUMNS,
    }
    atomic_write_json(METADATA_FILE, metadata, sort_keys=True)
    log(
        f"trained through {trained_through}; rows={len(usable)} days={metadata['training_days']} "
        f"validation_accuracy={metrics['accuracy']} log_loss={metrics['log_loss']}"
    )
    return metadata


def load_artifact():
    if not MODEL_FILE.exists() or not METADATA_FILE.exists():
        raise RuntimeError("ML model is missing; run --train")
    sk = _sklearn_imports()
    artifact = sk["joblib"].load(MODEL_FILE)
    metadata = json.loads(METADATA_FILE.read_text())
    if artifact.get("version") != VERSION or metadata.get("status") != "READY_SHADOW":
        raise RuntimeError("ML model is not READY_SHADOW")
    if artifact.get("trained_through") >= now_ist().date().isoformat():
        raise RuntimeError("ML model includes the current live day")
    trained_date = date.fromisoformat(str(artifact.get("trained_through")))
    maximum_age = max(configured_int("ML_SHADOW_MAX_MODEL_AGE_DAYS", 4), 1)
    if (now_ist().date() - trained_date).days > maximum_age:
        raise RuntimeError(
            f"ML model is stale: trained through {trained_date}; max age is {maximum_age} days"
        )
    return artifact, metadata


def fetch_live_features() -> tuple[pd.DataFrame, pd.Timestamp, int]:
    # Upstox rejects oversized minute-history windows in one request. Twenty
    # calendar days comfortably warms the longest 16-candle feature.
    historical = fetch_v3_historical_minutes(NIFTY_KEY, minutes=15, lookback_days=20)
    intraday = fetch_v3_intraday_minutes(NIFTY_KEY, minutes=15)
    candles = merge_candles(historical, intraday)
    candles = completed_candles(
        candles,
        15,
        current_time=now_ist(),
        grace_seconds=max(configured_float("ML_SHADOW_CANDLE_GRACE_SECONDS", 8.0), 5.0),
    )
    candles = market_candles_only(candles)
    if candles.empty:
        raise RuntimeError("No completed NIFTY 15-minute candle is available")
    features = build_feature_frame(candles)
    latest_time = pd.Timestamp(candles.index[-1])
    today_count = int(sum(day == now_ist().date() for day in candles.index.date))
    return features, latest_time, today_count


def score_latest(artifact, metadata, live_data=None) -> dict:
    features, candle_time, today_count = live_data or fetch_live_features()
    minimum_session_candles = max(configured_int("ML_SHADOW_OPENING_OBSERVATION_CANDLES", 2), 0)
    if candle_time.date() != now_ist().date():
        raise RuntimeError("Latest completed candle is not from today")
    if today_count < minimum_session_candles:
        raise RuntimeError(
            f"Opening observation incomplete: {today_count}/{minimum_session_candles} candles"
        )
    row = features.loc[[candle_time], artifact["feature_columns"]]
    if row.isna().any(axis=None):
        # The stored pipeline imputes ordinary missing values, but a completely
        # unavailable feature family indicates insufficient current context.
        missing_fraction = float(row.isna().mean(axis=1).iloc[0])
        if missing_fraction > 0.20:
            raise RuntimeError(f"Latest feature row is {missing_fraction:.0%} incomplete")
    probability_values = artifact["classifier"].predict_proba(row)[0]
    probabilities = _probability_map(artifact["classifier"], probability_values)
    call_target = max(float(artifact["call_target"].predict(row)[0]), 0.0)
    put_target = max(float(artifact["put_target"].predict(row)[0]), 0.0)
    call_stop = max(float(artifact["call_stop"].predict(row)[0]), 0.1)
    put_stop = max(float(artifact["put_stop"].predict(row)[0]), 0.1)
    call_rr = call_target / call_stop
    put_rr = put_target / put_stop
    return {
        "candle_time": candle_time.isoformat(),
        "model_trained_through": artifact["trained_through"],
        "model_hash": metadata["model_hash"],
        "call_probability": probabilities.get("CALL", 0.0),
        "put_probability": probabilities.get("PUT", 0.0),
        "none_probability": probabilities.get("NONE", 0.0),
        "call_target_points": call_target,
        "call_stop_points": call_stop,
        "call_reward_risk": call_rr,
        "put_target_points": put_target,
        "put_stop_points": put_stop,
        "put_reward_risk": put_rr,
        "underlying_entry_price": float(features.loc[candle_time, "close"]),
        "session_candle_number": today_count,
    }


def choose_action(prediction: dict) -> dict:
    threshold = configured_float("ML_SHADOW_MIN_PROBABILITY", 0.70)
    minimum_points = configured_float("ML_SHADOW_MIN_EXPECTED_POINTS", 10.0)
    minimum_rr = configured_float("ML_SHADOW_MIN_REWARD_RISK", 0.80)
    probability_gap = configured_float("ML_SHADOW_MIN_DIRECTION_PROBABILITY_GAP", 0.05)
    candidates = []
    for direction, prefix in (("CALL", "call"), ("PUT", "put")):
        probability = float(prediction[f"{prefix}_probability"])
        target = float(prediction[f"{prefix}_target_points"])
        stop = float(prediction[f"{prefix}_stop_points"])
        reward_risk = float(prediction[f"{prefix}_reward_risk"])
        expected_value = probability * target - (1.0 - probability) * stop
        if probability >= threshold and target >= minimum_points and reward_risk >= minimum_rr and expected_value > 0:
            candidates.append(
                {
                    "direction": direction,
                    "probability": probability,
                    "target_points": target,
                    "stop_points": stop,
                    "reward_risk": reward_risk,
                    "expected_value_points": expected_value,
                }
            )
    if not candidates:
        return {"action": "NO_TRADE", "reason": "probability/points/reward-risk rule did not qualify"}
    candidates.sort(key=lambda item: (item["expected_value_points"], item["probability"]), reverse=True)
    if len(candidates) > 1 and abs(candidates[0]["probability"] - candidates[1]["probability"]) < probability_gap:
        return {"action": "NO_TRADE", "reason": "CALL and PUT probabilities are too close"}
    selected = candidates[0]
    selected.update({"action": "PAPER_ENTRY", "reason": "calibrated ML rule qualified"})
    return selected


def read_state() -> dict:
    try:
        value = json.loads(STATE_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_state(state: dict) -> None:
    atomic_write_json(STATE_FILE, state, sort_keys=True)


def append_prediction(prediction: dict) -> None:
    row = {column: prediction.get(column, "") for column in PREDICTION_COLUMNS}
    locked_append_csv(PREDICTIONS_FILE, PREDICTION_COLUMNS, row)


def _selected_forecast_outcome(
    future: pd.DataFrame,
    direction: str,
    entry: float,
    target: float,
    stop: float,
) -> tuple[str, float]:
    if direction not in {"CALL", "PUT"} or target <= 0 or stop <= 0:
        return "", 0.0
    for _, candle in future.iterrows():
        if direction == "CALL":
            target_hit = float(candle["high"]) >= entry + target
            stop_hit = float(candle["low"]) <= entry - stop
        else:
            target_hit = float(candle["low"]) <= entry - target
            stop_hit = float(candle["high"]) >= entry + stop
        # OHLC cannot reveal intrabar ordering. Score an ambiguous candle
        # conservatively as a stop rather than inflating shadow performance.
        if stop_hit:
            return "STOP", -stop
        if target_hit:
            return "TARGET", target
    final_close = float(future.iloc[-1]["close"])
    realized = final_close - entry if direction == "CALL" else entry - final_close
    return "HORIZON", realized


def resolve_prediction_outcomes(candles: pd.DataFrame) -> int:
    """Resolve matured forecasts with the exact future candles predicted."""
    if not PREDICTIONS_FILE.exists() or candles.empty:
        return 0
    horizon = max(configured_int("ML_SHADOW_HORIZON_CANDLES", 4), 1)
    frame = market_candles_only(candles)
    changed = 0
    lock_path = PREDICTIONS_FILE.with_suffix(PREDICTIONS_FILE.suffix + ".lock")
    with file_lock(lock_path):
        with PREDICTIONS_FILE.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        lookup = {
            timestamp.isoformat(): position
            for position, timestamp in enumerate(frame.index)
        }
        for row in rows:
            if row.get("resolved_at") or not row.get("candle_time"):
                continue
            try:
                candle_time = pd.Timestamp(row["candle_time"])
                if candle_time.tzinfo is None:
                    candle_time = candle_time.tz_localize(IST)
                else:
                    candle_time = candle_time.tz_convert(IST)
                position = lookup.get(candle_time.isoformat())
                entry = float(row.get("underlying_entry_price") or 0)
            except (TypeError, ValueError):
                continue
            if position is None or entry <= 0:
                continue
            future = frame.iloc[position + 1 : position + 1 + horizon]
            if len(future) != horizon or len(set(future.index.date)) != 1:
                continue
            future_up = max(float(future["high"].max()) - entry, 0.0)
            future_down = max(entry - float(future["low"].min()), 0.0)
            row["future_up_points"] = round(future_up, 2)
            row["future_down_points"] = round(future_down, 2)
            call_target = float(row.get("call_target_points") or 0)
            call_stop = float(row.get("call_stop_points") or 0)
            put_target = float(row.get("put_target_points") or 0)
            put_stop = float(row.get("put_stop_points") or 0)
            row["call_target_hit"] = str(future_up >= call_target).lower()
            row["call_stop_hit"] = str(future_down >= call_stop).lower()
            row["put_target_hit"] = str(future_down >= put_target).lower()
            row["put_stop_hit"] = str(future_up >= put_stop).lower()
            outcome, realized = _selected_forecast_outcome(
                future,
                str(row.get("direction") or "").upper(),
                entry,
                float(row.get("expected_target_points") or 0),
                float(row.get("expected_stop_points") or 0),
            )
            row["selected_outcome"] = outcome
            row["selected_realized_points"] = round(realized, 2) if outcome else ""
            row["resolved_at"] = now_ist().isoformat()
            changed += 1
        if changed:
            temporary = PREDICTIONS_FILE.with_suffix(".csv.tmp")
            with temporary.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=PREDICTION_COLUMNS)
                writer.writeheader()
                writer.writerows(
                    [
                        {column: row.get(column, "") for column in PREDICTION_COLUMNS}
                        for row in rows
                    ]
                )
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(PREDICTIONS_FILE)
    return changed


def prediction_already_recorded(candle_time: str) -> bool:
    if not PREDICTIONS_FILE.exists():
        return False
    try:
        with PREDICTIONS_FILE.open(newline="") as handle:
            return any(row.get("candle_time") == candle_time for row in csv.DictReader(handle))
    except (OSError, ValueError):
        return False


def _round_tick(value: float) -> float:
    return round(round(float(value) / 0.05) * 0.05, 2)


def select_paper_option(direction: str) -> dict:
    option_type = "CE" if direction == "CALL" else "PE"
    expiries = get_expiries_from_upstox("NIFTY")
    expiry = choose_expiry("NIFTY", expiries)
    atm, _nearby, _chain = fetch_upstox_option_chain(
        "NIFTY", nearby=1, expiry_role="execution", expiry=expiry
    )
    row = atm.iloc[0].to_dict()
    instrument_key = row.get(f"{option_type}_instrument_key")
    ltp = float(row.get(f"{option_type}_ltp") or 0)
    ask = float(row.get(f"{option_type}_ask_price") or 0)
    bid = float(row.get(f"{option_type}_bid_price") or 0)
    delta = abs(float(row.get(f"{option_type}_delta") or 0.50))
    strike = float(row.get("strike") or 0)
    if not instrument_key or ltp <= 0:
        raise RuntimeError("ATM paper option has no valid instrument key/LTP")
    if ask > 0 and bid > 0 and (ask - bid) / ltp * 100 > configured_float("ML_SHADOW_MAX_OPTION_SPREAD_PERCENT", 5.0):
        raise RuntimeError("ATM paper option spread is too wide")
    entry = ask if ask > 0 else ltp
    # The option-contract API does not expose lot size in the chain response.
    # NIFTY lot size is made explicit and can be changed without model retraining.
    lot_size = max(configured_int("ML_SHADOW_NIFTY_LOT_SIZE", 65), 1)
    return {
        "instrument_key": str(instrument_key),
        "trading_symbol": f"NIFTY {int(strike)} {option_type} {expiry}",
        "option_type": option_type,
        "expiry": str(expiry),
        "strike": strike,
        "entry_price": _round_tick(entry),
        "delta": max(min(delta, 1.0), 0.05),
        "lot_size": lot_size,
    }


def open_paper_position(prediction: dict, decision: dict) -> dict:
    if read_state().get("status") == "POSITION_OPEN":
        raise RuntimeError("An ML shadow paper position is already open")
    option = select_paper_option(decision["direction"])
    target_points = float(decision["target_points"])
    stop_points = float(decision["stop_points"])
    delta = option["delta"]
    entry = option["entry_price"]
    option_target = _round_tick(entry + target_points * delta)
    option_stop = _round_tick(max(entry - stop_points * delta, 0.05))
    underlying_entry = float(prediction["underlying_entry_price"])
    if decision["direction"] == "CALL":
        underlying_target = underlying_entry + target_points
        underlying_stop = underlying_entry - stop_points
        market_direction = "BULLISH"
    else:
        underlying_target = underlying_entry - target_points
        underlying_stop = underlying_entry + stop_points
        market_direction = "BEARISH"
    now = now_ist()
    horizon_minutes = configured_int("ML_SHADOW_HORIZON_CANDLES", 4) * 15
    state = {
        "date": now.date().isoformat(),
        "symbol": "NIFTY",
        "underlying_symbol": "NIFTY",
        "state_slot": "ML_SHADOW_NIFTY",
        "instrument_class": "INDEX_OPTION",
        "strategy": "ML_SHADOW_V1_PAPER",
        "paper_trade": True,
        "execution_mode": "PAPER",
        "status": "POSITION_OPEN",
        "entry_transaction_type": "BUY",
        "position_side": "LONG_OPTION",
        "instrument_key": option["instrument_key"],
        "trading_symbol": option["trading_symbol"],
        "option_type": option["option_type"],
        "quantity": option["lot_size"],
        "lot_size": option["lot_size"],
        "direction": market_direction,
        "ml_direction": decision["direction"],
        "confidence": "CALIBRATED",
        "score": round(float(decision["probability"]) * 100.0, 2),
        "weighted_score": round(float(decision["probability"]) * 100.0, 2),
        "entry_score_version": VERSION,
        "entry_price": entry,
        "target_price": option_target,
        "planned_target_price": option_target,
        "stop_loss_price": option_stop,
        "original_stop_loss_price": option_stop,
        "target_points": round(target_points, 2),
        "stop_points": round(stop_points, 2),
        "option_delta_used": delta,
        "underlying_entry_price": round(underlying_entry, 2),
        "underlying_target_price": round(underlying_target, 2),
        "underlying_stop_price": round(underlying_stop, 2),
        "ml_probability": round(float(decision["probability"]), 6),
        "ml_reward_risk": round(float(decision["reward_risk"]), 4),
        "ml_expected_value_points": round(float(decision["expected_value_points"]), 4),
        "ml_model_hash": prediction["model_hash"],
        "ml_model_trained_through": prediction["model_trained_through"],
        "ml_candle_time": prediction["candle_time"],
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=horizon_minutes)).isoformat(),
        "highest_ltp": entry,
        "lowest_ltp": entry,
        "profit_booking_price": option_target,
        "profit_protection_stage": 0,
        "protective_stop_order_id": "PAPER_ONLY",
    }
    write_state(state)
    write_stream_instruments(
        [*read_stream_instruments(), NIFTY_KEY, option["instrument_key"]]
    )
    log(
        f"PAPER {decision['direction']} opened {option['trading_symbol']} entry={entry} "
        f"target={option_target} stop={option_stop} probability={decision['probability']:.3f} "
        f"expected={target_points:.1f}/{stop_points:.1f} NIFTY points"
    )
    return state


def _fresh_quote(instrument_key: str, maximum_age: float = 20.0) -> dict:
    quote = read_market_cache(instrument_key) or {}
    received = float(quote.get("received_at") or 0)
    age = time.time() - received if received else 999999.0
    if age > maximum_age:
        return {}
    return quote


def close_paper_position(state: dict, option_exit: float, reason: str) -> dict:
    state["highest_ltp"] = max(float(state.get("highest_ltp") or option_exit), option_exit)
    state["lowest_ltp"] = min(float(state.get("lowest_ltp") or option_exit), option_exit)
    row = record_closed_trade(state, option_exit, reason)
    write_state({})
    log(
        f"PAPER closed {state.get('trading_symbol')} entry={state.get('entry_price')} "
        f"exit={option_exit} pnl={row.get('gross_pnl')} reason={reason}"
    )
    return row


def monitor_once(force_squareoff: bool = False) -> bool:
    state = read_state()
    if state.get("status") != "POSITION_OPEN":
        return False
    maximum_age = configured_float("ML_SHADOW_QUOTE_MAX_AGE_SECONDS", 20.0)
    option_quote = _fresh_quote(state["instrument_key"], maximum_age)
    underlying_quote = _fresh_quote(NIFTY_KEY, maximum_age)
    option_ltp = float(option_quote.get("bid_price") or option_quote.get("ltp") or 0)
    underlying_ltp = float(underlying_quote.get("ltp") or 0)
    if option_ltp <= 0 or underlying_ltp <= 0:
        return False
    state["highest_ltp"] = max(float(state.get("highest_ltp") or option_ltp), option_ltp)
    state["lowest_ltp"] = min(float(state.get("lowest_ltp") or option_ltp), option_ltp)
    state["last_option_ltp"] = option_ltp
    state["last_underlying_ltp"] = underlying_ltp
    state["last_monitored_at"] = now_ist().isoformat()
    direction = state.get("ml_direction")
    if force_squareoff or now_ist().time() >= clock_time(15, 29):
        return bool(close_paper_position(state, _round_tick(option_ltp), "SQUAREOFF"))
    if direction == "CALL":
        target_hit = underlying_ltp >= float(state["underlying_target_price"])
        stop_hit = underlying_ltp <= float(state["underlying_stop_price"])
    else:
        target_hit = underlying_ltp <= float(state["underlying_target_price"])
        stop_hit = underlying_ltp >= float(state["underlying_stop_price"])
    option_target_hit = option_ltp >= float(state["target_price"])
    option_stop_hit = option_ltp <= float(state["stop_loss_price"])
    expired = now_ist() >= datetime.fromisoformat(state["expires_at"])
    if stop_hit or option_stop_hit:
        return bool(close_paper_position(state, _round_tick(option_ltp), "STOP_LOSS"))
    if target_hit or option_target_hit:
        return bool(close_paper_position(state, _round_tick(option_ltp), "TARGET"))
    if expired:
        return bool(close_paper_position(state, _round_tick(option_ltp), "HORIZON_EXIT"))
    write_state(state)
    return True


def _configured_minutes(name: str, default: str) -> int:
    value = os.getenv(name, default).strip()
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must use HH:MM") from error
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise RuntimeError(f"{name} must use HH:MM")
    return hour * 60 + minute


def scan() -> dict:
    load_env()
    if os.getenv("TRADING_ENGINE", "").strip().upper() != VERSION:
        raise RuntimeError(f"TRADING_ENGINE must be {VERSION}")
    if configured_bool("ENABLE_LIVE_TRADING", False):
        raise RuntimeError("ML_SHADOW_V1 refuses to run while ENABLE_LIVE_TRADING=true")
    if not configured_bool("ML_SHADOW_PAPER_ENABLED", True):
        raise RuntimeError("ML_SHADOW_PAPER_ENABLED must be true")
    artifact, metadata = load_artifact()
    features, candle_time, today_count = fetch_live_features()
    resolved = resolve_prediction_outcomes(features)
    prediction = score_latest(
        artifact,
        metadata,
        live_data=(features, candle_time, today_count),
    )
    if prediction_already_recorded(prediction["candle_time"]):
        log(f"candle {prediction['candle_time']} already scored")
        return {"action": "DUPLICATE", **prediction}
    decision = choose_action(prediction)
    current_minutes = now_ist().hour * 60 + now_ist().minute
    first_entry = _configured_minutes("ML_SHADOW_FIRST_ENTRY_TIME", "09:45")
    last_entry = _configured_minutes("ML_SHADOW_LAST_ENTRY_TIME", "14:16")
    if decision["action"] == "PAPER_ENTRY" and not first_entry <= current_minutes <= last_entry:
        decision = {
            **decision,
            "action": "NO_TRADE",
            "reason": "outside ML shadow entry window",
        }
    prediction.update(
        {
            "scan_time": now_ist().isoformat(),
            "direction": decision.get("direction", ""),
            "selected_probability": decision.get("probability", ""),
            "expected_target_points": decision.get("target_points", ""),
            "expected_stop_points": decision.get("stop_points", ""),
            "reward_risk": decision.get("reward_risk", ""),
            "action": decision["action"],
            "reason": decision["reason"],
        }
    )
    if decision["action"] == "PAPER_ENTRY" and read_state().get("status") == "POSITION_OPEN":
        prediction["action"] = "NO_TRADE"
        prediction["reason"] = "one non-overlapping ML paper position is already open"
    append_prediction(prediction)
    if prediction["action"] == "PAPER_ENTRY":
        open_paper_position(prediction, decision)
    log(
        f"candle={prediction['candle_time']} call={prediction['call_probability']:.3f} "
        f"put={prediction['put_probability']:.3f} none={prediction['none_probability']:.3f} "
        f"action={prediction['action']} reason={prediction['reason']} resolved={resolved}"
    )
    return prediction


def monitor_loop() -> None:
    load_env()
    interval = max(configured_float("ML_SHADOW_MONITOR_INTERVAL_SECONDS", 2.0), 1.0)
    log(f"paper monitor started at {interval:g}-second cadence")
    while now_ist().time() <= clock_time(15, 30):
        try:
            monitor_once()
        except Exception as error:
            log(f"monitor error: {error}")
        time.sleep(interval)
    log("paper monitor stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--train", action="store_true")
    action.add_argument("--scan", action="store_true")
    action.add_argument("--monitor", action="store_true")
    action.add_argument("--squareoff", action="store_true")
    args = parser.parse_args()
    load_env()
    if args.train:
        train()
    elif args.scan:
        scan()
    elif args.monitor:
        monitor_loop()
    elif args.squareoff:
        monitor_once(force_squareoff=True)


if __name__ == "__main__":
    main()
