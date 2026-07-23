"""Intraday participation breadth for NIFTY 50 constituents."""

import gzip
import json
import math
import os

from stock_futures_scanner import NIFTY50_SYMBOLS


UPSTOX_FULL_QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
DEFAULT_HEAVYWEIGHTS = {
    "RELIANCE": 20.0,
    "HDFCBANK": 18.0,
    "BHARTIARTL": 14.0,
    "ICICIBANK": 13.0,
    "INFY": 12.0,
    "TCS": 9.0,
    "ITC": 7.0,
    "LT": 7.0,
}


def _number(value, default=0.0):
    try:
        result = float(value) if value is not None else default
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def configured_heavyweights():
    raw = os.getenv("NIFTY_BREADTH_HEAVYWEIGHTS", "").strip()
    if not raw:
        return dict(DEFAULT_HEAVYWEIGHTS)
    parsed = {}
    for item in raw.split(","):
        symbol, separator, weight = item.partition(":")
        if separator and _number(weight) > 0:
            parsed[symbol.strip().upper()] = _number(weight)
    return parsed or dict(DEFAULT_HEAVYWEIGHTS)


def _equity_keys(instrument_cache, symbols):
    with gzip.open(instrument_cache, "rt", encoding="utf-8") as handle:
        rows = json.load(handle)
    keys = {}
    for row in rows:
        if str(row.get("segment") or "").upper() != "NSE_EQ":
            continue
        symbol = str(row.get("trading_symbol") or row.get("short_name") or "").upper()
        if symbol in symbols and row.get("instrument_key"):
            keys[symbol] = row["instrument_key"]
    return keys


def _quote_rows(payload):
    rows = {}
    data = (payload or {}).get("data", {}) or {}
    for response_key, quote in (data.items() if isinstance(data, dict) else []):
        if not isinstance(quote, dict):
            continue
        key = quote.get("instrument_token") or quote.get("instrument_key")
        key = key or str(response_key).replace(":", "|", 1)
        rows[key] = quote
    return rows


def _prices(quote):
    quote = quote or {}
    last = _number(
        quote.get("last_price")
        or quote.get("ltp")
        or (quote.get("market_data") or {}).get("ltp")
    )
    previous = 0.0
    if quote.get("net_change") is not None and last > 0:
        previous = last - _number(quote.get("net_change"))
    if previous <= 0:
        previous = _number(
            (quote.get("ohlc") or {}).get("close")
            or quote.get("close_price")
            or quote.get("cp")
        )
    return last, previous


def analyze_nifty_breadth(quote_payload, instrument_keys, heavyweights=None):
    heavyweights = heavyweights or configured_heavyweights()
    quotes = _quote_rows(quote_payload)
    rows = []
    for symbol, key in instrument_keys.items():
        last, previous = _prices(quotes.get(key, {}))
        if last <= 0 or previous <= 0:
            continue
        change = (last - previous) / previous * 100
        rows.append({"symbol": symbol, "change_percent": round(change, 3)})

    coverage = len(rows)
    if coverage < 30:
        return {
            "bias": "NEUTRAL",
            "confidence": "LOW",
            "score": 0.0,
            "coverage": coverage,
            "constituents": rows,
            "reasons": ["Fewer than 30 NIFTY constituent quotes were available"],
        }

    full_move = max(_number(os.getenv("NIFTY_BREADTH_FULL_MOVE_PERCENT"), 0.50), 0.10)
    equal_move = sum(max(-1.0, min(1.0, row["change_percent"] / full_move)) for row in rows) / coverage
    advances = sum(1 for row in rows if row["change_percent"] > 0)
    declines = sum(1 for row in rows if row["change_percent"] < 0)
    participation = (advances - declines) / coverage
    weighted_rows = [row for row in rows if row["symbol"] in heavyweights]
    total_weight = sum(heavyweights[row["symbol"]] for row in weighted_rows)
    heavyweight_move = (
        sum(
            heavyweights[row["symbol"]]
            * max(-1.0, min(1.0, row["change_percent"] / full_move))
            for row in weighted_rows
        )
        / total_weight
        if total_weight > 0
        else 0.0
    )
    score = round((0.45 * equal_move + 0.35 * participation + 0.20 * heavyweight_move) * 100, 1)
    threshold = max(_number(os.getenv("NIFTY_BREADTH_DIRECTION_THRESHOLD"), 18.0), 5.0)
    bias = "BULLISH" if score >= threshold else "BEARISH" if score <= -threshold else "NEUTRAL"
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
            f"NIFTY breadth={score:+.1f}; advances={advances}, declines={declines}",
            "Largest moves: "
            + ", ".join(f"{row['symbol']} {row['change_percent']:+.2f}%" for row in leaders[:4]),
        ],
    }


def get_nifty_breadth(instrument_cache, request_func):
    symbols = set(NIFTY50_SYMBOLS)
    keys = _equity_keys(instrument_cache, symbols)
    if not keys:
        return analyze_nifty_breadth({}, {})
    payload = request_func(
        "GET",
        UPSTOX_FULL_QUOTE_URL,
        params={"instrument_key": ",".join(keys.values())},
    )
    return analyze_nifty_breadth(payload, keys)
