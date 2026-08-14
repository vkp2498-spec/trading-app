"""Leakage-safe, fixed-window simulator for the opening V2 model.

The simulator trains the production V2 model family on an 18-month window and
evaluates it on the following six months without refitting.  It intentionally
uses only the first 4-hour NIFTY candle for each session.  Option P&L is an
explicit delta approximation; it is not a historical option-chain backtest.
"""

from __future__ import annotations

import argparse
import math
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import ml_shadow_4h_v2_live as v2
import ml_shadow_v1 as execution
from backtest_data import UpstoxBacktestData


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "ml_v2_simulator"
HISTORY_FILE = DATA_DIR / "nifty_first_4h.csv.gz"
RR_CUTOFFS = (None, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.5, 2.0)


@dataclass(frozen=True)
class SimulationAssumptions:
    probability_cutoff: float = 0.50
    rr_cutoff: float | None = None
    option_premium: float = 150.0
    option_delta: float = 0.50
    capital_per_trade: float = 100_000.0
    round_trip_cost: float = 0.0


def _as_timestamp(value) -> pd.Timestamp:
    return pd.Timestamp(value).tz_localize(None).normalize()


def save_history(candles: pd.DataFrame, path: Path = HISTORY_FILE) -> Path:
    frame = execution._as_ist(candles)
    if frame.empty:
        raise ValueError("No first-4H candles were available to save")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index_label="timestamp", compression="gzip")
    return path


def load_history(path: Path = HISTORY_FILE) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    frame = pd.read_csv(path, index_col="timestamp", parse_dates=["timestamp"])
    return v2.first_candle_per_day(frame)


def import_cache_folder(folder: Path, destination: Path = HISTORY_FILE) -> pd.DataFrame:
    """Consolidate Upstox candle-cache files into the simulator history file."""
    frames = []
    for path in sorted(Path(folder).rglob("*.csv.gz")):
        try:
            frame = pd.read_csv(path, index_col="timestamp", parse_dates=["timestamp"])
        except (OSError, ValueError):
            continue
        if not frame.empty and {"open", "high", "low", "close"}.issubset(frame.columns):
            frames.append(frame)
    if not frames:
        raise ValueError(f"No candle cache files found under {folder}")
    candles = v2.first_candle_per_day(pd.concat(frames).sort_index())
    save_history(candles, destination)
    return candles


def fetch_history(
    end_date: date | None = None,
    study_months: int = 24,
    warmup_calendar_days: int = 140,
    progress=None,
) -> pd.DataFrame:
    """Fetch the study range plus hidden warm-up history and save it locally."""
    execution.load_env()
    end = end_date or (execution.now_ist().date() - timedelta(days=1))
    visible_start = (_as_timestamp(end) - pd.DateOffset(months=study_months)).date()
    start = visible_start - timedelta(days=warmup_calendar_days)
    source = UpstoxBacktestData(
        DATA_DIR / "history_cache",
        progress=progress or (lambda _message: None),
        pause_seconds=0.12,
    )
    frames = []
    cursor = start.replace(day=1)
    while cursor <= end:
        chunk_start = max(start, cursor)
        chunk_end = min(end, cursor.replace(day=monthrange(cursor.year, cursor.month)[1]))
        frame = source.candles(v2.NIFTY_KEY, "4hour", chunk_start, chunk_end, expired=False)
        if not frame.empty:
            frames.append(frame)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    candles = v2.first_candle_per_day(
        pd.concat(frames).sort_index() if frames else pd.DataFrame()
    )
    save_history(candles)
    return candles


def labeled_samples(candles: pd.DataFrame) -> pd.DataFrame:
    """Build production V2 features and first-candle excursion labels."""
    samples = v2._labeled_features(v2.first_candle_per_day(candles))
    return samples.dropna(subset=v2.FEATURE_COLUMNS + ["call_label", "put_label"])


def window_bounds(test_start, train_months: int = 18, test_months: int = 6):
    test_start = _as_timestamp(test_start)
    return (
        test_start - pd.DateOffset(months=train_months),
        test_start,
        test_start + pd.DateOffset(months=test_months) - pd.Timedelta(days=1),
    )


def default_test_start(samples: pd.DataFrame, test_months: int = 6) -> pd.Timestamp:
    if samples.empty:
        raise ValueError("No usable samples")
    return _as_timestamp(samples.index.max()) - pd.DateOffset(months=test_months) + pd.Timedelta(days=1)


