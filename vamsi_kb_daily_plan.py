"""Build the frozen pre-market plan for the VAMSI knowledge engine.

The planner uses only completed post-market observations from dates before the
plan date.  It may soften at most one research gate per index for the session;
execution, quote-quality, stale-data and account-risk safeguards are never
relaxed here.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from safe_storage import atomic_write_json, file_lock
import trade_bot


VERSION = "VAMSI_KB_DAILY_PLAN_V1"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "vamsi_kb_intraday"
AUDIT_FILE = DATA_DIR / "post_market_followthrough.csv"
PLAN_FILE = DATA_DIR / "daily_trade_plan.json"
PLAN_LOCK_FILE = BASE_DIR / ".vamsi_kb_daily_plan.lock"
SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX")
PRIORITY = {symbol: index + 1 for index, symbol in enumerate(SYMBOLS)}
RELAXABLE_GATES = (
    "setup",
    "completed_candles",
    "breadth",
    "option_chain",
    "option_flow",
)
GATE_CATEGORIES = {
    "setup": "REJECT · SETUP / REGIME",
    "completed_candles": "REJECT · 5M + 15M ALIGNMENT",
    "breadth": "REJECT · CONSTITUENT BREADTH",
    "option_chain": "REJECT · OPTION CHAIN",
    "option_flow": "REJECT · OPTION VWAP / VOLUME",
}
DEFAULT_POINTS = {
    "NIFTY": (30.0, 30.0),
    "BANKNIFTY": (60.0, 60.0),
    "SENSEX": (40.0, 40.0),
}


def _configured_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be numeric") from error


def _configured_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be numeric") from error


def _points(symbol: str) -> tuple[float, float]:
    default_target, default_stop = DEFAULT_POINTS[symbol]
    target = _configured_float(
        f"VAMSI_KB_{symbol}_TARGET_POINTS",
        _configured_float("VAMSI_KB_TARGET_POINTS", default_target),
    )
    stop = _configured_float(
        f"VAMSI_KB_{symbol}_STOP_POINTS",
        _configured_float("VAMSI_KB_STOP_POINTS", default_stop),
    )
    return target, stop


def _categories(value) -> set[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    return {str(item) for item in parsed if str(item).startswith("REJECT ·")}


def _as_bool(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _weighted_stats(frame: pd.DataFrame, target: float, stop: float, plan_date: date) -> dict:
    if frame.empty:
        return {
            "samples": 0,
            "tradingDays": 0,
            "targetHitRate": None,
            "stopHitRate": None,
            "averageFavorablePoints": None,
            "medianFavorablePoints": None,
            "averageAdversePoints": None,
            "expectedIndexPoints": None,
        }
    half_life = max(_configured_float("VAMSI_KB_PLAN_RECENCY_HALF_LIFE_DAYS", 10), 1)
    ages = frame["trading_date_value"].map(lambda item: max((plan_date - item).days, 1))
    weights = ages.map(lambda age: math.pow(0.5, age / half_life)).astype(float)
    weight_sum = float(weights.sum()) or 1.0
    target_hits = frame["target_hit_value"].astype(float)
    # A later stop after the configured target was already reached is not a
    # losing path for the fixed-target entry policy.
    stop_hits = (
        frame["stop_hit_value"] & ~frame["target_hit_value"]
    ).astype(float)
    favorable = frame["favorable_value"].astype(float)
    adverse = frame["adverse_value"].astype(float)
    target_rate = float((target_hits * weights).sum() / weight_sum)
    stop_rate = float((stop_hits * weights).sum() / weight_sum)
    expected = target_rate * target - stop_rate * stop
    return {
        "samples": int(len(frame)),
        "tradingDays": int(frame["trading_date_value"].nunique()),
        "targetHitRate": round(target_rate * 100, 1),
        "stopHitRate": round(stop_rate * 100, 1),
        "averageFavorablePoints": round(float((favorable * weights).sum() / weight_sum), 2),
        "medianFavorablePoints": round(float(favorable.median()), 2),
        "averageAdversePoints": round(float((adverse * weights).sum() / weight_sum), 2),
        "expectedIndexPoints": round(expected, 2),
    }


def _eligible(stats: dict, target: float) -> bool:
    return bool(
        stats["samples"] >= _configured_int("VAMSI_KB_PLAN_MIN_SAMPLES", 6)
        and stats["tradingDays"] >= _configured_int("VAMSI_KB_PLAN_MIN_TRADING_DAYS", 2)
        and (stats["targetHitRate"] or 0)
        >= _configured_float("VAMSI_KB_PLAN_MIN_TARGET_HIT_RATE", 35)
        and (stats["averageFavorablePoints"] or 0)
        >= target * _configured_float("VAMSI_KB_PLAN_MIN_FAVORABLE_TARGET_RATIO", 0.75)
        and (stats["expectedIndexPoints"] or 0)
        >= _configured_float("VAMSI_KB_PLAN_MIN_EXPECTED_INDEX_POINTS", 0)
    )


def _prepare_frame(path: Path, plan_date: date) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()
    required = {
        "trading_date",
        "symbol",
        "categories_json",
        "favorable_points_before_stop",
        "adverse_points_before_stop",
        "target_hit_before_stop",
        "stop_hit",
    }
    if frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame()
    frame = frame.copy()
    frame["trading_date_value"] = pd.to_datetime(
        frame["trading_date"], errors="coerce"
    ).dt.date
    frame = frame[
        frame["trading_date_value"].notna()
        & (frame["trading_date_value"] < plan_date)
    ]
    lookback = max(_configured_int("VAMSI_KB_PLAN_LOOKBACK_DAYS", 60), 1)
    frame = frame[frame["trading_date_value"] >= plan_date - timedelta(days=lookback)]
    frame["symbol"] = frame["symbol"].fillna("").astype(str).str.upper()
    frame["categories"] = frame["categories_json"].map(_categories)
    frame["favorable_value"] = pd.to_numeric(
        frame["favorable_points_before_stop"], errors="coerce"
    )
    frame["adverse_value"] = pd.to_numeric(
        frame["adverse_points_before_stop"], errors="coerce"
    ).fillna(0)
    frame["target_hit_value"] = frame["target_hit_before_stop"].map(_as_bool)
    frame["stop_hit_value"] = frame["stop_hit"].map(_as_bool)
    return frame.dropna(subset=["favorable_value"])


def _isolated_gate_rows(frame: pd.DataFrame, gate: str) -> pd.DataFrame:
    wanted = GATE_CATEGORIES[gate]

    def isolated(categories: set[str]) -> bool:
        return categories == {wanted}

    return frame[frame["categories"].map(isolated)]


def build_daily_plan(plan_date=None, audit_file=AUDIT_FILE) -> dict:
    plan_date = plan_date or trade_bot.now_ist().date()
    if isinstance(plan_date, str):
        plan_date = date.fromisoformat(plan_date)
    frame = _prepare_frame(Path(audit_file), plan_date)
    training_through = ""
    if not frame.empty:
        training_through = max(frame["trading_date_value"]).isoformat()

    symbol_plans = {}
    for symbol in SYMBOLS:
        target, stop = _points(symbol)
        symbol_frame = frame[frame["symbol"] == symbol] if not frame.empty else frame
        candidates = []
        for gate in RELAXABLE_GATES:
            stats = _weighted_stats(
                _isolated_gate_rows(symbol_frame, gate), target, stop, plan_date
            )
            candidates.append(
                {
                    "gate": gate,
                    "eligible": _eligible(stats, target),
                    **stats,
                }
            )
        qualified = [item for item in candidates if item["eligible"]]
        qualified.sort(
            key=lambda item: (
                item["expectedIndexPoints"],
                item["targetHitRate"],
                item["samples"],
                -RELAXABLE_GATES.index(item["gate"]),
            ),
            reverse=True,
        )
        selected = qualified[:1]
        relaxed = [item["gate"] for item in selected]
        symbol_plans[symbol] = {
            "priority": PRIORITY[symbol],
            "mode": "ONE_GATE_ADAPTIVE" if relaxed else "STRICT",
            "eligibleScoreBuckets": ["80-89", "90-100"] if relaxed else ["90-100"],
            "relaxedGates": relaxed,
            "maximumRelaxedFailuresPerCandidate": 1 if relaxed else 0,
            "targetPoints": target,
            "stopPoints": stop,
            "selectedEvidence": selected,
            "gateRanking": candidates,
        }

    return {
        "version": VERSION,
        "planDate": plan_date.isoformat(),
        "generatedAt": trade_bot.now_ist().isoformat(),
        "trainingThrough": training_through,
        "lookbackDays": _configured_int("VAMSI_KB_PLAN_LOOKBACK_DAYS", 60),
        "indexPriority": list(SYMBOLS),
        "symbols": symbol_plans,
        "policy": (
            "One evidence gate per index may change daily. Completed historical days "
            "only; contract quality, entry freshness, data health and execution/risk "
            "controls remain hard gates. The plan is frozen for its planDate."
        ),
    }


def write_daily_plan(plan: dict, path=PLAN_FILE) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(destination, plan, sort_keys=True)


def load_daily_plan(current=None, path=PLAN_FILE) -> dict | None:
    current = current or trade_bot.now_ist()
    try:
        plan = json.loads(Path(path).read_text())
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(plan, dict):
        return None
    if plan.get("version") != VERSION or plan.get("planDate") != current.date().isoformat():
        return None
    return plan


def generate(plan_date=None, audit_file=AUDIT_FILE, output_file=PLAN_FILE) -> dict:
    trade_bot.load_env()
    with file_lock(PLAN_LOCK_FILE):
        plan = build_daily_plan(plan_date=plan_date, audit_file=audit_file)
        write_daily_plan(plan, output_file)
    choices = ", ".join(
        f"{symbol}={','.join(item['relaxedGates']) or 'strict'}"
        for symbol, item in plan["symbols"].items()
    )
    trade_bot.log(f"VAMSI_KB_DAILY_PLAN | {plan['planDate']} frozen plan: {choices}")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--date", help="Plan date in YYYY-MM-DD; defaults to today IST")
    parser.add_argument("--audit-file", default=str(AUDIT_FILE))
    parser.add_argument("--output-file", default=str(PLAN_FILE))
    args = parser.parse_args()
    if not args.generate:
        parser.error("choose --generate")
    plan = generate(args.date, args.audit_file, args.output_file)
    print(json.dumps(plan, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
