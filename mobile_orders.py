"""Explicitly gated live delivery-order operations for the private mobile app."""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
UPSTOX_BASE = "https://api.upstox.com"
UPSTOX_QUOTE_URL = f"{UPSTOX_BASE}/v2/market-quote/quotes"
UPSTOX_HOLDINGS_URL = f"{UPSTOX_BASE}/v2/portfolio/long-term-holdings"
UPSTOX_ORDER_URL = "https://api-hft.upstox.com/v3/order/place"


def _token() -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN is not configured")
    return token


def live_orders_enabled() -> bool:
    return (
        os.getenv("MOBILE_LIVE_ORDERS_ENABLED", "false").lower() == "true"
        and os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
    )


def _headers() -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {_token()}"}


def _request(method: str, url: str, **kwargs):
    response = requests.request(method, url, headers=_headers(), timeout=20, **kwargs)
    if response.status_code >= 300:
        raise RuntimeError(f"Upstox request failed with status {response.status_code}")
    return response.json()


def _quote_price(instrument_key: str) -> float:
    payload = _request("GET", UPSTOX_QUOTE_URL, params={"instrument_key": instrument_key})
    rows = (payload.get("data") or {}) if isinstance(payload, dict) else {}
    quote = rows.get(instrument_key) if isinstance(rows, dict) else None
    if not quote and isinstance(rows, dict) and rows:
        quote = next(iter(rows.values()))
    quote = quote or {}
    market_data = quote.get("market_data") or {}
    price = quote.get("last_price") or quote.get("ltp") or market_data.get("ltp")
    price = float(price or 0)
    if price <= 0:
        raise RuntimeError("Upstox returned no current market price")
    return price


def holdings() -> list[dict]:
    payload = _request("GET", UPSTOX_HOLDINGS_URL)
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    result = []
    for row in rows if isinstance(rows, list) else []:
        quantity = int(float(row.get("quantity") or row.get("available_quantity") or 0))
        if quantity <= 0:
            continue
        result.append({
            "instrumentKey": row.get("instrument_token") or row.get("instrument_key") or "",
            "symbol": row.get("trading_symbol") or row.get("symbol") or "",
            "quantity": quantity,
            "averagePrice": float(row.get("average_price") or 0),
            "lastPrice": float(row.get("last_price") or row.get("close_price") or 0),
            "pnl": float(row.get("pnl") or row.get("day_change") or 0),
            "isin": row.get("isin"),
        })
    return result


def _place(instrument_key: str, quantity: int, transaction_type: str, symbol: str) -> dict:
    tag = f"hk_mobile_{symbol[:12].lower()}_{datetime.now(IST):%Y%m%d}"
    payload = {
        "quantity": int(quantity),
        "product": "D",
        "validity": "DAY",
        "price": 0,
        "tag": tag,
        "instrument_token": instrument_key,
        "order_type": "MARKET",
        "transaction_type": transaction_type,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
        "slice": True,
    }
    response = _request("POST", UPSTOX_ORDER_URL, json=payload)
    data = response.get("data", {}) if isinstance(response, dict) else {}
    return {"orderID": data.get("order_id") or data.get("order_id"), "quantity": quantity, "transactionType": transaction_type, "product": "D", "tag": tag}


def buy_delivery(instrument_key: str, symbol: str, maximum_amount: float) -> dict:
    if not live_orders_enabled():
        raise PermissionError("Live mobile orders are disabled")
    if maximum_amount <= 0 or maximum_amount > 100_000:
        raise ValueError("Maximum order amount must be between ₹1 and ₹1,00,000")
    price = _quote_price(instrument_key)
    quantity = int(maximum_amount // price)
    if quantity < 1:
        raise ValueError("₹1,00,000 is less than the current price of one share")
    result = _place(instrument_key, quantity, "BUY", symbol)
    result.update({"symbol": symbol, "marketPrice": price, "requestedAmount": maximum_amount})
    return result


def exit_holding(instrument_key: str, symbol: str) -> dict:
    if not live_orders_enabled():
        raise PermissionError("Live mobile orders are disabled")
    matching = next((row for row in holdings() if row["instrumentKey"] == instrument_key), None)
    if not matching:
        raise ValueError("No available delivery holding was found for this instrument")
    result = _place(instrument_key, matching["quantity"], "SELL", symbol)
    result.update({"symbol": symbol, "marketPrice": _quote_price(instrument_key)})
    return result
