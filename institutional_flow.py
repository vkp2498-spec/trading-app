import csv
import gzip
import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path

import requests

from strategy_core import now_ist
from market_information import get_market_information


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_FILE = DATA_DIR / "institutional_flow.csv"
INSTRUMENT_CACHE = BASE_DIR / "upstox_complete.json.gz"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
FULL_QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
FII_URL = "https://api.upstox.com/v2/market/fii"
VIX_KEY = "NSE_INDEX|India VIX"

INDEX_CONFIG = {
    "NIFTY": {
        "spot_key": "NSE_INDEX|Nifty 50",
        "underlying_candidates": {"NIFTY"},
    },
    "BANKNIFTY": {
        "spot_key": "NSE_INDEX|Nifty Bank",
        "underlying_candidates": {"BANKNIFTY", "NIFTY BANK"},
    },
}

COLUMNS = [
    "timestamp",
    "symbol",
    "future_key",
    "future_expiry",
    "future_price",
    "future_oi",
    "spot_price",
    "basis",
    "vix",
    "nearby_ce_change_oi",
    "nearby_pe_change_oi",
    "nearby_flow_ratio",
    "futures_component",
    "options_component",
    "basis_component",
    "atm_component",
    "vix_component",
    "fii_component",
    "market_info_component",
    "base_score",
    "persistence_component",
    "score",
    "bias",
    "confidence",
]


def to_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def headers():
    token = os.getenv("UPSTOX_ANALYTICS_TOKEN") or os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("No Upstox token available for institutional-flow analysis")
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def ensure_snapshot_file():
    DATA_DIR.mkdir(exist_ok=True)
    if not SNAPSHOT_FILE.exists():
        with SNAPSHOT_FILE.open("w", newline="") as file:
            csv.DictWriter(file, fieldnames=COLUMNS).writeheader()
        return

    with SNAPSHOT_FILE.open("r", newline="") as file:
        reader = csv.DictReader(file)
        existing_fields = reader.fieldnames or []
        rows = list(reader)

    if existing_fields != COLUMNS:
        migrated = [{column: row.get(column, "") for column in COLUMNS} for row in rows]
        temporary = SNAPSHOT_FILE.with_suffix(".tmp")
        with temporary.open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(migrated)
        temporary.replace(SNAPSHOT_FILE)


def parse_expiry(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, now_ist().tzinfo).date()
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def ensure_instrument_cache():
    if not INSTRUMENT_CACHE.exists():
        urllib.request.urlretrieve(INSTRUMENTS_URL, INSTRUMENT_CACHE)


def nearest_index_future(symbol):
    ensure_instrument_cache()
    candidates = INDEX_CONFIG[symbol]["underlying_candidates"]
    today = now_ist().date()
    matches = []

    with gzip.open(INSTRUMENT_CACHE, "rt", encoding="utf-8") as file:
        instruments = json.load(file)

    for instrument in instruments:
        if instrument.get("segment") != "NSE_FO":
            continue
        if str(instrument.get("instrument_type", "")).upper() not in {"FUT", "FUTIDX"}:
            continue
        underlying = str(instrument.get("underlying_symbol", "")).upper()
        if underlying not in candidates:
            continue
        expiry = parse_expiry(instrument.get("expiry"))
        if expiry is None or expiry < today:
            continue
        matches.append((expiry, instrument))

    if not matches:
        raise RuntimeError(f"No current index future found for {symbol}")

    expiry, instrument = min(matches, key=lambda item: item[0])
    return {
        "instrument_key": instrument.get("instrument_key"),
        "trading_symbol": instrument.get("trading_symbol"),
        "expiry": expiry.isoformat(),
    }


