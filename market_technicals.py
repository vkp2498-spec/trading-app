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

def get_option_volume_vwap_analysis(instrument_key, side_label="OPTION"):
    try:
        df_5 = fetch_v3_intraday_minutes(instrument_key, minutes=5)
        if len(df_5) < 25:
            df_5 = fetch_v3_historical_minutes(instrument_key, minutes=5, lookback_days=5)

        df_5 = add_indicators(df_5)
        valid = df_5.dropna(subset=["close"])

        if valid.empty:
            return {
                "label": side_label,
                "bias": "NEUTRAL",
                "confidence": "LOW",
                "reasons": ["No valid option candle data"],
            }

        last = valid.iloc[-1]

        close = float(last.get("close") or 0)
        volume = float(last.get("volume") or 0)
        volume_ma20 = float(last.get("volume_ma20") or 0)
        volume_ratio = float(last.get("volume_ratio") or 0) if volume_ma20 else 0

        vwap = float(last.get("vwap")) if pd.notna(last.get("vwap")) else None
        vwap_series = df_5["vwap"].dropna() if "vwap" in df_5.columns else pd.Series(dtype=float)
        prev_vwap = float(vwap_series.iloc[-2]) if len(vwap_series) >= 2 else vwap
        vwap_slope = round(vwap - prev_vwap, 4) if vwap is not None and prev_vwap is not None else None

        volume_confirmed = volume_ma20 > 0 and volume > volume_ma20

        score = 0
        reasons = []

        if volume_confirmed:
            score += 1
            reasons.append("ATM option volume is above 20-period average")
        else:
            reasons.append("ATM option volume is not above 20-period average")

        if vwap is not None and close > vwap and (vwap_slope is None or vwap_slope >= 0):
            score += 1
            reasons.append("ATM option premium is above VWAP and VWAP is flat/up")
        elif vwap is not None and close < vwap and (vwap_slope is None or vwap_slope <= 0):
            score -= 1
            reasons.append("ATM option premium is below VWAP and VWAP is flat/down")
        else:
            reasons.append("ATM option VWAP is neutral/unavailable")

        bias = "BULLISH" if score >= 1 else "BEARISH" if score <= -1 else "NEUTRAL"
        confidence = "HIGH" if abs(score) >= 2 else "MEDIUM" if abs(score) == 1 else "LOW"

        return {
            "label": side_label,
            "bias": bias,
            "confidence": confidence,
            "score": score,
            "close": round(close, 2),
            "volume": round(volume, 2),
            "volume_ma20": round(volume_ma20, 2),
            "volume_ratio": round(volume_ratio, 2),
            "volume_confirmed": bool(volume_confirmed),
            "vwap": round(vwap, 2) if vwap is not None else None,
            "vwap_slope": vwap_slope,
            "reasons": reasons,
        }

    except Exception as e:
        return {
            "label": side_label,
            "bias": "NEUTRAL",
            "confidence": "LOW",
            "reasons": [f"Option volume/VWAP analysis failed: {e}"],
        }


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

    typical_price = (out["high"] + out["low"] + out["close"]) / 3
    cumulative_volume = out["volume"].replace(0, pd.NA).fillna(0).cumsum()
    cumulative_pv = (typical_price * out["volume"]).cumsum()

    out["vwap"] = cumulative_pv / cumulative_volume.replace(0, pd.NA)
    out["volume_ma20"] = out["volume"].rolling(20).mean()
    out["volume_ratio"] = out["volume"] / out["volume_ma20"]
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

    valid = df.dropna(subset=["close", "pivot", "ma20", "bb_upper", "bb_lower", "r1", "s1"])

    if valid.empty:
        return {
            "timeframe": timeframe,
            "bias": "NEUTRAL",
            "confidence": "LOW",
            "target": None,
            "stop_loss": None,
            "reasons": ["Not enough valid indicator rows after calculations"],
        }

    last = valid.iloc[-1]
    close = float(last["close"])
    pivot = float(last["pivot"])
    ma20 = float(last["ma20"])
    upper = float(last["bb_upper"])
    lower = float(last["bb_lower"])
    r1 = float(last["r1"])
    s1 = float(last["s1"])
    volume = float(last.get("volume", 0) or 0)
    volume_ma20 = float(last.get("volume_ma20", 0) or 0)
    volume_ratio = float(last.get("volume_ratio", 0) or 0) if volume_ma20 else 0

    vwap = float(last.get("vwap")) if pd.notna(last.get("vwap")) else None
    vwap_series = df["vwap"].dropna() if "vwap" in df.columns else pd.Series(dtype=float)
    prev_vwap_value = vwap_series.iloc[-2] if len(vwap_series) >= 2 else None
    prev_vwap = float(prev_vwap_value) if prev_vwap_value is not None else vwap

    vwap_slope = None
    if vwap is not None and prev_vwap is not None:
        vwap_slope = round(vwap - prev_vwap, 4)
    close_series = df["close"].dropna()
    prev_close = float(close_series.iloc[-2]) if len(close_series) >= 2 else close
    recent_closes = df["close"].dropna().tail(4)
    recent_avg = float(recent_closes.mean()) if len(recent_closes) else close

    momentum_score = 0
    momentum_reasons = []

    if close > prev_close:
        momentum_score += 1
        momentum_reasons.append("Latest close is above previous close")
    elif close < prev_close:
        momentum_score -= 1
        momentum_reasons.append("Latest close is below previous close")

    if close > recent_avg:
        momentum_score += 1
        momentum_reasons.append("Latest close is above recent 4-candle average")
    elif close < recent_avg:
        momentum_score -= 1
        momentum_reasons.append("Latest close is below recent 4-candle average")

    if close > pivot:
        momentum_score += 1
        momentum_reasons.append("Latest close is above pivot")
    elif close < pivot:
        momentum_score -= 1
        momentum_reasons.append("Latest close is below pivot")

    if close > ma20:
        momentum_score += 1
        momentum_reasons.append("Latest close is above middle band")
    elif close < ma20:
        momentum_score -= 1
        momentum_reasons.append("Latest close is below middle band")

    volume_confirmed = volume_ma20 > 0 and volume > volume_ma20
    vwap_bias = "NEUTRAL"

    if vwap is not None:
        if close > vwap and (vwap_slope is None or vwap_slope >= 0):
            vwap_bias = "BULLISH"
            momentum_score += 1
            momentum_reasons.append("Close is above VWAP and VWAP is flat/up")
        elif close < vwap and (vwap_slope is None or vwap_slope <= 0):
            vwap_bias = "BEARISH"
            momentum_score -= 1
            momentum_reasons.append("Close is below VWAP and VWAP is flat/down")

    if volume_confirmed:
        momentum_reasons.append("Volume is above 20-period average")
    else:
        momentum_reasons.append("Volume is not above 20-period average")

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

        bearish_targets = [level for level in [s1, lower] if level < close]
        bearish_stops = [level for level in [r1, ma20, upper] if level > close]

        target = round(max(bearish_targets), 2) if bearish_targets else round(close * 0.995, 2)
        stop_loss = round(min(bearish_stops), 2) if bearish_stops else round(close * 1.005, 2)
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
        "prev_close": round(prev_close, 2),
        "recent_avg_close": round(recent_avg, 2),
        "momentum_score": momentum_score,
        "momentum_reasons": momentum_reasons,
        "volume": round(volume, 2),
        "volume_ma20": round(volume_ma20, 2),
        "volume_ratio": round(volume_ratio, 2),
        "volume_confirmed": bool(volume_confirmed),
        "vwap": round(vwap, 2) if vwap is not None else None,
        "vwap_slope": vwap_slope,
        "vwap_bias": vwap_bias,
    }


def get_technical_analysis(symbol):
    instrument_key = INDEX_KEYS[symbol]

    df_15 = fetch_v3_intraday_minutes(instrument_key, minutes=15)
    if len(df_15) < 25:
        df_15 = fetch_v3_historical_minutes(instrument_key, minutes=15, lookback_days=5)

    df_4h = fetch_v3_historical_hours(instrument_key, hours=4, lookback_days=60)

    df_5 = fetch_v3_intraday_minutes(instrument_key, minutes=5)
    if len(df_5) < 25:
        df_5 = fetch_v3_historical_minutes(instrument_key, minutes=5, lookback_days=5)

    return {
        "four_hour": analyze_latest(df_4h, "4H"),
        "fifteen_min": analyze_latest(df_15, "15M"),
        "five_min": analyze_latest(df_5, "5M"),
    }