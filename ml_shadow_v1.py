"""ML Shadow: leakage-safe first-four-hour NIFTY model.

One sample is created per trading day from the 09:15-13:15 NIFTY candle.
The live forecast is made just after the open using prior-day features and
today's opening price only. CALL and PUT are independent questions, so either,
neither, or both can qualify. Paper execution is the default. Live execution
requires two explicit switches and uses an Upstox multi-leg GTT with target,
stop loss, and broker-managed trailing stop loss.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import time
from calendar import monthrange
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from backtest_data import UpstoxBacktestData
from market_technicals import completed_candles, fetch_v3_intraday_minutes
from safe_storage import atomic_write_json, file_lock, locked_append_csv
from strategy_core import (
    choose_expiry,
    fetch_upstox_option_chain,
    get_expiries_from_upstox,
    now_ist,
)
from trade_journal import record_closed_trade
from upstox_streams import read_market_cache, read_stream_instruments, write_stream_instruments


IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "ml_shadow_4h_v2"
MODEL_FILE = DATA_DIR / "model.joblib"
METADATA_FILE = DATA_DIR / "metadata.json"
PREDICTIONS_FILE = DATA_DIR / "predictions.csv"
ENV_FILE = BASE_DIR / ".env"
LEGACY_STATE_FILE = BASE_DIR / "trade_state_ML_SHADOW_NIFTY.json"
STATE_FILES = {
    "CALL": BASE_DIR / "trade_state_ML_SHADOW_CALL.json",
    "PUT": BASE_DIR / "trade_state_ML_SHADOW_PUT.json",
}
LOG_PREFIX = "ML_SHADOW_4H_V2"
ENGINE = "ML_SHADOW_V1"
MODEL_VERSION = "ML_SHADOW_4H_PERCENT_V2"
NIFTY_KEY = "NSE_INDEX|Nifty 50"
GTT_PLACE_URL = "https://api.upstox.com/v3/order/gtt/place"
GTT_DETAILS_URL = "https://api.upstox.com/v3/order/gtt"
GTT_CANCEL_URL = "https://api.upstox.com/v3/order/gtt/cancel"
ORDER_PLACE_URL = "https://api-hft.upstox.com/v3/order/place"
POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"

FEATURE_COLUMNS = [
    "previous_return_percent",
    "previous_range_percent",
    "previous_body_percent",
    "previous_up_percent",
    "previous_down_percent",
    "previous_close_location",
    "opening_gap_percent",
    "previous_volume_ratio_20",
    "trend_5_percent",
    "trend_20_percent",
    "return_mean_5",
    "return_std_5",
    "range_mean_5",
    "up_mean_5",
    "down_mean_5",
    "return_mean_10",
    "return_std_10",
    "range_mean_10",
    "up_mean_10",
    "down_mean_10",
    "return_mean_20",
    "return_std_20",
    "range_mean_20",
    "up_mean_20",
    "down_mean_20",
    "return_mean_60",
    "return_std_60",
    "range_mean_60",
    "up_mean_60",
    "down_mean_60",
    "day_of_week",
    "month_sin",
    "month_cos",
]

PREDICTION_COLUMNS = [
    "scan_time",
    "candle_time",
    "model_trained_through",
    "model_hash",
    "underlying_open",
    "underlying_entry_price",
    "call_probability",
    "call_target_percent",
    "call_stop_percent",
    "call_reward_risk",
    "call_action",
    "call_reason",
    "put_probability",
    "put_target_percent",
    "put_stop_percent",
    "put_reward_risk",
    "put_action",
    "put_reason",
    "overall_action",
    "execution_mode",
    "future_up_percent",
    "future_down_percent",
    "call_outcome",
    "call_realized_percent",
    "put_outcome",
    "put_realized_percent",
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
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


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
    index = index.tz_localize(IST) if index.tz is None else index.tz_convert(IST)
    result.index = index
    return result.sort_index()[~result.index.duplicated(keep="last")]


def first_candle_per_day(candles: pd.DataFrame) -> pd.DataFrame:
    """Return exactly the first 4-hour candle from each session."""
    frame = _as_ist(candles)
    if frame.empty:
        return frame
    times = frame.index.time
    frame = frame[(times >= clock_time(9, 15)) & (times < clock_time(13, 15))]
    return frame.groupby(frame.index.date, sort=True).head(1).copy()


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.replace(0, np.nan)


def build_feature_frame(candles: pd.DataFrame) -> pd.DataFrame:
    """Build one daily feature row without using that day's high/low/close."""
    frame = first_candle_per_day(candles)
    if frame.empty:
        return frame
    required = {"open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError("Missing candle fields: " + ", ".join(sorted(missing)))
    for column in ("open", "high", "low", "close", "volume"):
        if column not in frame:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    open_price = frame["open"]
    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    volume = frame["volume"].fillna(0.0)
    candle_return = _safe_divide(close - open_price, open_price) * 100
    candle_range = _safe_divide(high - low, open_price) * 100
    candle_body = _safe_divide(close - open_price, open_price) * 100
    up_percent = _safe_divide(high - open_price, open_price) * 100
    down_percent = _safe_divide(open_price - low, open_price) * 100
    close_location = _safe_divide(close - low, high - low)

    result = frame.copy()
    result["previous_return_percent"] = candle_return.shift(1)
    result["previous_range_percent"] = candle_range.shift(1)
    result["previous_body_percent"] = candle_body.shift(1)
    result["previous_up_percent"] = up_percent.shift(1)
    result["previous_down_percent"] = down_percent.shift(1)
    result["previous_close_location"] = close_location.shift(1)
    result["opening_gap_percent"] = _safe_divide(open_price - close.shift(1), close.shift(1)) * 100
    volume_average = volume.rolling(20, min_periods=5).mean().shift(1)
    result["previous_volume_ratio_20"] = _safe_divide(volume.shift(1), volume_average).fillna(1.0)
    result["trend_5_percent"] = (_safe_divide(close.shift(1), close.shift(6)) - 1) * 100
    result["trend_20_percent"] = (_safe_divide(close.shift(1), close.shift(21)) - 1) * 100
    for window in (5, 10, 20, 60):
        result[f"return_mean_{window}"] = candle_return.rolling(window).mean().shift(1)
        result[f"return_std_{window}"] = candle_return.rolling(window).std().shift(1)
        result[f"range_mean_{window}"] = candle_range.rolling(window).mean().shift(1)
        result[f"up_mean_{window}"] = up_percent.rolling(window).mean().shift(1)
        result[f"down_mean_{window}"] = down_percent.rolling(window).mean().shift(1)
    result["day_of_week"] = frame.index.dayofweek.astype(float)
    month_phase = (frame.index.month - 1) / 12 * 2 * math.pi
    result["month_sin"] = np.sin(month_phase)
    result["month_cos"] = np.cos(month_phase)
    result.replace([np.inf, -np.inf], np.nan, inplace=True)
    return result


def add_forward_labels(
    features: pd.DataFrame,
    horizon_candles: int = 1,
    minimum_move: float | None = None,
) -> pd.DataFrame:
    """Attach same-first-candle percentage excursions as independent labels."""
    del horizon_candles
    threshold = (
        configured_float("ML_SHADOW_EVENT_MOVE_PERCENT", 0.10)
        if minimum_move is None
        else float(minimum_move)
    )
    labeled = features.copy()
    labeled["future_up_percent"] = (
        _safe_divide(labeled["high"] - labeled["open"], labeled["open"]) * 100
    ).clip(lower=0)
    labeled["future_down_percent"] = (
        _safe_divide(labeled["open"] - labeled["low"], labeled["open"]) * 100
    ).clip(lower=0)
    labeled["call_label"] = (labeled["future_up_percent"] >= threshold).astype(int)
    labeled["put_label"] = (labeled["future_down_percent"] >= threshold).astype(int)
    return labeled


def _sklearn_imports():
    try:
        import joblib
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.ensemble import GradientBoostingRegressor, HistGradientBoostingClassifier
        from sklearn.impute import SimpleImputer
        from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, precision_score
        from sklearn.model_selection import TimeSeriesSplit
        from sklearn.pipeline import Pipeline
    except ImportError as error:
        raise RuntimeError("Install requirements.txt before training ML Shadow") from error
    return locals()


def _classifier(sk):
    base = sk["Pipeline"]([
        ("imputer", sk["SimpleImputer"](strategy="median")),
        ("model", sk["HistGradientBoostingClassifier"](
            learning_rate=0.04,
            max_iter=180,
            max_leaf_nodes=15,
            l2_regularization=1.0,
            random_state=41,
        )),
    ])
    return sk["CalibratedClassifierCV"](
        base,
        method="sigmoid",
        cv=sk["TimeSeriesSplit"](n_splits=5, gap=1),
    )


def _regressor(sk, quantile: float):
    return sk["Pipeline"]([
        ("imputer", sk["SimpleImputer"](strategy="median")),
        ("model", sk["GradientBoostingRegressor"](
            loss="quantile",
            alpha=quantile,
            n_estimators=140,
            learning_rate=0.04,
            max_depth=2,
            min_samples_leaf=12,
            random_state=41,
        )),
    ])


def _positive_probability(model, frame: pd.DataFrame) -> np.ndarray:
    classes = list(model.classes_)
    if 1 not in classes:
        return np.zeros(len(frame), dtype=float)
    return model.predict_proba(frame)[:, classes.index(1)]


def fetch_training_candles() -> pd.DataFrame:
    calendar_days = max(configured_int("ML_SHADOW_HISTORY_CALENDAR_DAYS", 800), 730)
    trading_days = max(configured_int("ML_SHADOW_TRAINING_DAYS", 504), 300)
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
    candles = first_candle_per_day(pd.concat(frames).sort_index() if frames else pd.DataFrame())
    available = sorted(set(candles.index.date))
    if len(available) > trading_days:
        candles = candles[candles.index.date >= available[-trading_days]]
    return candles


def _binary_metrics(sk, actual, probability, threshold: float) -> dict:
    predicted = (probability > threshold).astype(int)
    qualified = probability > threshold
    return {
        "accuracy": round(float(sk["accuracy_score"](actual, predicted)), 4),
        "baseline_accuracy": round(float(max(actual.mean(), 1 - actual.mean())), 4),
        "log_loss": round(float(sk["log_loss"](actual, np.c_[1 - probability, probability], labels=[0, 1])), 4),
        "brier_score": round(float(sk["brier_score_loss"](actual, probability)), 4),
        "qualified_count": int(qualified.sum()),
        "qualified_coverage": round(float(qualified.mean()), 4),
        "qualified_precision": round(float(sk["precision_score"](actual, predicted, zero_division=0)), 4),
    }


def train() -> dict:
    load_env()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    minimum_rows = max(configured_int("ML_SHADOW_MIN_TRAINING_ROWS", 300), 240)
    event_move = max(configured_float("ML_SHADOW_EVENT_MOVE_PERCENT", 0.10), 0.01)
    candles = fetch_training_candles()
    features = build_feature_frame(candles)
    labeled = add_forward_labels(features, minimum_move=event_move)
    usable = labeled.dropna(subset=FEATURE_COLUMNS + ["call_label", "put_label"])
    if len(usable) < minimum_rows:
        metadata = {
            "version": MODEL_VERSION,
            "status": "INSUFFICIENT_DATA",
            "training_rows": int(len(usable)),
            "minimum_rows": minimum_rows,
            "generated_at": now_ist().isoformat(),
        }
        atomic_write_json(METADATA_FILE, metadata, sort_keys=True)
        raise RuntimeError(f"Need {minimum_rows} first-candle rows; found {len(usable)}")
    for label in ("call_label", "put_label"):
        if usable[label].nunique() < 2:
            raise RuntimeError(f"{label} requires both classes; adjust ML_SHADOW_EVENT_MOVE_PERCENT")

    sk = _sklearn_imports()
    validation_rows = min(max(int(len(usable) * 0.20), 60), len(usable) - 180)
    training = usable.iloc[: -validation_rows]
    validation = usable.iloc[-validation_rows:]
    X_train = training[FEATURE_COLUMNS]
    X_validation = validation[FEATURE_COLUMNS]
    threshold = configured_float("ML_SHADOW_MIN_PROBABILITY", 0.50)

    call_validation_model = _classifier(sk).fit(X_train, training["call_label"])
    put_validation_model = _classifier(sk).fit(X_train, training["put_label"])
    call_probability = _positive_probability(call_validation_model, X_validation)
    put_probability = _positive_probability(put_validation_model, X_validation)
    call_metrics = _binary_metrics(sk, validation["call_label"], call_probability, threshold)
    put_metrics = _binary_metrics(sk, validation["put_label"], put_probability, threshold)

    call_target_validation = _regressor(sk, 0.50).fit(X_train, training["future_up_percent"])
    put_target_validation = _regressor(sk, 0.50).fit(X_train, training["future_down_percent"])
    call_stop_validation = _regressor(sk, 0.75).fit(X_train, training["future_down_percent"])
    put_stop_validation = _regressor(sk, 0.75).fit(X_train, training["future_up_percent"])
    call_target_pred = call_target_validation.predict(X_validation)
    put_target_pred = put_target_validation.predict(X_validation)
    call_stop_pred = call_stop_validation.predict(X_validation)
    put_stop_pred = put_stop_validation.predict(X_validation)
    rr_min = configured_float("ML_SHADOW_MIN_REWARD_RISK", 0.75)
    qualified = (
        ((call_probability > threshold) & (call_target_pred / np.maximum(call_stop_pred, 0.001) >= rr_min)).sum()
        + ((put_probability > threshold) & (put_target_pred / np.maximum(put_stop_pred, 0.001) >= rr_min)).sum()
    )
    metrics = {
        "accuracy": round((call_metrics["accuracy"] + put_metrics["accuracy"]) / 2, 4),
        "majority_baseline_accuracy": round((call_metrics["baseline_accuracy"] + put_metrics["baseline_accuracy"]) / 2, 4),
        "log_loss": round((call_metrics["log_loss"] + put_metrics["log_loss"]) / 2, 4),
        "qualified_direction_count": int(qualified),
        "qualified_direction_coverage": round(float(qualified / (validation_rows * 2)), 4),
        "rows": validation_rows,
        "start": validation.index.min().isoformat(),
        "end": validation.index.max().isoformat(),
        "call": call_metrics,
        "put": put_metrics,
        "call_target_mae_percent": round(float(np.mean(np.abs(call_target_pred - validation["future_up_percent"]))), 4),
        "put_target_mae_percent": round(float(np.mean(np.abs(put_target_pred - validation["future_down_percent"]))), 4),
        "call_stop_coverage": round(float(np.mean(validation["future_down_percent"] <= call_stop_pred)), 4),
        "put_stop_coverage": round(float(np.mean(validation["future_up_percent"] <= put_stop_pred)), 4),
    }

    X = usable[FEATURE_COLUMNS]
    artifact = {
        "version": MODEL_VERSION,
        "feature_columns": FEATURE_COLUMNS,
        "call_classifier": _classifier(sk).fit(X, usable["call_label"]),
        "put_classifier": _classifier(sk).fit(X, usable["put_label"]),
        "call_target": _regressor(sk, 0.50).fit(X, usable["future_up_percent"]),
        "put_target": _regressor(sk, 0.50).fit(X, usable["future_down_percent"]),
        "call_stop": _regressor(sk, 0.75).fit(X, usable["future_down_percent"]),
        "put_stop": _regressor(sk, 0.75).fit(X, usable["future_up_percent"]),
        "event_move_percent": event_move,
        "trained_through": max(candles.index.date).isoformat(),
        "history_tail": first_candle_per_day(candles).tail(90),
    }
    sk["joblib"].dump(artifact, MODEL_FILE)
    model_hash = hashlib.sha256(MODEL_FILE.read_bytes()).hexdigest()[:16]
    metadata = {
        "version": MODEL_VERSION,
        "status": "READY_SHADOW",
        "timeframe": "FIRST_4H",
        "generated_at": now_ist().isoformat(),
        "trained_through": artifact["trained_through"],
        "training_rows": int(len(usable)),
        "training_days": int(len(set(usable.index.date))),
        "history_calendar_days": configured_int("ML_SHADOW_HISTORY_CALENDAR_DAYS", 800),
        "event_move_percent": event_move,
        "validation": metrics,
        "model_hash": model_hash,
        "feature_columns": FEATURE_COLUMNS,
    }
    atomic_write_json(METADATA_FILE, metadata, sort_keys=True)
    log(
        f"trained first-4H percent model through {artifact['trained_through']}; "
        f"rows={len(usable)} validation_accuracy={metrics['accuracy']}"
    )
    return metadata


def load_artifact():
    if not MODEL_FILE.exists() or not METADATA_FILE.exists():
        raise RuntimeError("ML model is missing; run --train")
    sk = _sklearn_imports()
    artifact = sk["joblib"].load(MODEL_FILE)
    metadata = json.loads(METADATA_FILE.read_text())
    if artifact.get("version") != MODEL_VERSION or metadata.get("status") != "READY_SHADOW":
        raise RuntimeError("First-4H ML model is not READY_SHADOW")
    if artifact.get("trained_through") >= now_ist().date().isoformat():
        raise RuntimeError("ML model includes the current live day")
    trained_date = date.fromisoformat(str(artifact["trained_through"]))
    maximum_age = max(configured_int("ML_SHADOW_MAX_MODEL_AGE_DAYS", 4), 1)
    if (now_ist().date() - trained_date).days > maximum_age:
        raise RuntimeError(f"ML model is stale: trained through {trained_date}")
    return artifact, metadata


def fetch_today_open() -> tuple[pd.Timestamp, float, float]:
    candles = _as_ist(fetch_v3_intraday_minutes(NIFTY_KEY, minutes=1))
    candles = completed_candles(
        candles,
        1,
        current_time=now_ist(),
        grace_seconds=max(configured_float("ML_SHADOW_CANDLE_GRACE_SECONDS", 8), 5),
    )
    today = candles[candles.index.date == now_ist().date()]
    if today.empty:
        raise RuntimeError("The first completed NIFTY minute is not available")
    opening = float(today.iloc[0]["open"])
    current_quote = read_market_cache(NIFTY_KEY) or {}
    current_price = float(current_quote.get("ltp") or today.iloc[-1]["close"])
    candle_time = pd.Timestamp.combine(now_ist().date(), clock_time(9, 15)).tz_localize(IST)
    return candle_time, opening, current_price


def score_latest(artifact, metadata, live_data=None) -> dict:
    candle_time, opening, current_price = live_data or fetch_today_open()
    history = first_candle_per_day(artifact["history_tail"])
    synthetic = pd.DataFrame(
        [{"open": opening, "high": opening, "low": opening, "close": opening, "volume": 0.0}],
        index=pd.DatetimeIndex([candle_time]),
    )
    features = build_feature_frame(pd.concat([history, synthetic]).sort_index())
    row = features.loc[[candle_time], artifact["feature_columns"]]
    missing_fraction = float(row.isna().mean(axis=1).iloc[0])
    if missing_fraction > 0.10:
        raise RuntimeError(f"Opening feature row is {missing_fraction:.0%} incomplete")
    call_probability = float(_positive_probability(artifact["call_classifier"], row)[0])
    put_probability = float(_positive_probability(artifact["put_classifier"], row)[0])
    call_target = max(float(artifact["call_target"].predict(row)[0]), 0.001)
    put_target = max(float(artifact["put_target"].predict(row)[0]), 0.001)
    call_stop = max(float(artifact["call_stop"].predict(row)[0]), 0.001)
    put_stop = max(float(artifact["put_stop"].predict(row)[0]), 0.001)
    return {
        "candle_time": candle_time.isoformat(),
        "model_trained_through": artifact["trained_through"],
        "model_hash": metadata["model_hash"],
        "underlying_open": opening,
        "underlying_entry_price": current_price,
        "call_probability": call_probability,
        "call_target_percent": call_target,
        "call_stop_percent": call_stop,
        "call_reward_risk": call_target / call_stop,
        "put_probability": put_probability,
        "put_target_percent": put_target,
        "put_stop_percent": put_stop,
        "put_reward_risk": put_target / put_stop,
    }


def choose_actions(prediction: dict) -> dict[str, dict]:
    threshold = configured_float("ML_SHADOW_MIN_PROBABILITY", 0.50)
    minimum_rr = configured_float("ML_SHADOW_MIN_REWARD_RISK", 0.75)
    decisions = {}
    for direction, prefix in (("CALL", "call"), ("PUT", "put")):
        probability = float(prediction[f"{prefix}_probability"])
        target = float(prediction[f"{prefix}_target_percent"])
        stop = float(prediction[f"{prefix}_stop_percent"])
        reward_risk = float(prediction[f"{prefix}_reward_risk"])
        qualified = probability > threshold and reward_risk >= minimum_rr
        decisions[direction] = {
            "direction": direction,
            "qualified": qualified,
            "probability": probability,
            "target_percent": target,
            "stop_percent": stop,
            "reward_risk": reward_risk,
            "reason": (
                "independent probability and reward/risk qualified"
                if qualified
                else f"requires probability > {threshold:.0%} and reward/risk >= {minimum_rr:.2f}"
            ),
        }
    return decisions


def choose_action(prediction: dict) -> dict:
    """Compatibility helper; returns the strongest independently qualified side."""
    qualified = [item for item in choose_actions(prediction).values() if item["qualified"]]
    if not qualified:
        return {"action": "NO_TRADE", "reason": "neither direction qualified"}
    selected = max(qualified, key=lambda item: (item["probability"], item["reward_risk"]))
    return {**selected, "action": "ENTRY"}


def read_state(direction: str) -> dict:
    try:
        value = json.loads(STATE_FILES[direction].read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_state(direction: str, state: dict) -> None:
    atomic_write_json(STATE_FILES[direction], state, sort_keys=True)


def migrate_legacy_state() -> None:
    try:
        state = json.loads(LEGACY_STATE_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return
    if state.get("status") != "POSITION_OPEN" or not state.get("instrument_key"):
        return
    direction = str(state.get("ml_direction") or "CALL").upper()
    if direction not in STATE_FILES or read_state(direction):
        return
    state["state_slot"] = f"ML_SHADOW_{direction}"
    write_state(direction, state)
    atomic_write_json(LEGACY_STATE_FILE, {}, sort_keys=True)
    log(f"migrated legacy paper state into {direction} lane")


def append_prediction(prediction: dict) -> None:
    locked_append_csv(
        PREDICTIONS_FILE,
        PREDICTION_COLUMNS,
        {column: prediction.get(column, "") for column in PREDICTION_COLUMNS},
    )


def prediction_already_recorded(candle_time: str) -> bool:
    if not PREDICTIONS_FILE.exists():
        return False
    try:
        with PREDICTIONS_FILE.open(newline="") as handle:
            return any(row.get("candle_time") == candle_time for row in csv.DictReader(handle))
    except (OSError, ValueError):
        return False


def _direction_outcome(
    direction: str,
    up_percent: float,
    down_percent: float,
    close_percent: float,
    target_percent: float,
    stop_percent: float,
) -> tuple[str, float]:
    favorable = up_percent if direction == "CALL" else down_percent
    adverse = down_percent if direction == "CALL" else up_percent
    if adverse >= stop_percent:
        return "STOP", -stop_percent
    if favorable >= target_percent:
        return "TARGET", target_percent
    return "CLOSE", close_percent if direction == "CALL" else -close_percent


def resolve_prediction_outcomes(candles: pd.DataFrame) -> int:
    if not PREDICTIONS_FILE.exists() or candles.empty:
        return 0
    first = first_candle_per_day(candles)
    by_date = {timestamp.date(): row for timestamp, row in first.iterrows()}
    changed = 0
    lock_path = PREDICTIONS_FILE.with_suffix(PREDICTIONS_FILE.suffix + ".lock")
    with file_lock(lock_path):
        with PREDICTIONS_FILE.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            if row.get("resolved_at") or not row.get("candle_time"):
                continue
            try:
                candle_date = pd.Timestamp(row["candle_time"]).date()
                candle = by_date[candle_date]
                opening = float(candle["open"])
            except (KeyError, TypeError, ValueError):
                continue
            up = max((float(candle["high"]) - opening) / opening * 100, 0)
            down = max((opening - float(candle["low"])) / opening * 100, 0)
            close_change = (float(candle["close"]) - opening) / opening * 100
            row["future_up_percent"] = round(up, 4)
            row["future_down_percent"] = round(down, 4)
            for direction, prefix in (("CALL", "call"), ("PUT", "put")):
                outcome, realized = _direction_outcome(
                    direction,
                    up,
                    down,
                    close_change,
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
                writer.writerows([{column: row.get(column, "") for column in PREDICTION_COLUMNS} for row in rows])
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(PREDICTIONS_FILE)
    return changed


def fetch_resolution_candles() -> pd.DataFrame:
    end = now_ist().date()
    start = end - timedelta(days=10)
    source = UpstoxBacktestData(DATA_DIR / "history_cache", progress=log, pause_seconds=0.1)
    historical = source.candles(NIFTY_KEY, "4hour", start, end - timedelta(days=1), expired=False)
    intraday = source.candles(NIFTY_KEY, "4hour", end, end, expired=False)
    valid = [frame for frame in (historical, intraday) if not frame.empty]
    return pd.concat(valid).sort_index() if valid else pd.DataFrame()


def _round_tick(value: float) -> float:
    return round(round(float(value) / 0.05) * 0.05, 2)


def select_option(direction: str) -> dict:
    option_type = "CE" if direction == "CALL" else "PE"
    expiry = choose_expiry("NIFTY", get_expiries_from_upstox("NIFTY"))
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
        raise RuntimeError(f"ATM {direction} option has no valid instrument/LTP")
    if ask > 0 and bid > 0 and (ask - bid) / ltp * 100 > configured_float("ML_SHADOW_MAX_OPTION_SPREAD_PERCENT", 5):
        raise RuntimeError(f"ATM {direction} option spread is too wide")
    return {
        "instrument_key": str(instrument_key),
        "trading_symbol": f"NIFTY {int(strike)} {option_type} {expiry}",
        "option_type": option_type,
        "entry_price": _round_tick(ask if ask > 0 else ltp),
        "delta": max(min(delta, 1.0), 0.05),
        "lot_size": max(configured_int("ML_SHADOW_NIFTY_LOT_SIZE", 65), 1),
    }


def option_levels(prediction: dict, decision: dict, option: dict) -> dict:
    opening = float(prediction["underlying_open"])
    current_underlying = float(prediction["underlying_entry_price"])
    entry = float(option["entry_price"])
    delta = float(option["delta"])
    if decision["direction"] == "CALL":
        underlying_target = opening * (1 + decision["target_percent"] / 100)
        underlying_stop = opening * (1 - decision["stop_percent"] / 100)
        target_points = underlying_target - current_underlying
        stop_points = current_underlying - underlying_stop
    else:
        underlying_target = opening * (1 - decision["target_percent"] / 100)
        underlying_stop = opening * (1 + decision["stop_percent"] / 100)
        target_points = current_underlying - underlying_target
        stop_points = underlying_stop - current_underlying
    if target_points <= 0:
        raise RuntimeError(f"{decision['direction']} predicted underlying target was already reached")
    if stop_points <= 0:
        raise RuntimeError(f"{decision['direction']} predicted underlying stop was already crossed")
    execution_reward_risk = target_points / stop_points
    minimum_rr = configured_float("ML_SHADOW_MIN_REWARD_RISK", 0.75)
    if execution_reward_risk < minimum_rr:
        raise RuntimeError(
            f"{decision['direction']} remaining reward/risk {execution_reward_risk:.2f} "
            f"is below {minimum_rr:.2f}"
        )
    target_distance = max(target_points * delta, 0.05)
    stop_distance = min(max(stop_points * delta, 0.05), max(entry - 0.05, 0.05))
    target = _round_tick(entry + target_distance)
    stop = _round_tick(max(entry - stop_distance, 0.05))
    stop_distance = max(entry - stop, 0.05)
    fraction = max(configured_float("ML_SHADOW_TRAILING_GAP_FRACTION", 0.25), 0.10)
    trailing_gap = _round_tick(max(stop_distance * fraction, 0.05))
    return {
        "target_price": target,
        "stop_loss_price": stop,
        "trailing_gap": trailing_gap,
        "option_target_percent": round((target - entry) / entry * 100, 3),
        "option_stop_percent": round((entry - stop) / entry * 100, 3),
        "underlying_target_price": round(underlying_target, 2),
        "underlying_stop_price": round(underlying_stop, 2),
        "execution_reward_risk": round(execution_reward_risk, 4),
    }


def _base_state(prediction: dict, decision: dict, option: dict, levels: dict, mode: str) -> dict:
    direction = decision["direction"]
    expires = datetime.combine(now_ist().date(), clock_time(13, 15), tzinfo=IST)
    return {
        "date": now_ist().date().isoformat(),
        "symbol": "NIFTY",
        "underlying_symbol": "NIFTY",
        "state_slot": f"ML_SHADOW_{direction}",
        "instrument_class": "INDEX_OPTION",
        "strategy": f"{MODEL_VERSION}_{mode}",
        "paper_trade": mode == "PAPER",
        "execution_mode": mode,
        "status": "POSITION_OPEN" if mode == "PAPER" else "GTT_SUBMITTING",
        "entry_transaction_type": "BUY",
        "position_side": "LONG_OPTION",
        "instrument_key": option["instrument_key"],
        "trading_symbol": option["trading_symbol"],
        "option_type": option["option_type"],
        "quantity": option["lot_size"],
        "lot_size": option["lot_size"],
        "direction": "BULLISH" if direction == "CALL" else "BEARISH",
        "ml_direction": direction,
        "score": round(decision["probability"] * 100, 2),
        "weighted_score": round(decision["probability"] * 100, 2),
        "entry_score_version": MODEL_VERSION,
        "entry_price": option["entry_price"],
        "target_price": levels["target_price"],
        "planned_target_price": levels["target_price"],
        "stop_loss_price": levels["stop_loss_price"],
        "original_stop_loss_price": levels["stop_loss_price"],
        "trailing_gap": levels["trailing_gap"],
        "trailing_stop_active": True,
        "trailing_stop_reason": "Upstox-style fixed trailing gap",
        "underlying_open": prediction["underlying_open"],
        "underlying_entry_price": prediction["underlying_entry_price"],
        "underlying_target_price": levels["underlying_target_price"],
        "underlying_stop_price": levels["underlying_stop_price"],
        "target_percent": round(decision["target_percent"], 4),
        "stop_percent": round(decision["stop_percent"], 4),
        "option_target_percent": levels["option_target_percent"],
        "option_stop_percent": levels["option_stop_percent"],
        "ml_probability": round(decision["probability"], 6),
        "ml_reward_risk": round(decision["reward_risk"], 4),
        "execution_reward_risk": levels["execution_reward_risk"],
        "ml_model_hash": prediction["model_hash"],
        "ml_model_trained_through": prediction["model_trained_through"],
        "ml_candle_time": prediction["candle_time"],
        "created_at": now_ist().isoformat(),
        "expires_at": expires.isoformat(),
        "highest_ltp": option["entry_price"],
        "lowest_ltp": option["entry_price"],
        "profit_booking_price": levels["target_price"],
        "profit_protection_stage": 1,
    }


def open_paper_position(prediction: dict, decision: dict) -> dict:
    direction = decision["direction"]
    if read_state(direction).get("instrument_key"):
        raise RuntimeError(f"{direction} lane already has a position")
    option = select_option(direction)
    levels = option_levels(prediction, decision, option)
    state = _base_state(prediction, decision, option, levels, "PAPER")
    state["protective_stop_order_id"] = "PAPER_GTT_TRAILING"
    write_state(direction, state)
    write_stream_instruments([*read_stream_instruments(), NIFTY_KEY, option["instrument_key"]])
    log(
        f"PAPER {direction} opened {option['trading_symbol']} entry={option['entry_price']} "
        f"target={levels['target_price']} stop={levels['stop_loss_price']} trail={levels['trailing_gap']}"
    )
    return state


def _auth_headers() -> dict:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN is required for live GTT execution")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    algo_name = os.getenv("UPSTOX_ALGO_NAME", "").strip()
    if algo_name:
        headers["X-Algo-Name"] = algo_name
    return headers


def _response_json(response: requests.Response, action: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:500]}
    if response.status_code >= 300 or payload.get("status") == "error":
        raise RuntimeError(f"Upstox {action} failed ({response.status_code}): {payload}")
    return payload


def place_live_gtt(prediction: dict, decision: dict) -> dict:
    direction = decision["direction"]
    if read_state(direction).get("instrument_key"):
        raise RuntimeError(f"{direction} lane already has state")
    option = select_option(direction)
    levels = option_levels(prediction, decision, option)
    state = _base_state(prediction, decision, option, levels, "LIVE_GTT")
    write_state(direction, state)  # durable intent prevents a retry after an ambiguous timeout
    payload = {
        "type": "MULTIPLE",
        "quantity": option["lot_size"],
        "product": "I",
        "instrument_token": option["instrument_key"],
        "transaction_type": "BUY",
        "rules": [
            {"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": option["entry_price"], "market_protection": -1},
            {"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": levels["target_price"], "market_protection": -1},
            {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": levels["stop_loss_price"], "trailing_gap": levels["trailing_gap"], "market_protection": -1},
        ],
    }
    try:
        response = requests.post(GTT_PLACE_URL, headers=_auth_headers(), json=payload, timeout=20)
        result = _response_json(response, "GTT placement")
        identifiers = (result.get("data") or {}).get("gtt_order_ids") or []
        if not identifiers:
            raise RuntimeError(f"Upstox GTT response has no ID: {result}")
        state["gtt_order_id"] = identifiers[0]
        state["status"] = "GTT_ACTIVE"
        state["gtt_payload"] = payload
        write_state(direction, state)
        log(f"LIVE {direction} GTT active id={identifiers[0]} target/stop/trailing managed by Upstox")
        return state
    except Exception as error:
        state["status"] = "GTT_SUBMISSION_UNKNOWN"
        state["submission_error"] = str(error)
        write_state(direction, state)
        raise


def _fresh_quote(instrument_key: str, maximum_age: float = 20) -> dict:
    quote = read_market_cache(instrument_key) or {}
    received = float(quote.get("received_at") or 0)
    if not received or time.time() - received > maximum_age:
        return {}
    return quote


def close_paper_position(direction: str, state: dict, option_exit: float, reason: str) -> dict:
    state["highest_ltp"] = max(float(state.get("highest_ltp") or option_exit), option_exit)
    state["lowest_ltp"] = min(float(state.get("lowest_ltp") or option_exit), option_exit)
    row = record_closed_trade(state, option_exit, reason)
    write_state(direction, {})
    log(f"PAPER {direction} closed exit={option_exit} pnl={row.get('gross_pnl')} reason={reason}")
    return row


def monitor_paper_direction(direction: str, state: dict, force_squareoff: bool = False) -> bool:
    quote = _fresh_quote(
        state["instrument_key"],
        configured_float("ML_SHADOW_QUOTE_MAX_AGE_SECONDS", 20),
    )
    option_ltp = float(quote.get("bid_price") or quote.get("ltp") or 0)
    if option_ltp <= 0:
        return False
    highest = max(float(state.get("highest_ltp") or option_ltp), option_ltp)
    state["highest_ltp"] = highest
    state["lowest_ltp"] = min(float(state.get("lowest_ltp") or option_ltp), option_ltp)
    state["last_option_ltp"] = option_ltp
    state["last_monitored_at"] = now_ist().isoformat()
    original_stop = float(state.get("original_stop_loss_price") or state["stop_loss_price"])
    entry = float(state["entry_price"])
    trailing_gap = max(float(state.get("trailing_gap") or 0.05), 0.05)
    favorable_steps = max(math.floor((highest - entry) / trailing_gap), 0)
    trailed_stop = _round_tick(original_stop + favorable_steps * trailing_gap)
    state["stop_loss_price"] = trailed_stop
    if force_squareoff or now_ist() >= datetime.fromisoformat(state["expires_at"]):
        close_paper_position(direction, state, _round_tick(option_ltp), "FIRST_4H_CLOSE")
        return True
    if option_ltp <= trailed_stop:
        close_paper_position(direction, state, _round_tick(option_ltp), "TRAILING_STOP")
        return True
    if option_ltp >= float(state["target_price"]):
        close_paper_position(direction, state, _round_tick(option_ltp), "TARGET")
        return True
    write_state(direction, state)
    return True


def fetch_live_positions() -> list[dict]:
    response = requests.get(POSITIONS_URL, headers=_auth_headers(), timeout=15)
    payload = _response_json(response, "positions")
    data = payload.get("data") or []
    return data if isinstance(data, list) else []


def cancel_gtt(gtt_order_id: str) -> None:
    response = requests.delete(
        GTT_CANCEL_URL,
        headers=_auth_headers(),
        json={"gtt_order_id": gtt_order_id},
        timeout=15,
    )
    _response_json(response, "GTT cancellation")


def squareoff_live_direction(direction: str, state: dict) -> bool:
    gtt_order_id = str(state.get("gtt_order_id") or "")
    if gtt_order_id:
        try:
            cancel_gtt(gtt_order_id)
        except Exception as error:
            log(f"{direction} GTT cancel check: {error}")
    position = next(
        (
            item for item in fetch_live_positions()
            if str(item.get("instrument_token") or item.get("instrument_key")) == state["instrument_key"]
            and int(float(item.get("quantity") or 0)) != 0
        ),
        None,
    )
    if position is None:
        write_state(direction, {})
        log(f"LIVE {direction} GTT has no open broker quantity at first-4H close")
        return True
    quantity = abs(int(float(position.get("quantity") or 0)))
    payload = {
        "quantity": quantity,
        "product": "I",
        "validity": "DAY",
        "price": 0,
        "tag": f"ml4h_{direction.lower()}_close",
        "instrument_token": state["instrument_key"],
        "order_type": "MARKET",
        "transaction_type": "SELL",
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
        "slice": True,
        "market_protection": -1,
    }
    response = requests.post(ORDER_PLACE_URL, headers=_auth_headers(), json=payload, timeout=20)
    result = _response_json(response, "first-4H square-off")
    state["status"] = "SQUAREOFF_SENT"
    state["squareoff_response"] = result
    write_state(direction, state)
    log(f"LIVE {direction} first-4H square-off sent for qty={quantity}")
    return True


def monitor_once(force_squareoff: bool = False) -> bool:
    migrate_legacy_state()
    active = False
    for direction in ("CALL", "PUT"):
        state = read_state(direction)
        if not state.get("instrument_key"):
            continue
        active = True
        if state.get("paper_trade"):
            monitor_paper_direction(direction, state, force_squareoff)
        elif force_squareoff or now_ist() >= datetime.fromisoformat(state["expires_at"]):
            squareoff_live_direction(direction, state)
    return active


def _configured_minutes(name: str, default: str) -> int:
    value = os.getenv(name, default).strip()
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must use HH:MM") from error
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise RuntimeError(f"{name} must use HH:MM")
    return hour * 60 + minute


def live_enabled() -> bool:
    return configured_bool("ENABLE_LIVE_TRADING", False) and configured_bool(
        "ML_SHADOW_LIVE_TRADING_ENABLED", False
    )


def scan() -> dict:
    load_env()
    migrate_legacy_state()
    if os.getenv("TRADING_ENGINE", "").strip().upper() != ENGINE:
        raise RuntimeError(f"TRADING_ENGINE must be {ENGINE}")
    mode = "LIVE_GTT" if live_enabled() else "PAPER"
    if mode == "PAPER" and not configured_bool("ML_SHADOW_PAPER_ENABLED", True):
        raise RuntimeError("Paper mode is disabled and both live switches are not enabled")
    if configured_bool("ML_SHADOW_LIVE_TRADING_ENABLED", False) != configured_bool("ENABLE_LIVE_TRADING", False):
        raise RuntimeError("Both ENABLE_LIVE_TRADING and ML_SHADOW_LIVE_TRADING_ENABLED must match")

    current_minutes = now_ist().hour * 60 + now_ist().minute
    first_entry = _configured_minutes("ML_SHADOW_FIRST_ENTRY_TIME", "09:17")
    last_entry = _configured_minutes("ML_SHADOW_LAST_ENTRY_TIME", "09:30")
    if current_minutes >= 13 * 60 + 16:
        resolved = resolve_prediction_outcomes(fetch_resolution_candles())
        log(f"first-4H resolution pass; resolved={resolved}")
        return {"overall_action": "RESOLVE_ONLY", "resolved": resolved}
    if not first_entry <= current_minutes <= last_entry:
        raise RuntimeError("Outside first-4H forecast entry window")

    artifact, metadata = load_artifact()
    prediction = score_latest(artifact, metadata)
    if prediction_already_recorded(prediction["candle_time"]):
        log(f"first-4H forecast for {prediction['candle_time']} already recorded")
        return {"overall_action": "DUPLICATE", **prediction}
    decisions = choose_actions(prediction)
    actions = []
    for direction, prefix in (("CALL", "call"), ("PUT", "put")):
        decision = decisions[direction]
        action = "NO_TRADE"
        reason = decision["reason"]
        if decision["qualified"]:
            if read_state(direction).get("instrument_key"):
                reason = f"{direction} lane already has active state"
            else:
                try:
                    if mode == "LIVE_GTT":
                        place_live_gtt(prediction, decision)
                        action = "LIVE_GTT"
                    else:
                        open_paper_position(prediction, decision)
                        action = "PAPER_ENTRY"
                    reason = "independent probability and reward/risk qualified"
                except Exception as error:
                    action = "ERROR"
                    reason = str(error)
        prediction[f"{prefix}_action"] = action
        prediction[f"{prefix}_reason"] = reason
        actions.append(action)
    entries = [action for action in actions if action in {"PAPER_ENTRY", "LIVE_GTT"}]
    prediction.update({
        "scan_time": now_ist().isoformat(),
        "execution_mode": mode,
        "overall_action": "BOTH" if len(entries) == 2 else entries[0] if entries else "NO_TRADE",
    })
    append_prediction(prediction)
    log(
        f"first4h={prediction['candle_time']} call={prediction['call_probability']:.3f}/"
        f"rr{prediction['call_reward_risk']:.2f}/{prediction['call_action']} "
        f"put={prediction['put_probability']:.3f}/rr{prediction['put_reward_risk']:.2f}/"
        f"{prediction['put_action']} mode={mode}"
    )
    return prediction


def monitor_loop() -> None:
    load_env()
    interval = max(configured_float("ML_SHADOW_MONITOR_INTERVAL_SECONDS", 2), 1)
    log(f"CALL/PUT GTT-style monitor started at {interval:g}-second cadence")
    while now_ist().time() <= clock_time(13, 20):
        try:
            monitor_once()
        except Exception as error:
            log(f"monitor error: {error}")
        time.sleep(interval)
    log("first-4H monitor stopped")


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
    else:
        monitor_once(force_squareoff=True)


if __name__ == "__main__":
    main()