def fetch_quotes(instrument_keys):
    response = requests.get(
        FULL_QUOTE_URL,
        headers=headers(),
        params={"instrument_key": ",".join(instrument_keys)},
        timeout=30,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Upstox full quote failed {response.status_code}: {response.text[:400]}")

    data = response.json().get("data", {}) or {}
    values = list(data.values()) if isinstance(data, dict) else data
    result = {}
    for quote in values or []:
        key = quote.get("instrument_token") or quote.get("instrument_key")
        if key:
            result[key] = quote

    for requested_key in instrument_keys:
        if requested_key in result:
            continue
        direct = data.get(requested_key) if isinstance(data, dict) else None
        if isinstance(direct, dict):
            result[requested_key] = direct
            continue
        for quote in values or []:
            if quote.get("instrument_token") == requested_key:
                result[requested_key] = quote
                break

    return result


def read_today_rows(symbol, future_key):
    ensure_snapshot_file()
    today = now_ist().date()
    rows = []
    with SNAPSHOT_FILE.open("r", newline="") as file:
        for row in csv.DictReader(file):
            if row.get("symbol") != symbol or row.get("future_key") != future_key:
                continue
            try:
                timestamp = datetime.fromisoformat(row.get("timestamp", ""))
            except ValueError:
                continue
            if timestamp.date() == today:
                rows.append(row)
    return rows


def directional_component(current_price, current_oi, previous_price, previous_oi):
    if None in {current_price, current_oi, previous_price, previous_oi}:
        return 0.0, "Not enough futures snapshots"
    if previous_price <= 0 or previous_oi <= 0:
        return 0.0, "Invalid previous futures snapshot"

    price_change = (current_price - previous_price) / previous_price * 100
    oi_change = (current_oi - previous_oi) / previous_oi * 100

    price_active = abs(price_change) >= 0.02
    oi_active = abs(oi_change) >= 0.05
    if not price_active and not oi_active:
        return 0.0, f"Futures price/OI flat: price={price_change:.3f}%, oi={oi_change:.3f}%"

    if price_change > 0 and oi_change > 0:
        return 30.0, f"Futures long buildup: price={price_change:.3f}%, oi={oi_change:.3f}%"
    if price_change < 0 and oi_change > 0:
        return -30.0, f"Futures short buildup: price={price_change:.3f}%, oi={oi_change:.3f}%"
    if price_change > 0 and oi_change < 0:
        return 20.0, f"Futures short covering: price={price_change:.3f}%, oi={oi_change:.3f}%"
    if price_change < 0 and oi_change < 0:
        return -20.0, f"Futures long unwinding: price={price_change:.3f}%, oi={oi_change:.3f}%"

    return (10.0 if price_change > 0 else -10.0), (
        f"Futures directional price move without decisive OI: price={price_change:.3f}%, oi={oi_change:.3f}%"
    )


def options_component(nearby_flow):
    ce_change = to_float(nearby_flow.get("ce_change_oi"), 0.0)
    pe_change = to_float(nearby_flow.get("pe_change_oi"), 0.0)
    denominator = abs(ce_change) + abs(pe_change)
    if denominator <= 0:
        return 0.0, 0.0, "Nearby option OI change unavailable"

    ratio = clamp((pe_change - ce_change) / denominator, -1.0, 1.0)
    component = round(25.0 * ratio, 2)
    if component > 3:
        reason = f"Nearby strikes favor put-side OI flow: ratio={ratio:.3f}"
    elif component < -3:
        reason = f"Nearby strikes favor call-side OI flow: ratio={ratio:.3f}"
    else:
        reason = f"Nearby strike OI flow is balanced: ratio={ratio:.3f}"
    return component, ratio, reason


def basis_component(current_basis, previous_basis, spot_price):
    if current_basis is None or previous_basis is None or not spot_price:
        return 0.0, "Not enough futures-basis history"
    change_bps = (current_basis - previous_basis) / spot_price * 10000
    component = clamp(change_bps * 1.5, -10.0, 10.0)
    return round(component, 2), f"Futures basis change={change_bps:.2f} bps"


def atm_component(atm_flow, underlying_direction):
    bias = atm_flow.get("bias")
    confirmed = bool(atm_flow.get("volume_confirmed"))

    if underlying_direction not in {"BULLISH", "BEARISH"}:
        return 0.0, "Selected option premium has no directional underlying signal"

    direction_sign = 1.0 if underlying_direction == "BULLISH" else -1.0
    option_type = "CE" if underlying_direction == "BULLISH" else "PE"

    if bias == "BULLISH" and confirmed:
        return (
            10.0 * direction_sign,
            f"Selected {option_type} premium strengthens the {underlying_direction.lower()} view with confirmed volume",
        )
    if bias == "BULLISH":
        return (
            6.0 * direction_sign,
            f"Selected {option_type} premium strengthens the {underlying_direction.lower()} view without volume confirmation",
        )
    if bias == "BEARISH":
        return (
            -10.0 * direction_sign,
            f"Selected {option_type} premium is weakening against the {underlying_direction.lower()} view",
        )
    return 0.0, "Selected option premium flow is neutral"


def vix_component(current_vix, previous_vix):
    if current_vix is None or previous_vix is None or previous_vix <= 0:
        return 0.0, "Not enough India VIX history"
    change = (current_vix - previous_vix) / previous_vix * 100
    component = clamp(-change * 2.0, -5.0, 5.0)
    return round(component, 2), f"India VIX change={change:.3f}%"


def load_fii_context():
    cache_file = DATA_DIR / f"fii_context_{now_ist().date().isoformat()}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    response = requests.get(
        FII_URL,
        headers=headers(),
        params=[
            ("data_type", "NSE_FO|INDEX_FUTURES"),
            ("data_type", "NSE_FO|INDEX_OPTIONS"),
            ("interval", "1D"),
        ],
        timeout=30,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Upstox FII API failed {response.status_code}: {response.text[:400]}")

    data = response.json().get("data", {}) or {}
    futures_rows = data.get("NSE_FO|INDEX_FUTURES", []) or []
    options_rows = data.get("NSE_FO|INDEX_OPTIONS", []) or []
    futures = max(futures_rows, key=lambda row: row.get("time_stamp", 0)) if futures_rows else {}
    options = max(options_rows, key=lambda row: row.get("time_stamp", 0)) if options_rows else {}

    futures_long = to_float(futures.get("total_long_contracts"), 0.0)
    futures_short = to_float(futures.get("total_short_contracts"), 0.0)
    futures_total = futures_long + futures_short
    futures_ratio = (futures_long - futures_short) / futures_total if futures_total else 0.0

    option_bullish = to_float(options.get("total_call_long_contracts"), 0.0) + to_float(
        options.get("total_put_short_contracts"), 0.0
    )
    option_bearish = to_float(options.get("total_put_long_contracts"), 0.0) + to_float(
        options.get("total_call_short_contracts"), 0.0
    )
    option_total = option_bullish + option_bearish
    options_ratio = (option_bullish - option_bearish) / option_total if option_total else 0.0

    combined_ratio = clamp((futures_ratio + options_ratio) / 2, -1.0, 1.0)
    result = {
        "score": round(combined_ratio * 5.0, 2),
        "futures_net_ratio": round(futures_ratio, 4),
        "options_net_ratio": round(options_ratio, 4),
        "timestamp": futures.get("time_stamp") or options.get("time_stamp"),
        "reason": "Daily FII positioning is context only, not an intraday trigger",
    }
    DATA_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(result, indent=2, sort_keys=True))
    return result


