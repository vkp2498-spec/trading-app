"""Best-effort wrapper for Upstox market-information APIs.

These endpoints are read-only context. A failure here must never prevent the
existing option-chain strategy from running with its REST fallback.
"""

import json
import os
import time
from pathlib import Path

import requests

from strategy_core import INDEX_CONFIG, now_ist


BASE_URL = "https://api.upstox.com/v2/market"
DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_TTL_SECONDS = int(os.getenv("MARKET_INFORMATION_CACHE_SECONDS", "300"))


def _headers():
    token = os.getenv("UPSTOX_ANALYTICS_TOKEN") or os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("No Upstox token available for market information")
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def _request(path, params):
    response = requests.get(
        f"{BASE_URL}/{path}",
        headers=_headers(),
        params=params,
        timeout=20,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Upstox market/{path} failed {response.status_code}: {response.text[:300]}")
    return response.json().get("data") or {}


def _cache_path(symbol):
    DATA_DIR.mkdir(exist_ok=True)
    return DATA_DIR / f"market_information_{symbol.lower()}.json"


def _read_cache(symbol):
    path = _cache_path(symbol)
    try:
        payload = json.loads(path.read_text())
        if time.time() - float(payload.get("saved_epoch", 0)) <= CACHE_TTL_SECONDS:
            return payload.get("data") or {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _write_cache(symbol, data):
    _cache_path(symbol).write_text(
        json.dumps({"saved_epoch": time.time(), "data": data}, indent=2, default=str)
    )


def _latest_row(value):
    if isinstance(value, list):
        return value[-1] if value else {}
    return value if isinstance(value, dict) else {}


def _flow_score(row):
    buy = float(row.get("buy_amount") or 0)
    sell = float(row.get("sell_amount") or 0)
    total = buy + sell
    return round((buy - sell) / total * 100 if total else 0, 2)


def get_market_information(symbol, expiry, date_text=None):
    """Return FII/DII/OI/change-OI/max-pain/PCR context for one index.

    Missing endpoints are represented in ``errors`` and do not raise after the
    initial token check. This makes the strategy safe during weekends,
    holidays, token rotations, or temporary rate limiting.
    """
    cached = _read_cache(symbol)
    if cached:
        return cached

    config = INDEX_CONFIG[symbol]
    instrument_key = config["instrument_key"]
    date_text = date_text or now_ist().date().isoformat()
    result = {
        "symbol": symbol,
        "instrument_key": instrument_key,
        "expiry": expiry,
        "date": date_text,
        "timestamp": now_ist().isoformat(),
        "fii": {},
        "dii": {},
        "oi": {},
        "change_oi": {},
        "max_pain": {},
        "pcr": {},
        "errors": [],
    }

    requests_to_make = [
        (
            "fii",
            "fii",
            [
                ("data_type", "NSE_FO|INDEX_FUTURES"),
                ("data_type", "NSE_FO|INDEX_OPTIONS"),
                ("interval", "1D"),
            ],
        ),
        ("dii", "dii", {"data_type": "NSE_EQ|CASH", "interval": "1D"}),
        (
            "oi",
            "oi",
            {"instrument_key": instrument_key, "expiry": expiry, "date": date_text},
        ),
        (
            "change_oi",
            "change-oi",
            {
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": date_text,
                "interval": int(os.getenv("MARKET_CHANGE_OI_INTERVAL_DAYS", "1")),
            },
        ),
        (
            "max_pain",
            "max-pain",
            {
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": date_text,
                "bucket_interval": int(os.getenv("MARKET_INFO_BUCKET_MINUTES", "60")),
            },
        ),
        (
            "pcr",
            "pcr",
            {
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": date_text,
                "bucket_interval": int(os.getenv("MARKET_INFO_BUCKET_MINUTES", "60")),
            },
        ),
    ]

    for key, path, params in requests_to_make:
        try:
            result[key] = _request(path, params)
        except Exception as error:
            result["errors"].append(f"{key}: {error}")

    fii_data = result.get("fii") or {}
    fii_rows = fii_data.get("NSE_FO|INDEX_FUTURES", []) if isinstance(fii_data, dict) else []
    fii_options = fii_data.get("NSE_FO|INDEX_OPTIONS", []) if isinstance(fii_data, dict) else []
    dii_data = result.get("dii") or {}
    dii_rows = dii_data.get("NSE_EQ|CASH", []) if isinstance(dii_data, dict) else []
    result["summary"] = {
        "fii_futures": _latest_row(fii_rows),
        "fii_options": _latest_row(fii_options),
        "dii_cash": _latest_row(dii_rows),
        "fii_futures_score": _flow_score(_latest_row(fii_rows)),
        "dii_cash_score": _flow_score(_latest_row(dii_rows)),
        "oi_pcr": (
            float((result.get("oi") or {}).get("total_puts"))
            / float((result.get("oi") or {}).get("total_calls"))
            if (result.get("oi") or {}).get("total_calls")
            else None
        ),
        "reported_pcr": (result.get("pcr") or {}).get("pcr"),
        "max_pain": (result.get("max_pain") or {}).get("max_pain"),
    }
    _write_cache(symbol, result)
    return result