def split_samples(
    samples: pd.DataFrame,
    test_start,
    train_months: int = 18,
    test_months: int = 6,
) -> tuple[pd.DataFrame, pd.DataFrame, tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    train_start, test_start, test_end = window_bounds(test_start, train_months, test_months)
    index = pd.DatetimeIndex(samples.index).tz_localize(None)
    training = samples[(index >= train_start) & (index < test_start)].copy()
    testing = samples[(index >= test_start) & (index <= test_end)].copy()
    if len(training) < 240:
        raise ValueError(f"The 18-month training window has only {len(training)} usable sessions; need 240")
    if len(testing) < 60:
        raise ValueError(f"The six-month test window has only {len(testing)} usable sessions; need 60")
    for label in ("call_label", "put_label"):
        if training[label].nunique() < 2:
            raise ValueError(f"Training window needs both classes for {label}")
    return training, testing, (train_start, test_start, test_end)


def fit_and_forecast(
    samples: pd.DataFrame,
    test_start,
    train_months: int = 18,
    test_months: int = 6,
) -> tuple[pd.DataFrame, dict]:
    """Fit once on the training window and forecast every holdout session."""
    training, testing, bounds = split_samples(samples, test_start, train_months, test_months)
    sk = execution._sklearn_imports()
    X_train = training[v2.FEATURE_COLUMNS]
    X_test = testing[v2.FEATURE_COLUMNS]
    output = testing[["open", "high", "low", "close", "future_up_percent", "future_down_percent"]].copy()
    for prefix in ("call", "put"):
        classifier = execution._classifier(sk).fit(X_train, training[f"{prefix}_label"])
        target_model = execution._regressor(sk, 0.50).fit(
            X_train, training["future_up_percent" if prefix == "call" else "future_down_percent"]
        )
        stop_model = execution._regressor(sk, 0.75).fit(
            X_train, training["future_down_percent" if prefix == "call" else "future_up_percent"]
        )
        probability = execution._positive_probability(classifier, X_test)
        target = np.maximum(target_model.predict(X_test), 0.001)
        stop = np.maximum(stop_model.predict(X_test), 0.001)
        output[f"{prefix}_probability"] = probability
        output[f"{prefix}_target_percent"] = target
        output[f"{prefix}_stop_percent"] = stop
        output[f"{prefix}_reward_risk"] = target / stop
        output[f"{prefix}_expected_value_r"] = v2.expected_value_r(probability, target / stop)
    metadata = {
        "train_start": bounds[0],
        "train_end": bounds[1] - pd.Timedelta(days=1),
        "test_start": bounds[1],
        "test_end": bounds[2],
        "training_sessions": len(training),
        "test_sessions": len(testing),
    }
    return output, metadata


def _trade_outcome(row: pd.Series, direction: str, assumptions: SimulationAssumptions) -> dict:
    prefix = direction.lower()
    opening = float(row["open"])
    target_percent = float(row[f"{prefix}_target_percent"])
    stop_percent = float(row[f"{prefix}_stop_percent"])
    target_underlying_points = opening * target_percent / 100
    stop_underlying_points = opening * stop_percent / 100
    if direction == "CALL":
        target_hit = float(row["high"]) >= opening + target_underlying_points
        stop_hit = float(row["low"]) <= opening - stop_underlying_points
        close_points = float(row["close"]) - opening
    else:
        target_hit = float(row["low"]) <= opening - target_underlying_points
        stop_hit = float(row["high"]) >= opening + stop_underlying_points
        close_points = opening - float(row["close"])

    option_target_points = target_underlying_points * assumptions.option_delta
    option_stop_points = min(
        stop_underlying_points * assumptions.option_delta,
        assumptions.option_premium,
    )
    if stop_hit:
        # A 4H OHLC bar cannot reveal ordering. Treat both-hit candles as a stop.
        outcome = "STOP" if not target_hit else "BOTH_STOP_FIRST"
        option_points = -option_stop_points
    elif target_hit:
        outcome = "TARGET"
        option_points = option_target_points
    else:
        outcome = "CLOSE"
        option_points = max(close_points * assumptions.option_delta, -assumptions.option_premium)

    quantity = assumptions.capital_per_trade / assumptions.option_premium
    gross_pnl = option_points * quantity
    net_pnl = gross_pnl - assumptions.round_trip_cost
    return {
        "direction": direction,
        "probability": float(row[f"{prefix}_probability"]),
        "predicted_target_percent": target_percent,
        "predicted_stop_percent": stop_percent,
        "predicted_reward_risk": float(row[f"{prefix}_reward_risk"]),
        "expected_value_r": float(row[f"{prefix}_expected_value_r"]),
        "option_entry": assumptions.option_premium,
        "option_target_points": option_target_points,
        "option_stop_points": option_stop_points,
        "outcome": outcome,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
    }


def simulate_trades(
    forecasts: pd.DataFrame,
    assumptions: SimulationAssumptions,
    rule: str = "rr",
    minimum_ev_r: float = 0.10,
) -> pd.DataFrame:
    """Turn qualified CALL/PUT forecasts into independent ₹1L deployments."""
    rows = []
    for timestamp, forecast in forecasts.iterrows():
        for direction in ("CALL", "PUT"):
            prefix = direction.lower()
            probability = float(forecast[f"{prefix}_probability"])
            rr = float(forecast[f"{prefix}_reward_risk"])
            ev = float(forecast[f"{prefix}_expected_value_r"])
            qualifies = probability > assumptions.probability_cutoff
            if rule == "ev":
                qualifies = qualifies and ev >= minimum_ev_r
            elif assumptions.rr_cutoff is not None:
                qualifies = qualifies and rr >= assumptions.rr_cutoff
            if not qualifies:
                continue
            result = _trade_outcome(forecast, direction, assumptions)
            result["timestamp"] = timestamp
            rows.append(result)
    if not rows:
        return pd.DataFrame(columns=[
            "timestamp", "direction", "probability", "predicted_target_percent",
            "predicted_stop_percent", "predicted_reward_risk", "expected_value_r",
            "option_entry", "option_target_points", "option_stop_points", "outcome",
            "gross_pnl", "net_pnl",
        ])
    return pd.DataFrame(rows).sort_values(["timestamp", "direction"]).reset_index(drop=True)


def summarize(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {
            "trades": 0, "calls": 0, "puts": 0, "targets": 0, "stops": 0,
            "closes": 0, "profitable_trades": 0, "win_probability": 0.0,
            "target_hit_rate": 0.0, "average_target_points": 0.0,
            "average_stop_points": 0.0, "gross_pnl": 0.0, "net_pnl": 0.0,
            "expectancy": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0,
        }
    pnl = pd.to_numeric(trades["net_pnl"], errors="coerce").fillna(0.0)
    winners = pnl[pnl > 0]
    losers = pnl[pnl < 0]
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax().clip(lower=0)
    targets = int((trades["outcome"] == "TARGET").sum())
    stops = int(trades["outcome"].isin(["STOP", "BOTH_STOP_FIRST"]).sum())
    profit_factor = winners.sum() / abs(losers.sum()) if not losers.empty else math.inf if not winners.empty else 0.0
    return {
        "trades": len(trades),
        "calls": int((trades["direction"] == "CALL").sum()),
        "puts": int((trades["direction"] == "PUT").sum()),
        "targets": targets,
        "stops": stops,
        "closes": int((trades["outcome"] == "CLOSE").sum()),
        "profitable_trades": int((pnl > 0).sum()),
        "win_probability": float((pnl > 0).mean()),
        "target_hit_rate": targets / len(trades),
        "average_target_points": float(trades["option_target_points"].mean()),
        "average_stop_points": float(trades["option_stop_points"].mean()),
        "gross_pnl": float(trades["gross_pnl"].sum()),
        "net_pnl": float(pnl.sum()),
        "expectancy": float(pnl.mean()),
        "profit_factor": float(profit_factor),
        "max_drawdown": float(abs(drawdown.min())),
    }


def compare_rr_cutoffs(
    forecasts: pd.DataFrame,
    base_assumptions: SimulationAssumptions,
    cutoffs=RR_CUTOFFS,
) -> pd.DataFrame:
    rows = []
    for cutoff in cutoffs:
        assumptions = SimulationAssumptions(**{**base_assumptions.__dict__, "rr_cutoff": cutoff})
        summary = summarize(simulate_trades(forecasts, assumptions, rule="rr"))
        rows.append({"rr_cutoff": "Any" if cutoff is None else f"{cutoff:g}", **summary})
    return pd.DataFrame(rows)


def probability_rr_surface(
    forecasts: pd.DataFrame,
    base_assumptions: SimulationAssumptions,
    probability_cutoffs=None,
    rr_cutoffs=None,
) -> pd.DataFrame:
    """Net P&L and sample count for every probability/RR combination and side."""
    probabilities = probability_cutoffs or tuple(np.round(np.arange(0.50, 0.91, 0.05), 2))
    reward_risks = rr_cutoffs or (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.5, 2.0)
    rows = []
    for probability in probabilities:
        for reward_risk in reward_risks:
            assumptions = SimulationAssumptions(**{
                **base_assumptions.__dict__,
                "probability_cutoff": float(probability),
                "rr_cutoff": None if float(reward_risk) == 0 else float(reward_risk),
            })
            trades = simulate_trades(forecasts, assumptions, rule="rr")
            for direction in ("CALL", "PUT"):
                side = trades[trades["direction"] == direction]
                summary = summarize(side)
                rows.append({
                    "direction": direction,
                    "probability_cutoff": float(probability),
                    "rr_cutoff": float(reward_risk),
                    "trades": summary["trades"],
                    "net_pnl": summary["net_pnl"],
                    "win_probability": summary["win_probability"],
                })
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-data", action="store_true", help="Download and cache the latest history")
    parser.add_argument("--import-cache", type=Path, help="Import an existing Upstox candle-cache folder")
    parser.add_argument("--end-date", type=date.fromisoformat)
    args = parser.parse_args()
    if args.import_cache:
        frame = import_cache_folder(args.import_cache)
        print(f"Imported {len(frame)} first-4H candles to {HISTORY_FILE}")
    elif args.refresh_data:
        frame = fetch_history(args.end_date, progress=print)
        print(f"Saved {len(frame)} first-4H candles to {HISTORY_FILE}")
    else:
        frame = load_history()
        samples = labeled_samples(frame)
        print(f"Loaded {len(frame)} candles and {len(samples)} usable samples")


if __name__ == "__main__":
    main()
