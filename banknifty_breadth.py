"""Weighted intraday breadth for the largest BANKNIFTY constituents."""

import gzip
import json
import math
import os


UPSTOX_FULL_QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
DEFAULT_WEIGHTS = {
    "HDFCBANK": 30.0,
    "ICICIBANK": 25.0,
    "SBIN": 15.0,
    "KOTAKBANK": 15.0,
    "AXISBANK": 15.0,
}


def _number(value, default=0.0):
    try:
        result = float(value) if value is not None else default
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def configured_weights():
    text = os.getenv("BANKNIFTY_BREADTH_WEIGHTS", "").strip()
    if not text:
        return dict(DEFAULT_WEIGHTS)
    parsed = {}
    for item in text.split(","):
        symbol, separator, weight = item.partition(":")
        if separator and _number(weight) > 0:
            parsed[symbol.strip().upper()] = _number(weight)
    return parsed or dict(DEFAULT_WEIGHTS)


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


def _last_price(quote):
    quote = quote or {}
    return _number(
        quote.get("last_price")
        or quote.get("ltp")
        or (quote.get("market_data") or {}).get("ltp")
    )


def _previous_close(quote):
    quote = quote or {}
    last = _last_price(quote)
    if quote.get("net_change") is not None and last > 0:
        previous = last - _number(quote.get("net_change"))
        if previous > 0:
            return previous
    return _number(
        (quote.get("ohlc") or {}).get("close")
        or quote.get("close_price")
        or quote.get("cp")
    )


def analyze_banknifty_breadth(quote_payload, instrument_keys, weights=None):
    weights = weights or configured_weights()
    quotes = _quote_rows(quote_payload)
    rows = []
    for symbol, weight in weights.items():
        key = instrument_keys.get(symbol)
        quote = quotes.get(key, {})
        last = _last_price(quote)
        previous = _previous_close(quote)
        if not key or last <= 0 or previous <= 0:
            continue
        change_percent = (last - previous) / previous * 100
        rows.append(
            {
                "symbol": symbol,
                "weight": float(weight),
                "change_percent": round(change_percent, 3),
            }
        )

    if len(rows) < 3:
        return {
            "bias": "NEUTRAL",
            "confidence": "LOW",
            "score": 0.0,
            "coverage": len(rows),
            "constituents": rows,
            "reasons": ["Fewer than three major-bank quotes were available"],
        }

    total_weight = sum(row["weight"] for row in rows)
    move_for_full_score = max(_number(os.getenv("BANKNIFTY_BREADTH_FULL_MOVE_PERCENT"), 0.50), 0.10)
    net = sum(
        row["weight"]
        * max(-1.0, min(1.0, row["change_percent"] / move_for_full_score))
        for row in rows
    ) / total_weight
    score = round(net * 100, 1)
    absolute = abs(score)
    threshold = max(_number(os.getenv("BANKNIFTY_BREADTH_DIRECTION_THRESHOLD"), 18.0), 5.0)
    bias = "BULLISH" if score >= threshold else "BEARISH" if score <= -threshold else "NEUTRAL"
    confidence = "HIGH" if absolute >= 45 else "MEDIUM" if absolute >= 25 else "LOW"
    leaders = sorted(rows, key=lambda row: abs(row["weight"] * row["change_percent"]), reverse=True)
    return {
        "bias": bias,
        "confidence": confidence,
        "score": score,
        "coverage": len(rows),
        "constituents": rows,
        "reasons": [
            f"Weighted major-bank breadth={score:+.1f}",
            "Largest contributors: "
            + ", ".join(
                f"{row['symbol']} {row['change_percent']:+.2f}%" for row in leaders[:3]
            ),
        ],
    }


def get_banknifty_breadth(instrument_cache, request_func):
    weights = configured_weights()
    keys = _equity_keys(instrument_cache, set(weights))
    if not keys:
        return analyze_banknifty_breadth({}, {}, weights)
    payload = request_func(
        "GET",
        UPSTOX_FULL_QUOTE_URL,
        params={"instrument_key": ",".join(keys.values())},
    )
    return analyze_banknifty_breadth(payload, keys, weights)
