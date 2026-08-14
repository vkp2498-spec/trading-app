"""Intraday participation breadth for the current BSE SENSEX constituents."""

import gzip
import json
import math
import os


UPSTOX_FULL_QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"

# Source: BSE Indices' SENSEX constituent list (index code 16), refreshed for
# the June 2026 reconstitution.  NSE equity keys are deliberately used for
# the dual-listed constituents because Upstox supplies one consistent quote
# shape for the complete basket.
SENSEX_SYMBOLS = {
    "ADANIPORTS", "ASIANPAINT", "AXISBANK", "BAJFINANCE", "BAJAJFINSV",
    "BEL", "BHARTIARTL", "ETERNAL", "HCLTECH", "HDFCBANK", "HINDUNILVR",
    "ICICIBANK", "INDIGO", "INFY", "ITC", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "NTPC", "POWERGRID", "RELIANCE", "SBIN", "SUNPHARMA",
    "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO",
}


def _number(value, default=0.0):
    try:
        result = float(value) if value is not None else default
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _equity_keys(instrument_cache):
    with gzip.open(instrument_cache, "rt", encoding="utf-8") as handle:
        rows = json.load(handle)
    keys = {}
    for row in rows:
        if str(row.get("segment") or "").upper() != "NSE_EQ":
            continue
        symbol = str(row.get("trading_symbol") or row.get("short_name") or "").upper()
        if symbol in SENSEX_SYMBOLS and row.get("instrument_key"):
            keys[symbol] = row["instrument_key"]
    return keys


def _quote_rows(payload):
    data = (payload or {}).get("data", {}) or {}
    values = data.values() if isinstance(data, dict) else data
    rows = {}
    for quote in values or []:
        key = quote.get("instrument_token") or quote.get("instrument_key")
        if key:
            rows[key] = quote
    for key, quote in (data.items() if isinstance(data, dict) else []):
        if isinstance(quote, dict):
            rows.setdefault(key, quote)
    return rows


def _prices(quote):
    quote = quote or {}
    last = _number(quote.get("last_price") or quote.get("ltp"))
    previous = _number(
        (quote.get("ohlc") or {}).get("close")
        or quote.get("close_price")
        or quote.get("cp")
    )
    return last, previous


def analyze_sensex_breadth(quote_payload, instrument_keys):
    quotes = _quote_rows(quote_payload)
    rows = []
    for symbol, key in instrument_keys.items():
        last, previous = _prices(quotes.get(key, {}))
        if last <= 0 or previous <= 0:
            continue
        change = (last - previous) / previous * 100
        rows.append({"symbol": symbol, "change_percent": round(change, 3)})

    coverage = len(rows)
    minimum = max(int(_number(os.getenv("SENSEX_BREADTH_MIN_COVERAGE"), 20)), 1)
    if coverage < minimum:
        return {
            "bias": "NEUTRAL",
            "confidence": "LOW",
            "score": 0.0,
            "coverage": coverage,
            "constituents": rows,
            "reasons": [
                f"Only {coverage} SENSEX constituent quotes were available; "
                f"at least {minimum} are required"
            ],
        }

    full_move = max(
        _number(os.getenv("SENSEX_BREADTH_FULL_MOVE_PERCENT"), 0.50), 0.10
    )
    normalized_move = sum(
        max(-1.0, min(1.0, row["change_percent"] / full_move)) for row in rows
    ) / coverage
    advances = sum(1 for row in rows if row["change_percent"] > 0)
    declines = sum(1 for row in rows if row["change_percent"] < 0)
    participation = (advances - declines) / coverage
    score = round((0.60 * normalized_move + 0.40 * participation) * 100, 1)
    threshold = max(
        _number(os.getenv("SENSEX_BREADTH_DIRECTION_THRESHOLD"), 18.0), 5.0
    )
    bias = (
        "BULLISH" if score >= threshold
        else "BEARISH" if score <= -threshold
        else "NEUTRAL"
    )
    confidence = "HIGH" if abs(score) >= 45 else "MEDIUM" if abs(score) >= 25 else "LOW"
    leaders = sorted(rows, key=lambda row: abs(row["change_percent"]), reverse=True)
    return {
        "bias": bias,
        "confidence": confidence,
        "score": score,
        "coverage": coverage,
        "advances": advances,
        "declines": declines,
        "constituents": rows,
        "reasons": [
            f"SENSEX breadth={score:+.1f}; advances={advances}, declines={declines}",
            "Largest moves: "
            + ", ".join(
                f"{row['symbol']} {row['change_percent']:+.2f}%" for row in leaders[:4]
            ),
        ],
    }


def get_sensex_breadth(instrument_cache, request_func):
    keys = _equity_keys(instrument_cache)
    if not keys:
        return analyze_sensex_breadth({}, {})
    payload = request_func(
        "GET",
        UPSTOX_FULL_QUOTE_URL,
        params={"instrument_key": ",".join(keys.values())},
    )
    return analyze_sensex_breadth(payload, keys)
