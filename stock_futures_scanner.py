import gzip
import json
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from market_technicals import get_instrument_technical_analysis


IST = ZoneInfo("Asia/Kolkata")
UPSTOX_FULL_QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"

# Kept locally so a temporary NSE page failure cannot alter the trading universe.
# The instrument-file intersection below automatically ignores symbols without a
# currently listed NSE stock-futures contract.
NIFTY50_SYMBOLS = {
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC",
    "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI",
    "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE",
    "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS",
    "TMPV", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT",
    "ULTRACEMCO", "WIPRO",
}


def _float(value, default=0.0):
    try:
        result = float(value) if value is not None else default
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _int(value, default=0):
    try:
        return int(float(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def _expiry_date(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, IST).date()
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def configured_universe():
    raw = os.getenv("NIFTY50_FUTURES_SYMBOLS", "").strip()
    if not raw:
        return set(NIFTY50_SYMBOLS)
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def load_nearest_stock_futures(instrument_cache, minimum_days_to_expiry=2):
    today = datetime.now(IST).date()
    universe = configured_universe()
    with gzip.open(instrument_cache, "rt", encoding="utf-8") as handle:
        instruments = json.load(handle)

    grouped = {}
    for item in instruments:
        if item.get("segment") != "NSE_FO":
            continue
        instrument_type = str(item.get("instrument_type") or "").upper()
        if instrument_type not in {"FUT", "FUTSTK"}:
            continue
        underlying = str(item.get("underlying_symbol") or "").upper()
        if underlying not in universe:
            continue
        expiry = _expiry_date(item.get("expiry"))
        if expiry is None or (expiry - today).days < minimum_days_to_expiry:
            continue
        normalized = dict(item)
        normalized["expiry_date"] = expiry.isoformat()
        normalized["underlying_symbol"] = underlying
        grouped.setdefault(underlying, []).append(normalized)

    return [
        min(items, key=lambda row: row["expiry_date"])
        for items in grouped.values()
    ]


def _quote_rows(payload):
    data = (payload or {}).get("data", {}) or {}
    rows = {}
    iterable = data.values() if isinstance(data, dict) else data
    for quote in iterable or []:
        if not isinstance(quote, dict):
            continue
        key = quote.get("instrument_token") or quote.get("instrument_key")
        if key:
            rows[key] = quote
    return rows


def _quote_price(quote):
    return _float(
        quote.get("last_price")
        or quote.get("ltp")
        or (quote.get("market_data") or {}).get("ltp")
    )


def _previous_close(quote):
    ohlc = quote.get("ohlc") or {}
    return _float(ohlc.get("close") or quote.get("close_price"))


def _spread_percent(quote, last_price):
    depth = quote.get("depth") or quote.get("market_depth") or {}
    buys = depth.get("buy") or depth.get("bids") or []
    sells = depth.get("sell") or depth.get("asks") or []
    bid = _float((buys[0] or {}).get("price")) if buys else 0
    ask = _float((sells[0] or {}).get("price")) if sells else 0
    if bid <= 0 or ask <= 0 or last_price <= 0:
        return 0.0
    return max((ask - bid) / last_price * 100, 0)


def fetch_bulk_quotes(contracts, request_func):
    if not contracts:
        return {}
    keys = ",".join(item["instrument_key"] for item in contracts)
    payload = request_func(
        "GET",
        UPSTOX_FULL_QUOTE_URL,
        params={"instrument_key": keys},
    )
    return _quote_rows(payload)


def prefilter_contracts(contracts, quotes, limit=8):
    ranked = []
    for contract in contracts:
        quote = quotes.get(contract["instrument_key"], {})
        last_price = _quote_price(quote)
        previous_close = _previous_close(quote)
        if last_price <= 0 or previous_close <= 0:
            continue
        change_percent = (last_price - previous_close) / previous_close * 100
        volume = _float(quote.get("volume") or quote.get("volume_traded_today"))
        oi = _float(quote.get("oi"))
        spread_percent = _spread_percent(quote, last_price)
        ranked.append(
            {
                "contract": contract,
                "quote": quote,
                "last_price": last_price,
                "change_percent": change_percent,
                "volume": volume,
                "oi": oi,
                "spread_percent": spread_percent,
                "prefilter_score": abs(change_percent) * 10 + min(volume / 100000, 5),
            }
        )
    return sorted(ranked, key=lambda row: row["prefilter_score"], reverse=True)[:limit]


def _aligned_component(analysis, direction, points, neutral_points=0):
    bias = analysis.get("bias")
    if bias == direction:
        return points
    if bias == "NEUTRAL":
        return neutral_points
    return 0


def _build_levels(entry, direction, five, fifteen):
    aligned = [
        analysis
        for analysis in (five, fifteen)
        if analysis.get("bias") == direction
    ]
    targets = [_float(item.get("target")) for item in aligned]
    stops = [_float(item.get("stop_loss")) for item in aligned]
    if direction == "BULLISH":
        targets = [value for value in targets if value > entry]
        stops = [value for value in stops if 0 < value < entry]
    else:
        targets = [value for value in targets if 0 < value < entry]
        stops = [value for value in stops if value > entry]

    atr = max(_float(five.get("atr14")), entry * 0.0025)
    if direction == "BULLISH":
        target = min(targets) if targets else entry + atr * 1.5
        stop = max(stops) if stops else entry - atr
        reward, risk = target - entry, entry - stop
    else:
        target = max(targets) if targets else entry - atr * 1.5
        stop = min(stops) if stops else entry + atr
        reward, risk = entry - target, stop - entry
    return round(target, 2), round(stop, 2), reward / risk if risk > 0 else 0


def evaluate_contract(prefiltered):
    contract = prefiltered["contract"]
    technicals = get_instrument_technical_analysis(contract["instrument_key"])
    two = technicals.get("two_hour", {})
    fifteen = technicals.get("fifteen_min", {})
    five = technicals.get("five_min", {})

    bullish_votes = sum(item.get("bias") == "BULLISH" for item in (fifteen, five))
    bearish_votes = sum(item.get("bias") == "BEARISH" for item in (fifteen, five))
    direction = "BULLISH" if bullish_votes > bearish_votes else "BEARISH" if bearish_votes > bullish_votes else "NEUTRAL"
    reasons = []
    if direction == "NEUTRAL":
        return None, f"{contract['underlying_symbol']}: 5M and 15M do not establish a direction"
    if five.get("bias") != direction or fifteen.get("bias") != direction:
        return None, f"{contract['underlying_symbol']}: 5M and 15M are not aligned"
    if two.get("bias") not in {direction, "NEUTRAL"} and two.get("confidence") in {"MEDIUM", "HIGH"}:
        return None, f"{contract['underlying_symbol']}: 2H trend strongly conflicts"

    entry = _float(prefiltered.get("last_price")) or _float(five.get("close"))
    spread = _float(prefiltered.get("spread_percent"))
    max_spread = _float(os.getenv("STOCK_FUTURES_MAX_SPREAD_PERCENT"), 0.20)
    if entry <= 0:
        return None, f"{contract['underlying_symbol']}: no valid futures price"
    if spread > max_spread:
        return None, f"{contract['underlying_symbol']}: spread {spread:.2f}% exceeds {max_spread:.2f}%"

    volume_ratio = _float(five.get("volume_ratio"))
    minimum_volume = _float(os.getenv("STOCK_FUTURES_MIN_VOLUME_RATIO"), 1.20)
    score = 0.0
    score += _aligned_component(fifteen, direction, 25, 5)
    score += _aligned_component(five, direction, 25, 5)
    score += _aligned_component(two, direction, 15, 7)
    if five.get("vwap_bias") == direction:
        score += 10
        reasons.append("5M price and VWAP align")
    if volume_ratio >= minimum_volume:
        score += 10
        reasons.append(f"5M volume ratio {volume_ratio:.2f} confirms participation")
    momentum = _int(five.get("momentum_score"))
    if (direction == "BULLISH" and momentum >= 3) or (direction == "BEARISH" and momentum <= -3):
        score += 10
        reasons.append("5M momentum confirms direction")
    current_oi = _float(five.get("oi"))
    previous_oi = _float(five.get("previous_oi"))
    current_close = _float(five.get("close"))
    previous_close = _float(five.get("prev_close"))
    price_aligns = (
        direction == "BULLISH" and current_close > previous_close
    ) or (
        direction == "BEARISH" and current_close < previous_close
    )
    if current_oi > previous_oi > 0 and price_aligns:
        score += 5
        reasons.append(
            "Futures price and open-interest buildup confirm the direction"
        )
    if spread <= max_spread:
        score += 5

    target, stop, reward_risk = _build_levels(entry, direction, five, fifteen)
    minimum_rr = _float(os.getenv("STOCK_FUTURES_MIN_REWARD_RISK"), 1.20)
    if reward_risk < minimum_rr:
        return None, f"{contract['underlying_symbol']}: reward/risk {reward_risk:.2f} is below {minimum_rr:.2f}"

    minimum_score = _float(os.getenv("STOCK_FUTURES_MIN_SCORE"), 80)
    if score < minimum_score:
        return None, f"{contract['underlying_symbol']}: score {score:.1f} is below {minimum_score:.1f}"

    return {
        "symbol": "STOCK_FUTURE",
        "underlying_symbol": contract["underlying_symbol"],
        "instrument_class": "STOCK_FUTURE",
        "instrument": contract,
        "direction": direction,
        "transaction_type": "BUY" if direction == "BULLISH" else "SELL",
        "confidence": "HIGH" if score >= 90 else "MEDIUM",
        "signal_score": round(score, 1),
        "entry_price": round(entry, 2),
        "target_price": target,
        "stop_loss_price": stop,
        "reward_risk": round(reward_risk, 2),
        "quantity": _int(contract.get("lot_size")),
        "technicals": technicals,
        "reasons": reasons,
        "spread_percent": round(spread, 3),
        "volume_ratio": round(volume_ratio, 2),
    }, None


def scan_stock_futures(instrument_cache, request_func, log_func=print):
    minimum_days = max(_int(os.getenv("STOCK_FUTURES_MIN_DAYS_TO_EXPIRY"), 2), 0)
    shortlist_size = max(_int(os.getenv("STOCK_FUTURES_MAX_CANDIDATES"), 8), 1)
    contracts = load_nearest_stock_futures(instrument_cache, minimum_days)
    quotes = fetch_bulk_quotes(contracts, request_func)
    shortlist = prefilter_contracts(contracts, quotes, shortlist_size)
    qualified = []
    rejected = []
    for item in shortlist:
        try:
            candidate, reason = evaluate_contract(item)
            if candidate:
                qualified.append(candidate)
            else:
                rejected.append(reason)
        except Exception as error:
            rejected.append(f"{item['contract']['underlying_symbol']}: analysis failed: {error}")
    qualified.sort(key=lambda row: row["signal_score"], reverse=True)
    log_func(
        f"Stock futures scanner: universe={len(contracts)} shortlist={len(shortlist)} "
        f"qualified={len(qualified)}"
    )
    return {
        "universe_count": len(contracts),
        "shortlist_count": len(shortlist),
        "qualified": qualified,
        "rejected": rejected,
    }


def write_scanner_status(path, **values):
    path = Path(path)
    path.parent.mkdir(exist_ok=True)
    payload = {
        "last_run": datetime.now(IST).isoformat(),
        **values,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return payload
