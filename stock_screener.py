"""Short-horizon NSE equity research screener.

This module is deliberately read-only: it produces research candidates and
does not place orders. Results are cached so the mobile app remains useful
when the broker API is temporarily unavailable.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import tempfile
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Thread
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from market_technicals import upstox_headers

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RESULT_FILE = DATA_DIR / "stock_screener.json"
RUNNING_FILE = DATA_DIR / "stock_screener.running"
INSTRUMENTS_FILE = DATA_DIR / "complete.json.gz"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
HISTORICAL_URL = "https://api.upstox.com/v3/historical-candle"


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _cache_instruments() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if INSTRUMENTS_FILE.exists() and (datetime.now().timestamp() - INSTRUMENTS_FILE.stat().st_mtime) < 86400:
        return INSTRUMENTS_FILE
    response = requests.get(INSTRUMENTS_URL, timeout=90)
    response.raise_for_status()
    fd, temporary = tempfile.mkstemp(dir=DATA_DIR, suffix=".gz")
    try:
        with open(fd, "wb", closefd=True) as handle:
            handle.write(response.content)
        os.replace(temporary, INSTRUMENTS_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return INSTRUMENTS_FILE


def equity_universe() -> list[dict]:
    with gzip.open(_cache_instruments(), "rt", encoding="utf-8") as handle:
        rows = json.load(handle)
    result = []
    seen = set()
    for row in rows:
        if str(row.get("segment") or "") != "NSE_EQ":
            continue
        key = row.get("instrument_key")
        symbol = str(row.get("trading_symbol") or row.get("short_name") or "").upper()
        if not key or not symbol or symbol in seen:
            continue
        seen.add(symbol)
        is_etf = " ETF" in f" {symbol} " or "ETF" in str(row.get("name") or "").upper()
        if is_etf:
            continue
        result.append({
            "instrumentKey": key,
            "symbol": symbol,
            "name": row.get("name") or symbol,
            "isETF": False,
        })
    return result


def _candles(instrument_key: str, days: int = 400, unit: str = "days", interval: int = 1) -> pd.DataFrame:
    today = datetime.now(IST).date()
    start = today - timedelta(days=days)
    url = f"{HISTORICAL_URL}/{instrument_key}/{unit}/{interval}/{today}/{start}"
    response = requests.get(url, headers=upstox_headers(), timeout=25)
    response.raise_for_status()
    rows = []
    for candle in (response.json().get("data", {}) or {}).get("candles", []) or []:
        if len(candle) < 6:
            continue
        rows.append({"timestamp": pd.to_datetime(candle[0]), "open": _number(candle[1]), "high": _number(candle[2]), "low": _number(candle[3]), "close": _number(candle[4]), "volume": _number(candle[5])})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("timestamp").drop_duplicates("timestamp").set_index("timestamp")


def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gains = delta.clip(lower=0).rolling(period).mean()
    losses = (-delta.clip(upper=0)).rolling(period).mean()
    last_loss = float(losses.iloc[-1]) if not losses.empty else 0
    if last_loss == 0:
        return 100.0
    return 100 - (100 / (1 + float(gains.iloc[-1]) / last_loss))


def _candle_signal(frame: pd.DataFrame) -> tuple[str, str] | None:
    """Return a simple, explainable signal from the two most recent candles.

    This is deliberately limited to strong body/wick patterns.  It is a
    confirmation input, not a standalone trading strategy.
    """
    if len(frame) < 2:
        return None
    previous = frame.iloc[-2]
    current = frame.iloc[-1]

    def parts(candle):
        body = abs(float(candle["close"]) - float(candle["open"]))
        candle_range = max(float(candle["high"]) - float(candle["low"]), 1e-9)
        upper = float(candle["high"]) - max(float(candle["open"]), float(candle["close"]))
        lower = min(float(candle["open"]), float(candle["close"])) - float(candle["low"])
        return body, candle_range, upper, lower

    body, candle_range, upper, lower = parts(current)
    previous_open = float(previous["open"])
    previous_close = float(previous["close"])
    current_open = float(current["open"])
    current_close = float(current["close"])
    bullish = current_close > current_open
    bearish = current_close < current_open
    previous_bearish = previous_close < previous_open
    previous_bullish = previous_close > previous_open

    if bullish and previous_bearish and current_open <= previous_close and current_close >= previous_open:
        return "Bullish engulfing", "BULLISH"
    if bearish and previous_bullish and current_open >= previous_close and current_close <= previous_open:
        return "Bearish engulfing", "BEARISH"
    if bullish and lower >= max(body * 2, candle_range * 0.45) and upper <= max(body, candle_range * 0.15):
        return "Hammer", "BULLISH"
    if bearish and upper >= max(body * 2, candle_range * 0.45) and lower <= max(body, candle_range * 0.15):
        return "Shooting star", "BEARISH"
    return None


def _evaluate(asset: dict, frame: pd.DataFrame, four_hour: pd.DataFrame) -> dict | None:
    # Avoid scoring a candle while it is still forming.  This matters because
    # the screener can be run during market hours.
    def completed(data: pd.DataFrame, interval_hours: int) -> pd.DataFrame:
        if data.empty:
            return data
        timestamps = pd.DatetimeIndex(pd.to_datetime(data.index))
        if timestamps.tz is None:
            timestamps = timestamps.tz_localize(IST)
        else:
            timestamps = timestamps.tz_convert(IST)
        cutoff = pd.Timestamp(datetime.now(IST)) - pd.Timedelta(hours=interval_hours)
        return data.loc[timestamps <= cutoff]

    frame = completed(frame, 24)
    four_hour = completed(four_hour, 4)
    if len(frame) < 80:
        return None
    close = frame["close"]
    latest = float(close.iloc[-1])
    minimum_price = _number(os.getenv("STOCK_SCREENER_MIN_PRICE"), 50.0)
    if latest < minimum_price:
        return None
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    rolling20 = close.rolling(20)
    middle = float(rolling20.mean().iloc[-1])
    std = float(rolling20.std().iloc[-1])
    upper = middle + 2 * std
    atr = float((frame["high"] - frame["low"]).rolling(14).mean().iloc[-1])
    rsi = _rsi(close)
    recent_volume = float(frame["volume"].tail(20).mean())
    volume_ratio = float(frame["volume"].iloc[-1]) / recent_volume if recent_volume > 0 else 1
    average_turnover = recent_volume * latest
    minimum_turnover = _number(os.getenv("STOCK_SCREENER_MIN_AVG_TURNOVER"), 5_000_000.0)
    if recent_volume > 0 and average_turnover < minimum_turnover:
        return None
    if len(four_hour) < 50:
        return None
    four_close = four_hour["close"]
    four_ema20 = float(four_close.ewm(span=20, adjust=False).mean().iloc[-1])
    four_ema50 = float(four_close.ewm(span=50, adjust=False).mean().iloc[-1])
    four_hour_up = float(four_close.iloc[-1]) > four_ema20 > four_ema50

    daily_candle = _candle_signal(frame)
    four_hour_candle = _candle_signal(four_hour)

    score = 0.0
    reasons = []
    if latest > ema20 > ema50:
        score += 30; reasons.append("Price is above rising 20/50-day averages")
    elif latest > ema20:
        score += 15; reasons.append("Price is above the 20-day average")
    if four_hour_up:
        score += 25; reasons.append("4-hour trend confirms higher highs")
    elif float(four_close.iloc[-1]) > four_ema20:
        score += 10; reasons.append("4-hour price is above its 20-candle average")
    if 52 <= rsi <= 72:
        score += 20; reasons.append("RSI supports constructive momentum")
    elif rsi > 72:
        score += 8; reasons.append("Momentum is strong but extended")
    if latest > middle and latest < upper:
        score += 15; reasons.append("Price is above Bollinger middle band")
    if volume_ratio >= 1.1:
        score += 10; reasons.append("Recent volume is above its 20-day average")
    candle_score = 0.0
    candle_pattern = daily_candle[0] if daily_candle else None
    candle_direction = daily_candle[1] if daily_candle else "NEUTRAL"
    if daily_candle:
        candle_score = 5.0 if daily_candle[1] == "BULLISH" else -5.0
        reasons.append(f"{daily_candle[0]} on daily candles ({candle_score:+.0f} confirmation points)")
        if four_hour_candle and four_hour_candle[1] == daily_candle[1]:
            candle_score += 3.0 if daily_candle[1] == "BULLISH" else -3.0
            reasons.append(f"4-hour {four_hour_candle[0].lower()} confirms the daily direction")
        elif four_hour_candle and four_hour_candle[1] != daily_candle[1]:
            candle_score = 0.0
            reasons.append("4-hour candle disagrees; candle bonus withheld")
    score += candle_score
    if score < 55 or atr <= 0 or latest <= 0:
        return None
    target = round(latest + max(atr * 2.5, latest * 0.04), 2)
    stop = round(max(latest - atr * 1.25, latest * 0.94), 2)
    investment_amount = _number(os.getenv("STOCK_SCREENER_INVESTMENT_AMOUNT"), 100_000.0)
    quantity = max(1, int(investment_amount // latest))
    invested_value = round(quantity * latest, 2)
    potential_profit = round(max((target - latest) * quantity, 0), 2)
    potential_loss = round(max((latest - stop) * quantity, 0), 2)
    reward_risk = round(potential_profit / potential_loss, 2) if potential_loss else 0.0
    minimum_reward_risk = _number(os.getenv("STOCK_SCREENER_MIN_REWARD_RISK"), 1.50)
    if reward_risk < minimum_reward_risk:
        return None
    probability = round(min(85, max(55, 50 + score * 0.38)), 1)
    return {
        "symbol": asset["symbol"], "name": asset["name"], "instrumentKey": asset["instrumentKey"],
        "isETF": asset["isETF"], "lastPrice": round(latest, 2), "targetPrice": target,
        "stopLossPrice": stop, "probabilityUp": probability, "score": round(score, 1),
        "investmentAmount": investment_amount, "quantity": quantity,
        "investedValue": invested_value, "potentialProfit": potential_profit,
        "potentialLoss": potential_loss, "rewardRisk": reward_risk,
        "averageDailyTurnover": round(average_turnover, 2),
        "trend": "BULLISH", "horizon": "Up to 1 week", "rsi14": round(rsi, 1),
        "reasons": reasons, "asOf": datetime.now(IST).isoformat(),
        "candlePattern": candle_pattern,
        "candleDirection": candle_direction,
        "candleConfirmation": "CONFIRMED" if daily_candle and four_hour_candle and daily_candle[1] == four_hour_candle[1] else "DAILY ONLY" if daily_candle else "NONE",
        "candleScore": round(candle_score, 1),
    }


def load_result() -> dict:
    if RESULT_FILE.exists():
        try:
            return json.loads(RESULT_FILE.read_text())
        except (OSError, ValueError):
            pass
    return {"status": "NOT_RUN", "asOf": None, "recommendations": [], "invested": []}


def run_screener() -> dict:
    universe = equity_universe()
    maximum = max(20, int(os.getenv("STOCK_SCREENER_MAX_SYMBOLS", "250")))
    # A deterministic liquid-first approximation keeps the button responsive;
    # increase STOCK_SCREENER_MAX_SYMBOLS for a broader scan.
    candidates = universe[:maximum]
    recommendations = []
    errors = 0
    workers = max(2, min(12, int(os.getenv("STOCK_SCREENER_WORKERS", "8"))))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_screen_asset, asset): asset for asset in candidates}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    recommendations.append(result)
            except Exception:
                errors += 1
    recommendations.sort(key=lambda row: (row["potentialProfit"], row["rewardRisk"], row["probabilityUp"]), reverse=True)
    previous = load_result()
    invested = _refresh_invested(previous.get("invested", []), universe, recommendations)
    result = {"status": "READY", "asOf": datetime.now(IST).isoformat(), "scanned": len(candidates), "errors": errors, "recommendations": recommendations[:10], "invested": invested}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(result, indent=2))
    return result


def _background_screener() -> None:
    try:
        run_screener()
    except Exception:
        previous = load_result()
        previous["status"] = "ERROR"
        previous["error"] = "The stock scan failed. Please try again."
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RESULT_FILE.write_text(json.dumps(previous, indent=2))
    finally:
        RUNNING_FILE.unlink(missing_ok=True)


def start_screener() -> dict:
    """Start a scan without holding the mobile HTTP request open."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if RUNNING_FILE.exists():
        age_seconds = datetime.now().timestamp() - RUNNING_FILE.stat().st_mtime
        if age_seconds < 900:
            return {**load_result(), "status": "RUNNING"}
        RUNNING_FILE.unlink(missing_ok=True)

    try:
        descriptor = os.open(RUNNING_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(descriptor)
    except FileExistsError:
        return {**load_result(), "status": "RUNNING"}

    Thread(target=_background_screener, name="stock-screener", daemon=True).start()
    return {**load_result(), "status": "RUNNING"}


def _screen_asset(asset: dict) -> dict | None:
    daily = _candles(asset["instrumentKey"], days=400, unit="days", interval=1)
    four_hour = _candles(asset["instrumentKey"], days=60, unit="hours", interval=4)
    return _evaluate(asset, daily, four_hour)


def get_screener() -> dict:
    result = load_result()
    if RUNNING_FILE.exists():
        result["status"] = "RUNNING"
    return result


def save_invested(items: list[dict]) -> dict:
    result = load_result()
    result["invested"] = _refresh_invested(items[:20], [], result.get("recommendations", []))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(result, indent=2))
    return result


def _refresh_invested(items: list[dict], universe: list[dict], recommendations: list[dict]) -> list[dict]:
    """Attach the latest available status without making the read API scan."""
    by_symbol = {row.get("symbol"): row for row in universe}
    by_symbol.update({row.get("symbol"): row for row in recommendations})
    refreshed = []
    for item in items:
        row = dict(item)
        candidate = by_symbol.get(row.get("symbol"))
        if candidate and candidate.get("lastPrice"):
            row.update({key: candidate[key] for key in ("lastPrice", "targetPrice", "stopLossPrice", "probabilityUp", "trend", "rsi14") if key in candidate})
            last = _number(row.get("lastPrice"))
            stop = _number(row.get("stopLossPrice"))
            target = _number(row.get("targetPrice"))
            row["status"] = "EXIT" if last <= stop else "TARGET REACHED" if last >= target else candidate.get("trend", "NEUTRAL")
        else:
            row.setdefault("status", "NEUTRAL")
        refreshed.append(row)
    return refreshed
