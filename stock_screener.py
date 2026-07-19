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
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Thread
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from market_technicals import upstox_headers
from stock_futures_scanner import NIFTY50_SYMBOLS

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RESULT_FILE = DATA_DIR / "stock_screener.json"
RUNNING_FILE = DATA_DIR / "stock_screener.running"
INSTRUMENTS_FILE = DATA_DIR / "complete.json.gz"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
HISTORICAL_URL = "https://api.upstox.com/v3/historical-candle"
MARKET_LTP_URL = "https://api.upstox.com/v3/market-quote/ltp"
BENCHMARK_KEY = "NSE_INDEX|Nifty 50"


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _get_json(url: str, **kwargs) -> dict:
    """GET broker data with bounded retries for throttling and transient faults."""
    last_error = None
    timeout = kwargs.pop("timeout", 25)
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=timeout, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    delay = _number(response.headers.get("Retry-After"), 2 ** attempt)
                    time.sleep(max(0.5, min(delay, 8.0)))
                    continue
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except (requests.RequestException, ValueError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Upstox data request failed: {last_error}") from last_error


def _write_result(result: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=DATA_DIR, suffix=".json")
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(result, handle, indent=2)
        os.replace(temporary, RESULT_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
    configured = os.getenv("STOCK_SCREENER_SYMBOLS", "").strip()
    allowed_symbols = (
        {item.strip().upper() for item in configured.split(",") if item.strip()}
        if configured and configured.upper() != "ALL"
        else None if configured.upper() == "ALL"
        else set(NIFTY50_SYMBOLS)
    )
    result = []
    seen = set()
    for row in rows:
        if str(row.get("segment") or "") != "NSE_EQ":
            continue
        key = row.get("instrument_key")
        symbol = str(row.get("trading_symbol") or row.get("short_name") or "").upper()
        if not key or not symbol or symbol in seen:
            continue
        instrument_type = str(row.get("instrument_type") or "").upper()
        if instrument_type and instrument_type not in {"EQ", "EQUITY"}:
            continue
        if allowed_symbols is not None and symbol not in allowed_symbols:
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
    return sorted(result, key=lambda item: item["symbol"])


def _candles(instrument_key: str, days: int = 400, unit: str = "days", interval: int = 1) -> pd.DataFrame:
    today = datetime.now(IST).date()
    start = today - timedelta(days=days)
    url = f"{HISTORICAL_URL}/{instrument_key}/{unit}/{interval}/{today}/{start}"
    payload = _get_json(url, headers=upstox_headers(), timeout=25)
    rows = []
    for candle in (payload.get("data", {}) or {}).get("candles", []) or []:
        if len(candle) < 6:
            continue
        rows.append({"timestamp": pd.to_datetime(candle[0]), "open": _number(candle[1]), "high": _number(candle[2]), "low": _number(candle[3]), "close": _number(candle[4]), "volume": _number(candle[5])})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("timestamp").drop_duplicates("timestamp").set_index("timestamp")


def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    last_gain = float(gains.iloc[-1]) if not gains.empty else 0
    last_loss = float(losses.iloc[-1]) if not losses.empty else 0
    if last_gain == 0 and last_loss == 0:
        return 50.0
    if last_loss == 0:
        return 100.0
    return 100 - (100 / (1 + last_gain / last_loss))


def _wilder_atr(frame: pd.DataFrame, period: int = 14) -> float:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return _number(atr.iloc[-1]) if not atr.empty else 0.0


def _completed_candles(
    data: pd.DataFrame,
    unit: str,
    interval: int,
    now: datetime | None = None,
) -> pd.DataFrame:
    if data.empty:
        return data
    current = now or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    else:
        current = current.astimezone(IST)
    timestamps = pd.DatetimeIndex(pd.to_datetime(data.index))
    if timestamps.tz is None:
        timestamps = timestamps.tz_localize(IST)
    else:
        timestamps = timestamps.tz_convert(IST)
    if unit == "days":
        market_closed = current.time() >= datetime.strptime("15:30", "%H:%M").time()
        mask = (timestamps.date < current.date()) | (
            (timestamps.date == current.date()) & market_closed
        )
    else:
        mask = timestamps + pd.Timedelta(hours=interval) <= pd.Timestamp(current)
    return data.iloc[list(mask)]


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


def _evaluate(
    asset: dict,
    frame: pd.DataFrame,
    four_hour: pd.DataFrame,
    benchmark: pd.DataFrame | None = None,
    now: datetime | None = None,
) -> dict | None:
    frame = _completed_candles(frame, "days", 1, now)
    four_hour = _completed_candles(four_hour, "hours", 4, now)
    benchmark = _completed_candles(benchmark, "days", 1, now) if benchmark is not None else pd.DataFrame()
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
    atr = _wilder_atr(frame)
    rsi = _rsi(close)
    # The signal candle must not inflate its own comparison baseline.
    recent_volume = float(frame["volume"].shift(1).tail(20).mean())
    volume_ratio = float(frame["volume"].iloc[-1]) / recent_volume if recent_volume > 0 else 1
    average_turnover = float((frame["close"] * frame["volume"]).shift(1).tail(20).mean())
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
        score += 25; reasons.append("Price is above rising 20/50-day averages")
    elif latest > ema20:
        score += 10; reasons.append("Price is above the 20-day average")
    else:
        score -= 15; reasons.append("Price is below the 20-day average")
    if four_hour_up:
        score += 15; reasons.append("4-hour trend confirms the daily uptrend")
    elif float(four_close.iloc[-1]) > four_ema20:
        score += 7; reasons.append("4-hour price is above its 20-candle average")
    else:
        score -= 7; reasons.append("4-hour price is below its 20-candle average")
    if 52 <= rsi <= 68:
        score += 10; reasons.append("RSI supports constructive momentum")
    elif 68 < rsi <= 75:
        score += 5; reasons.append("Momentum is strong but becoming extended")
    elif rsi > 75:
        score -= 5; reasons.append("RSI is overextended")
    elif rsi < 45:
        score -= 10; reasons.append("RSI momentum is weak")
    if latest > middle and latest < upper:
        score += 10; reasons.append("Price is above Bollinger middle band without exceeding the upper band")
    elif latest >= upper:
        score -= 5; reasons.append("Price is above the upper Bollinger band and may be extended")
    else:
        score -= 10; reasons.append("Price is below the Bollinger middle band")
    if volume_ratio >= 1.2:
        score += 10; reasons.append("Recent volume is above its 20-day average")
    elif volume_ratio >= 1.0:
        score += 5; reasons.append("Recent volume is near its 20-day average")

    relative_strength = None
    market_regime = "UNAVAILABLE"
    if len(benchmark) >= 50:
        benchmark_close = benchmark["close"]
        benchmark_ema20 = float(benchmark_close.ewm(span=20, adjust=False).mean().iloc[-1])
        benchmark_ema50 = float(benchmark_close.ewm(span=50, adjust=False).mean().iloc[-1])
        benchmark_latest = float(benchmark_close.iloc[-1])
        market_regime = "BULLISH" if benchmark_latest > benchmark_ema20 > benchmark_ema50 else "DEFENSIVE"
        if market_regime == "BULLISH":
            score += 10; reasons.append("NIFTY market regime supports weekly long positions")
        else:
            score -= 10; reasons.append("NIFTY market regime is not supportive")
        stock_return = latest / float(close.iloc[-21]) - 1 if len(close) >= 21 else 0
        benchmark_return = benchmark_latest / float(benchmark_close.iloc[-21]) - 1 if len(benchmark_close) >= 21 else 0
        relative_strength = (stock_return - benchmark_return) * 100
        if relative_strength >= 3:
            score += 15; reasons.append("Stock is outperforming NIFTY over 20 sessions")
        elif relative_strength >= 0:
            score += 8; reasons.append("Stock is holding up better than NIFTY")
        elif relative_strength <= -3:
            score -= 10; reasons.append("Stock is materially underperforming NIFTY")
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
    minimum_score = _number(os.getenv("STOCK_SCREENER_MIN_SCORE"), 60.0)
    if score < minimum_score or atr <= 0 or latest <= 0:
        return None

    prior_high = _number(frame["high"].shift(1).tail(20).max())
    target_options = [
        (upper, "upper Bollinger band"),
        (prior_high, "prior 20-session resistance"),
        (latest + atr * 2.0, "two-ATR weekly move"),
    ]
    target, target_basis = min(
        ((price, basis) for price, basis in target_options if price > latest * 1.005),
        key=lambda value: value[0],
        default=(latest + atr * 2.0, "two-ATR weekly move"),
    )

    swing_low = _number(frame["low"].shift(1).tail(10).min())
    support_options = [
        (ema20, "20-day EMA"),
        (middle, "Bollinger middle band"),
        (swing_low, "prior 10-session support"),
        (latest - atr * 1.5, "1.5-ATR risk level"),
    ]
    technical_stop, stop_basis = max(
        ((price, basis) for price, basis in support_options if 0 < price < latest),
        key=lambda value: value[0],
        default=(latest - atr * 1.5, "1.5-ATR risk level"),
    )
    minimum_risk_distance = max(atr * 0.75, latest * 0.015)
    maximum_risk_distance = max(
        minimum_risk_distance,
        min(atr * 2.0, latest * 0.06),
    )
    stop = min(technical_stop, latest - minimum_risk_distance)
    stop = max(stop, latest - maximum_risk_distance)
    target = round(target, 2)
    stop = round(stop, 2)
    investment_amount = _number(os.getenv("STOCK_SCREENER_INVESTMENT_AMOUNT"), 100_000.0)
    quantity = int(investment_amount // latest)
    if quantity < 1:
        return None
    invested_value = round(quantity * latest, 2)
    potential_profit = round(max((target - latest) * quantity, 0), 2)
    potential_loss = round(max((latest - stop) * quantity, 0), 2)
    reward_risk = round(potential_profit / potential_loss, 2) if potential_loss else 0.0
    minimum_reward_risk = _number(os.getenv("STOCK_SCREENER_MIN_REWARD_RISK"), 1.50)
    if reward_risk < minimum_reward_risk:
        return None
    setup_strength = round(min(100, max(0, score)), 1)
    return {
        "symbol": asset["symbol"], "name": asset["name"], "instrumentKey": asset["instrumentKey"],
        "isETF": asset["isETF"], "lastPrice": round(latest, 2), "targetPrice": target,
        # probabilityUp remains numeric for older clients, but now mirrors the
        # explicitly uncalibrated setup score instead of claiming odds.
        "stopLossPrice": stop, "probabilityUp": setup_strength, "probabilityAvailable": False,
        "probabilityLabel": "Uncalibrated setup strength; not a win probability",
        "setupStrength": setup_strength, "score": round(score, 1),
        "investmentAmount": investment_amount, "quantity": quantity,
        "investedValue": invested_value, "potentialProfit": potential_profit,
        "potentialLoss": potential_loss, "rewardRisk": reward_risk,
        "averageDailyTurnover": round(average_turnover, 2),
        "volumeRatio": round(volume_ratio, 2),
        "atr14": round(atr, 2),
        "relativeStrength20D": round(relative_strength, 2) if relative_strength is not None else None,
        "marketRegime": market_regime,
        "targetBasis": target_basis,
        "stopBasis": stop_basis,
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
    maximum = max(1, int(os.getenv("STOCK_SCREENER_MAX_SYMBOLS", "50")))
    # The default universe is the explicit NIFTY 50 list. Custom symbols can be
    # supplied with STOCK_SCREENER_SYMBOLS; raw instrument-file ordering is not
    # treated as a liquidity ranking.
    candidates = universe[:maximum]
    recommendations = []
    errors = 0
    error_details = []
    successful = 0
    benchmark_error = None
    try:
        benchmark = _candles(BENCHMARK_KEY, days=400, unit="days", interval=1)
    except Exception as error:
        benchmark = pd.DataFrame()
        benchmark_error = {
            "symbol": "NIFTY_BENCHMARK",
            "type": type(error).__name__,
            "message": str(error)[:180],
        }
    workers = max(1, min(8, int(os.getenv("STOCK_SCREENER_WORKERS", "4"))))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_screen_asset, asset, benchmark): asset for asset in candidates}
        for future in as_completed(futures):
            asset = futures[future]
            try:
                result = future.result()
                successful += 1
                if result:
                    recommendations.append(result)
            except Exception as error:
                errors += 1
                if len(error_details) < 20:
                    error_details.append({
                        "symbol": asset.get("symbol"),
                        "type": type(error).__name__,
                        "message": str(error)[:180],
                    })
    recommendations.sort(
        key=lambda row: (
            row["setupStrength"],
            row["rewardRisk"],
            row["averageDailyTurnover"],
        ),
        reverse=True,
    )
    previous = load_result()
    if not successful and errors:
        result = {
            **previous,
            "status": "ERROR",
            "asOf": datetime.now(IST).isoformat(),
            "scanned": len(candidates),
            "successful": 0,
            "errors": errors,
            "errorDetails": error_details,
            "error": "No symbols could be evaluated; previous recommendations were retained.",
        }
        _write_result(result)
        return result
    top_recommendations = recommendations[:10]
    prices = _latest_prices(previous.get("invested", []))
    invested = _refresh_invested(previous.get("invested", []), universe, top_recommendations, prices)
    coverage = round(successful / len(candidates) * 100, 1) if candidates else 0.0
    result = {
        "status": "READY" if errors == 0 else "PARTIAL",
        "asOf": datetime.now(IST).isoformat(),
        "scanned": len(candidates),
        "successful": successful,
        "coveragePercent": coverage,
        "errors": errors,
        "errorDetails": error_details,
        "warnings": [benchmark_error] if benchmark_error else [],
        "recommendations": top_recommendations,
        "invested": invested,
    }
    _write_result(result)
    return result


def _background_screener() -> None:
    try:
        run_screener()
    except Exception:
        previous = load_result()
        previous["status"] = "ERROR"
        previous["error"] = "The stock scan failed. Please try again."
        _write_result(previous)
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


def _screen_asset(asset: dict, benchmark: pd.DataFrame | None = None) -> dict | None:
    daily = _candles(asset["instrumentKey"], days=400, unit="days", interval=1)
    four_hour = _candles(asset["instrumentKey"], days=60, unit="hours", interval=4)
    return _evaluate(asset, daily, four_hour, benchmark)


def get_screener(refresh_invested: bool = True) -> dict:
    result = load_result()
    if refresh_invested and result.get("invested"):
        prices = _latest_prices(result.get("invested", []))
        refreshed = _refresh_invested(
            result.get("invested", []),
            [],
            result.get("recommendations", []),
            prices,
        )
        if refreshed != result.get("invested"):
            result["invested"] = refreshed
            _write_result(result)
    if RUNNING_FILE.exists():
        result["status"] = "RUNNING"
    return result


def save_invested(items: list[dict]) -> dict:
    result = load_result()
    prices = _latest_prices(items[:20])
    result["invested"] = _refresh_invested(
        items[:20], [], result.get("recommendations", []), prices
    )
    _write_result(result)
    return result


def _latest_prices(items: list[dict]) -> dict[str, float]:
    keys = sorted({str(item.get("instrumentKey") or "").strip() for item in items if item.get("instrumentKey")})
    if not keys:
        return {}
    try:
        payload = _get_json(
            MARKET_LTP_URL,
            headers=upstox_headers(),
            params={"instrument_key": ",".join(keys)},
            timeout=20,
        )
    except Exception:
        return {}
    prices = {}
    for quote in (payload.get("data") or {}).values():
        if not isinstance(quote, dict):
            continue
        key = str(quote.get("instrument_token") or quote.get("instrument_key") or "")
        price = _number(quote.get("last_price") or quote.get("ltp"))
        if key and price > 0:
            prices[key] = price
    return prices


def _refresh_invested(
    items: list[dict],
    universe: list[dict],
    recommendations: list[dict],
    prices: dict[str, float] | None = None,
) -> list[dict]:
    """Refresh prices while preserving the levels accepted at entry."""
    by_symbol = {row.get("symbol"): row for row in universe}
    by_symbol.update({row.get("symbol"): row for row in recommendations})
    prices = prices or {}
    refreshed = []
    for item in items:
        row = dict(item)
        candidate = by_symbol.get(row.get("symbol"))
        if candidate:
            if not row.get("instrumentKey"):
                row["instrumentKey"] = candidate.get("instrumentKey")
            if not row.get("originalTargetPrice"):
                row["originalTargetPrice"] = row.get("targetPrice") or candidate.get("targetPrice")
            if not row.get("originalStopLossPrice"):
                row["originalStopLossPrice"] = row.get("stopLossPrice") or candidate.get("stopLossPrice")
            row["signalSetupStrength"] = row.get("signalSetupStrength") or candidate.get("setupStrength")
            row["currentSetupStrength"] = candidate.get("setupStrength")
            row["trend"] = candidate.get("trend", row.get("trend", "NEUTRAL"))
            row["rsi14"] = candidate.get("rsi14", row.get("rsi14"))
        row["targetPrice"] = row.get("originalTargetPrice") or row.get("targetPrice")
        row["stopLossPrice"] = row.get("originalStopLossPrice") or row.get("stopLossPrice")
        instrument_key = str(row.get("instrumentKey") or "")
        live_price = prices.get(instrument_key)
        if live_price:
            row["lastPrice"] = round(live_price, 2)
            row["priceAsOf"] = datetime.now(IST).isoformat()
        elif candidate and candidate.get("lastPrice"):
            row["lastPrice"] = candidate.get("lastPrice")
        last = _number(row.get("lastPrice"))
        stop = _number(row.get("stopLossPrice"))
        target = _number(row.get("targetPrice"))
        entry = _number(row.get("entryPrice"))
        quantity = max(1, int(_number(row.get("quantity"), 1)))
        if last > 0 and entry > 0:
            row["unrealizedPnl"] = round((last - entry) * quantity, 2)
            row["unrealizedPercent"] = round((last / entry - 1) * 100, 2)
        if last > 0 and stop > 0 and last <= stop:
            row["status"] = "STOP REACHED"
        elif last > 0 and target > 0 and last >= target:
            row["status"] = "TARGET REACHED"
        elif last > 0:
            row["status"] = "ACTIVE"
        else:
            row["status"] = "PRICE UNAVAILABLE"
        refreshed.append(row)
    return refreshed