def persistence_component(previous_rows, current_base_score):
    recent_scores = [to_float(row.get("base_score"), 0.0) for row in previous_rows[-2:]]
    recent_scores.append(current_base_score)
    if len(recent_scores) < 3:
        return 0.0, "Institutional persistence needs three snapshots"
    if all(score >= 25 for score in recent_scores):
        return 15.0, "Bullish institutional footprint persisted for three snapshots"
    if all(score <= -25 for score in recent_scores):
        return -15.0, "Bearish institutional footprint persisted for three snapshots"
    return 0.0, "Institutional footprint is not persistent"


def append_snapshot(row):
    ensure_snapshot_file()
    with SNAPSHOT_FILE.open("a", newline="") as file:
        csv.DictWriter(file, fieldnames=COLUMNS).writerow(
            {column: row.get(column, "") for column in COLUMNS}
        )


def get_institutional_footprint(symbol, recommendation, atm_flow):
    reasons = []
    future = nearest_index_future(symbol)
    spot_key = INDEX_CONFIG[symbol]["spot_key"]
    quotes = fetch_quotes([future["instrument_key"], spot_key, VIX_KEY])

    future_quote = quotes.get(future["instrument_key"], {}) or {}
    spot_quote = quotes.get(spot_key, {}) or {}
    vix_quote = quotes.get(VIX_KEY, {}) or {}

    future_price = to_float(future_quote.get("last_price"))
    future_oi = to_float(future_quote.get("oi"))
    spot_price = to_float(spot_quote.get("last_price")) or to_float(
        (recommendation.get("atm", {}) or {}).get("spot")
    )
    current_vix = to_float(vix_quote.get("last_price"))
    current_basis = future_price - spot_price if future_price is not None and spot_price is not None else None

    previous_rows = read_today_rows(symbol, future["instrument_key"])
    previous = previous_rows[-1] if previous_rows else {}
    previous_future_price = to_float(previous.get("future_price"))
    previous_future_oi = to_float(previous.get("future_oi"))
    previous_basis = to_float(previous.get("basis"))
    previous_vix = to_float(previous.get("vix"))

    futures_score, futures_reason = directional_component(
        future_price,
        future_oi,
        previous_future_price,
        previous_future_oi,
    )
    reasons.append(futures_reason)

    nearby_flow = recommendation.get("nearby_flow", {}) or {}
    option_score, nearby_ratio, option_reason = options_component(nearby_flow)
    reasons.append(option_reason)

    basis_score, basis_reason = basis_component(current_basis, previous_basis, spot_price)
    reasons.append(basis_reason)

    selected_option_score, selected_option_reason = atm_component(
        atm_flow or {},
        recommendation.get("direction"),
    )
    reasons.append(selected_option_reason)

    vix_score, vix_reason = vix_component(current_vix, previous_vix)
    reasons.append(vix_reason)

    try:
        fii = load_fii_context()
        fii_score = to_float(fii.get("score"), 0.0)
        reasons.append(
            f"Daily FII context score={fii_score:.2f}; futures_ratio={fii.get('futures_net_ratio')}, "
            f"options_ratio={fii.get('options_net_ratio')}"
        )
    except Exception as error:
        fii = {"score": 0.0, "reason": str(error)}
        fii_score = 0.0
        reasons.append(f"Daily FII context unavailable: {error}")

    try:
        market_info = get_market_information(
            symbol,
            recommendation.get("atm", {}).get("expiry"),
        )
        market_summary = market_info.get("summary", {}) or {}
        fii_market_score = to_float(market_summary.get("fii_futures_score"), 0.0)
        dii_market_score = to_float(market_summary.get("dii_cash_score"), 0.0)
        market_info_score = clamp((fii_market_score + dii_market_score) / 20.0, -5.0, 5.0)
        if market_summary.get("reported_pcr") is not None:
            reasons.append(f"Upstox market PCR={market_summary.get('reported_pcr')}")
        if market_summary.get("max_pain") is not None:
            reasons.append(f"Upstox max pain={market_summary.get('max_pain')}")
        reasons.append(
            f"Upstox FII futures flow={fii_market_score:.2f}, "
            f"DII cash flow={dii_market_score:.2f}"
        )
        for error in market_info.get("errors", [])[:2]:
            reasons.append(f"Market information partial: {error}")
    except Exception as error:
        market_info = {"errors": [str(error)], "summary": {}}
        market_info_score = 0.0
        reasons.append(f"Upstox market information unavailable: {error}")

    base_score = round(
        futures_score
        + option_score
        + basis_score
        + selected_option_score
        + vix_score
        + fii_score
        + market_info_score,
        2,
    )
    persistence_score, persistence_reason = persistence_component(previous_rows, base_score)
    reasons.append(persistence_reason)
    score = round(clamp(base_score + persistence_score, -100.0, 100.0), 2)

    bias = "BULLISH" if score >= 30 else "BEARISH" if score <= -30 else "NEUTRAL"
    confidence = "HIGH" if abs(score) >= 70 else "MEDIUM" if abs(score) >= 45 else "LOW"

    row = {
        "timestamp": now_ist().isoformat(),
        "symbol": symbol,
        "future_key": future["instrument_key"],
        "future_expiry": future["expiry"],
        "future_price": future_price,
        "future_oi": future_oi,
        "spot_price": spot_price,
        "basis": round(current_basis, 2) if current_basis is not None else None,
        "vix": current_vix,
        "nearby_ce_change_oi": nearby_flow.get("ce_change_oi"),
        "nearby_pe_change_oi": nearby_flow.get("pe_change_oi"),
        "nearby_flow_ratio": round(nearby_ratio, 4),
        "futures_component": futures_score,
        "options_component": option_score,
        "basis_component": basis_score,
        "atm_component": selected_option_score,
        "vix_component": vix_score,
        "fii_component": fii_score,
        "market_info_component": market_info_score,
        "base_score": base_score,
        "persistence_component": persistence_score,
        "score": score,
        "bias": bias,
        "confidence": confidence,
    }
    append_snapshot(row)

    return {
        **row,
        "trading_symbol": future.get("trading_symbol"),
        "aligns_with_option_signal": (
            bias == "NEUTRAL" or recommendation.get("direction") == bias
        ),
        "reasons": reasons,
        "fii_context": fii,
        "market_information": market_info,
    }


def neutral_institutional_footprint(reason):
    return {
        "score": 0.0,
        "bias": "NEUTRAL",
        "confidence": "LOW",
        "aligns_with_option_signal": True,
        "reasons": [reason],
    }
