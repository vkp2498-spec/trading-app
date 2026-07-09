import os
import socket
from datetime import timedelta

import pandas as pd
import requests
import urllib3.util.connection as urllib3_cn

from strategy_core import now_ist

urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

UPSTOX_BASE = "https://api.upstox.com"
INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}


def upstox_headers():
    token = os.getenv("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_ANALYTICS_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN or UPSTOX_ANALYTICS_TOKEN not set")

    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _parse_candles(payload):
    candles = payload.get("data", {}).get("candles", []) or []
    rows = []

    for candle in candles:
        if len(candle) < 5:
            continue
        rows.append(
            {
                "timestamp": pd.to_datetime(candle[0]),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]) if len(candle) > 5 and candle[5] is not None else 0,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df = df.sort_values("timestamp").drop_duplicates("timestamp")
    df = df.set_index("timestamp")
    return df

def fetch_v3_historical_hours(instrument_key, hours=4, lookback_days=60):
    to_date = now_ist().date()
    from_date = to_date - timedelta(days=lookback_days)

    url = (
        f"{UPSTOX_BASE}/v3/historical-candle/"
        f"{instrument_key}/hours/{hours}/{to_date}/{from_date}"
    )

    response = requests.get(url, headers=upstox_headers(), timeout=30)
    if response.status_code >= 300:
        raise RuntimeError(f"Upstox hourly candle API failed {response.status_code}: {response.text[:500]}")

    return _parse_candles(response.json())


def fetch_v3_historical_minutes(instrument_key, minutes=15, lookback_days=7):
    to_date = now_ist().date()
    from_date = to_date - timedelta(days=lookback_days)

    url = (
        f"{UPSTOX_BASE}/v3/historical-candle/"
        f"{instrument_key}/minutes/{minutes}/{to_date}/{from_date}"
    )

    response = requests.get(url, headers=upstox_headers(), timeout=30)
    if response.status_code >= 300:
        raise RuntimeError(f"Upstox candle API failed {response.status_code}: {response.text[:500]}")

    return _parse_candles(response.json())


def fetch_v3_intraday_minutes(instrument_key, minutes=15):
    url = f"{UPSTOX_BASE}/v3/historical-candle/intraday/{instrument_key}/minutes/{minutes}"

    response = requests.get(url, headers=upstox_headers(), timeout=30)
    if response.status_code >= 300:
        raise RuntimeError(f"Upstox intraday candle API failed {response.status_code}: {response.text[:500]}")

    return _parse_candles(response.json())


def resample_ohlc(df, rule):
    if df.empty:
        return df

    out = df.resample(rule).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    return out.dropna()

def convert_index_levels_to_option_premium(analysis, option_side, option_entry_price, delta=0.5):
    """
    Converts index-level technical target/stop into approximate option premium target/stop.
    option_side: CE or PE
    """
    if not analysis or analysis.get("bias") == "NEUTRAL":
        return analysis

    close = analysis.get("close")
    index_target = analysis.get("target")
    index_stop = analysis.get("stop_loss")

    if close is None or index_target is None or index_stop is None or option_entry_price is None:
        return analysis

    close = float(close)
    index_target = float(index_target)
    index_stop = float(index_stop)
    entry = float(option_entry_price)

    if option_side == "CE":
        target_move = index_target - close
        stop_move = index_stop - close
    elif option_side == "PE":
        target_move = close - index_target
        stop_move = close - index_stop
    else:
        return analysis

    premium_target = round(entry + (target_move * delta), 0)
    premium_stop = round(entry + (stop_move * delta), 0)

    # Keep only sensible option-buying levels.
    if premium_target <= entry:
        premium_target = None
    if premium_stop >= entry:
        premium_stop = None

    updated = dict(analysis)
    updated["option_delta_used"] = delta
    updated["option_entry_price"] = round(entry, 2)
    updated["option_target_price"] = premium_target
    updated["option_stop_loss_price"] = premium_stop
    updated["option_conversion_reason"] = (
        f"Converted index target/stop to option premium using delta={delta}"
    )

    return updated


def add_indicators(df):
    if df.empty:
        return df

    out = df.copy()
    out["ma20"] = out["close"].rolling(20).mean()
    out["std20"] = out["close"].rolling(20).std()
    out["bb_upper"] = out["ma20"] + 2 * out["std20"]
    out["bb_lower"] = out["ma20"] - 2 * out["std20"]

    prev = out.shift(1)
    out["pivot"] = (prev["high"] + prev["low"] + prev["close"]) / 3
    out["r1"] = 2 * out["pivot"] - prev["low"]
    out["s1"] = 2 * out["pivot"] - prev["high"]
    out["r2"] = out["pivot"] + (prev["high"] - prev["low"])
    out["s2"] = out["pivot"] - (prev["high"] - prev["low"])
    return out


def analyze_latest(df, timeframe):
    if df.empty or len(df) < 25:
        return {
            "timeframe": timeframe,
            "bias": "NEUTRAL",
            "confidence": "LOW",
            "target": None,
            "stop_loss": None,
            "reasons": ["Not enough candle data"],
        }

    df = add_indicators(df)
    last = df.dropna().iloc[-1]
    close = float(last["close"])
    pivot = float(last["pivot"])
    ma20 = float(last["ma20"])
    upper = float(last["bb_upper"])
    lower = float(last["bb_lower"])
    r1 = float(last["r1"])
    s1 = float(last["s1"])

    score = 0
    reasons = []

    if close > pivot:
        score += 1
        reasons.append("Close is above pivot")
    elif close < pivot:
        score -= 1
        reasons.append("Close is below pivot")

    if close > ma20:
        score += 1
        reasons.append("Close is above Bollinger middle band / MA20")
    elif close < ma20:
        score -= 1
        reasons.append("Close is below Bollinger middle band / MA20")

    if close > upper:
        score += 1
        reasons.append("Close is above upper Bollinger band, showing strong upside momentum")
    elif close < lower:
        score -= 1
        reasons.append("Close is below lower Bollinger band, showing strong downside momentum")
    else:
        reasons.append("Close is inside Bollinger bands")

    if score >= 2:
        bias = "BULLISH"
        target = round(r1, 2)
        stop_loss = round(max(s1, lower), 2)
    elif score <= -2:
        bias = "BEARISH"
        target = round(s1, 2)
        stop_loss = round(min(r1, upper), 2)
    else:
        bias = "NEUTRAL"
        target = None
        stop_loss = None

    confidence = "HIGH" if abs(score) >= 3 else "MEDIUM" if abs(score) == 2 else "LOW"

    return {
        "timeframe": timeframe,
        "bias": bias,
        "confidence": confidence,
        "score": score,
        "close": round(close, 2),
        "pivot": round(pivot, 2),
        "middle_band": round(ma20, 2),
        "upper_band": round(upper, 2),
        "lower_band": round(lower, 2),
        "target": target,
        "stop_loss": stop_loss,
        "reasons": reasons,
    }


def get_technical_analysis(symbol):
    instrument_key = INDEX_KEYS[symbol]

    df_15 = fetch_v3_intraday_minutes(instrument_key, minutes=15)
    if len(df_15) < 25:
        df_15 = fetch_v3_historical_minutes(instrument_key, minutes=15, lookback_days=5)

    df_4h = fetch_v3_historical_hours(instrument_key, hours=4, lookback_days=60)

    return {
        "four_hour": analyze_latest(df_4h, "4H"),
        "fifteen_min": analyze_latest(df_15, "15M"),
    }