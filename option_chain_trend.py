import csv
from pathlib import Path

import pandas as pd

from strategy_core import now_ist

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TREND_FILE = DATA_DIR / "option_chain_trend.csv"

COLUMNS = [
    "timestamp",
    "symbol",
    "bias",
    "confidence",
    "score",
    "pcr_oi",
    "pcr_volume",
    "total_ce_oi",
    "total_pe_oi",
    "total_ce_volume",
    "total_pe_volume",
]


def ensure_file():
    DATA_DIR.mkdir(exist_ok=True)
    if not TREND_FILE.exists():
        with TREND_FILE.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=COLUMNS).writeheader()


def record_option_chain_snapshot(symbol, rec):
    ensure_file()

    totals = rec.get("chain_totals", {}) or {}
    row = {
        "timestamp": now_ist().isoformat(),
        "symbol": symbol,
        "bias": rec.get("direction"),
        "confidence": rec.get("confidence"),
        "score": rec.get("score"),
        "pcr_oi": totals.get("pcr_oi"),
        "pcr_volume": totals.get("pcr_volume"),
        "total_ce_oi": totals.get("total_ce_oi"),
        "total_pe_oi": totals.get("total_pe_oi"),
        "total_ce_volume": totals.get("total_ce_volume"),
        "total_pe_volume": totals.get("total_pe_volume"),
    }

    with TREND_FILE.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writerow(row)

    return row


def get_option_chain_trend(symbol, direction, lookback=5):
    ensure_file()

    df = pd.read_csv(TREND_FILE)
    if df.empty:
        return {
            "bias": "NEUTRAL",
            "score": 0,
            "reasons": ["Not enough option-chain trend history"],
        }

    df = df[df["symbol"] == symbol].tail(lookback).copy()
    df["pcr_oi"] = pd.to_numeric(df["pcr_oi"], errors="coerce")
    df["score"] = pd.to_numeric(df["score"], errors="coerce")

    df = df.dropna(subset=["pcr_oi"])
    if len(df) < 3:
        return {
            "bias": "NEUTRAL",
            "score": 0,
            "reasons": ["Less than 3 option-chain snapshots available"],
        }

    first_pcr = float(df["pcr_oi"].iloc[0])
    last_pcr = float(df["pcr_oi"].iloc[-1])
    pcr_change = last_pcr - first_pcr

    score = 0
    reasons = []

    if pcr_change > 0.05:
        score += 2
        reasons.append(f"PCR OI rising over recent snapshots: {first_pcr:.2f} -> {last_pcr:.2f}")
    elif pcr_change < -0.05:
        score -= 2
        reasons.append(f"PCR OI falling over recent snapshots: {first_pcr:.2f} -> {last_pcr:.2f}")
    else:
        reasons.append(f"PCR OI mostly flat: {first_pcr:.2f} -> {last_pcr:.2f}")

    avg_score = float(df["score"].fillna(0).mean())
    if avg_score >= 3:
        score += 1
        reasons.append("Recent option-chain scores are mostly bullish")
    elif avg_score <= -3:
        score -= 1
        reasons.append("Recent option-chain scores are mostly bearish")

    trend_bias = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"

    aligns = (
        direction == trend_bias
        or trend_bias == "NEUTRAL"
        or direction == "NEUTRAL"
    )

    return {
        "bias": trend_bias,
        "score": score,
        "pcr_start": round(first_pcr, 4),
        "pcr_latest": round(last_pcr, 4),
        "pcr_change": round(pcr_change, 4),
        "aligns_with_option_signal": aligns,
        "reasons": reasons,
    }