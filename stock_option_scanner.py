"""Live NIFTY-50 top-mover stock-option scanner.

The scanner is intentionally decision-only. It ranks the current top gainer
and loser, evaluates one ATM option for each, and returns qualified long-option
candidates. Order placement and position state remain owned by trade_bot.py.
"""

import gzip
import json
import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from institutional_flow import neutral_institutional_footprint
from market_technicals import (
    get_instrument_technical_analysis,
    get_option_volume_vwap_analysis,
)
from option_chain_trend import get_option_chain_trend, record_option_chain_snapshot
from signal_score import weighted_alignment_score
from stock_futures_scanner import NIFTY50_SYMBOLS
from strategy_core import option_chain_signal, option_contract_quality


IST = ZoneInfo("Asia/Kolkata")
UPSTOX_FULL_QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
UPSTOX_OPTION_CONTRACT_URL = "https://api.upstox.com/v2/option/contract"
UPSTOX_OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"


def _number(value, default=0.0):
    try:
        result = float(value) if value is not None else default
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _expiry_date(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, IST).date()
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %b"):
        try:
            if fmt == "%d %b":
                return datetime.strptime(f"{text} {datetime.now(IST).year}", "%d %b %Y").date()
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def choose_stock_option_expiry(expiries, today=None, minimum_days=3):
    """Choose the first expiry with enough time, automatically rolling forward."""
    today = today or datetime.now(IST).date()
    parsed = sorted(
        {expiry for expiry in (_expiry_date(value) for value in expiries) if expiry and expiry >= today}
    )
    if not parsed:
        raise RuntimeError("No current stock-option expiry is available")
    eligible = [expiry for expiry in parsed if (expiry - today).days >= int(minimum_days)]
    return (eligible[0] if eligible else parsed[-1]).isoformat()


def _load_instruments(instrument_cache):
    with gzip.open(instrument_cache, "rt", encoding="utf-8") as handle:
        rows = json.load(handle)

    equities = []
    derivatives_by_key = {}
    configured = os.getenv("STOCK_OPTION_SYMBOLS", "").strip()
    allowed = (
        {item.strip().upper() for item in configured.split(",") if item.strip()}
        if configured
        else set(NIFTY50_SYMBOLS)
    )
    seen = set()
    for row in rows:
        key = row.get("instrument_key")
        segment = str(row.get("segment") or "").upper()
        if segment == "NSE_FO" and key:
            derivatives_by_key[key] = row
            continue
        if segment != "NSE_EQ":
            continue
        symbol = str(row.get("trading_symbol") or row.get("short_name") or "").upper()
        instrument_type = str(row.get("instrument_type") or "").upper()
        if not key or not symbol or symbol in seen or symbol not in allowed:
            continue
        if instrument_type and instrument_type not in {"EQ", "EQUITY"}:
            continue
        seen.add(symbol)
        equities.append(
            {
                "symbol": symbol,
                "instrument_key": key,
                "trading_symbol": row.get("trading_symbol") or symbol,
            }
        )
    return equities, derivatives_by_key


def _quote_rows(payload):
    data = (payload or {}).get("data", {}) or {}
    rows = {}
    for response_key, quote in (data.items() if isinstance(data, dict) else []):
        if not isinstance(quote, dict):
            continue
        key = quote.get("instrument_token") or quote.get("instrument_key")
        key = key or str(response_key).replace(":", "|", 1)
        rows[key] = quote
    return rows


def _quote_price(quote):
    return _number(
        (quote or {}).get("last_price")
        or (quote or {}).get("ltp")
        or ((quote or {}).get("market_data") or {}).get("ltp")
    )


def _previous_close(quote):
    quote = quote or {}
    return _number(
        (quote.get("ohlc") or {}).get("close")
        or quote.get("close_price")
        or quote.get("cp")
    )


def rank_top_movers(equities, quote_payload):
    quotes = _quote_rows(quote_payload)
    ranked = []
    for equity in equities:
        quote = quotes.get(equity["instrument_key"], {})
        last_price = _quote_price(quote)
        previous_close = _previous_close(quote)
        if last_price <= 0 or previous_close <= 0:
            continue
        change_percent = (last_price - previous_close) / previous_close * 100
        ranked.append(
            {
                **equity,
                "last_price": round(last_price, 2),
                "previous_close": round(previous_close, 2),
                "change_percent": round(change_percent, 3),
            }
        )
    if not ranked:
        return []
    ranked.sort(key=lambda row: row["change_percent"])
    loser = {**ranked[0], "mover_type": "TOP_LOSER", "direction": "BEARISH"}
    gainer = {**ranked[-1], "mover_type": "TOP_GAINER", "direction": "BULLISH"}
    return [gainer] if gainer["symbol"] == loser["symbol"] else [gainer, loser]


def _chain_frames(symbol, equity_key, expiry, request_func, nearby=5):
    payload = request_func(
        "GET",
        UPSTOX_OPTION_CHAIN_URL,
        params={"instrument_key": equity_key, "expiry_date": expiry},
    )
    rows = []
    for item in payload.get("data", []) or []:
        call = item.get("call_options", {}) or {}
        put = item.get("put_options", {}) or {}
        call_md = call.get("market_data", {}) or {}
        put_md = put.get("market_data", {}) or {}
        call_greeks = call.get("option_greeks", {}) or {}
        put_greeks = put.get("option_greeks", {}) or {}
        ce_oi = _number(call_md.get("oi"))
        pe_oi = _number(put_md.get("oi"))
        ce_prev = _number(call_md.get("prev_oi"))
        pe_prev = _number(put_md.get("prev_oi"))
        rows.append(
            {
                "symbol": symbol,
                "expiry": expiry,
                "spot": item.get("underlying_spot_price"),
                "strike": item.get("strike_price"),
                "CE_ltp": call_md.get("ltp"),
                "CE_instrument_key": call.get("instrument_key"),
                "CE_bid_price": call_md.get("bid_price"),
                "CE_ask_price": call_md.get("ask_price"),
                "CE_bid_qty": call_md.get("bid_qty"),
                "CE_ask_qty": call_md.get("ask_qty"),
                "CE_oi": ce_oi,
                "CE_previous_oi": ce_prev,
                "CE_change_oi": ce_oi - ce_prev,
                "CE_volume": call_md.get("volume"),
                "CE_iv": call_greeks.get("iv"),
                "CE_delta": call_greeks.get("delta"),
                "CE_gamma": call_greeks.get("gamma"),
                "CE_theta": call_greeks.get("theta"),
                "CE_vega": call_greeks.get("vega"),
                "CE_pop": call_greeks.get("pop"),
                "PE_ltp": put_md.get("ltp"),
                "PE_instrument_key": put.get("instrument_key"),
                "PE_bid_price": put_md.get("bid_price"),
                "PE_ask_price": put_md.get("ask_price"),
                "PE_bid_qty": put_md.get("bid_qty"),
                "PE_ask_qty": put_md.get("ask_qty"),
                "PE_oi": pe_oi,
                "PE_previous_oi": pe_prev,
                "PE_change_oi": pe_oi - pe_prev,
                "PE_volume": put_md.get("volume"),
                "PE_iv": put_greeks.get("iv"),
                "PE_delta": put_greeks.get("delta"),
                "PE_gamma": put_greeks.get("gamma"),
                "PE_theta": put_greeks.get("theta"),
                "PE_vega": put_greeks.get("vega"),
                "PE_pop": put_greeks.get("pop"),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError(f"No option-chain rows returned for {symbol} {expiry}")
    for column in frame.columns:
        if column not in {"symbol", "expiry", "CE_instrument_key", "PE_instrument_key"}:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)
    spot = frame["spot"].dropna()
    if spot.empty:
        index = (frame["CE_ltp"] - frame["PE_ltp"]).abs().idxmin()
    else:
        index = (frame["strike"] - float(spot.iloc[0])).abs().idxmin()
    start = max(0, int(index) - nearby)
    end = min(len(frame), int(index) + nearby + 1)
    return frame.iloc[int(index)], frame.iloc[start:end].copy(), frame


def _get_expiry(equity_key, request_func):
    payload = request_func(
        "GET", UPSTOX_OPTION_CONTRACT_URL, params={"instrument_key": equity_key}
    )
    expiries = {row.get("expiry") for row in payload.get("data", []) or [] if row.get("expiry")}
    minimum_days = max(int(float(os.getenv("STOCK_OPTION_MIN_DAYS_TO_EXPIRY", "3"))), 0)
    return choose_stock_option_expiry(expiries, minimum_days=minimum_days)


def _quality_allowed(quality):
    maximum_spread = _number(os.getenv("STOCK_OPTION_MAX_SPREAD_PERCENT"), 2.5)
    spread = quality.get("spread_percent")
    if spread is not None and float(spread) > maximum_spread:
        return False, f"spread {float(spread):.2f}% exceeds {maximum_spread:.2f}%"
    delta = quality.get("delta")
    minimum_delta = _number(os.getenv("STOCK_OPTION_MIN_DELTA"), 0.20)
    maximum_delta = _number(os.getenv("STOCK_OPTION_MAX_DELTA"), 0.80)
    if delta is not None and not minimum_delta <= abs(float(delta)) <= maximum_delta:
        return False, f"delta {float(delta):.3f} is outside {minimum_delta:.2f}-{maximum_delta:.2f}"
    return True, "liquidity and Greeks accepted"


def _evaluate_mover(mover, derivatives_by_key, request_func, market_cache_reader):
    symbol = mover["symbol"]
    expected_direction = mover["direction"]
    expiry = _get_expiry(mover["instrument_key"], request_func)
    atm, nearby, chain = _chain_frames(
        symbol, mover["instrument_key"], expiry, request_func
    )
    direction, confidence, score, reasons = option_chain_signal(atm)
    recommendation = {
        "symbol": symbol,
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "reasons": reasons,
        "atm": atm.to_dict(),
        "nearby_flow": {
            "ce_oi": float(nearby["CE_oi"].fillna(0).sum()),
            "pe_oi": float(nearby["PE_oi"].fillna(0).sum()),
            "ce_change_oi": float(nearby["CE_change_oi"].fillna(0).sum()),
            "pe_change_oi": float(nearby["PE_change_oi"].fillna(0).sum()),
        },
    }
    record_option_chain_snapshot(symbol, recommendation)
    if direction != expected_direction or confidence != "HIGH" or abs(score) < 4:
        return None, (
            f"mover={expected_direction} but option chain={direction}/{confidence} score={score}"
        )

    option_type = "CE" if direction == "BULLISH" else "PE"
    option_key = atm.get(f"{option_type}_instrument_key")
    instrument = derivatives_by_key.get(option_key)
    entry_price = _number(atm.get(f"{option_type}_ltp"))
    if not instrument or entry_price <= 0:
        return None, "ATM option instrument or premium is unavailable"

    technicals = get_instrument_technical_analysis(mover["instrument_key"])
    fifteen = technicals.get("fifteen_min", {}) or {}
    five = technicals.get("five_min", {}) or {}
    two = technicals.get("two_hour", {}) or {}
    opposite = "BEARISH" if direction == "BULLISH" else "BULLISH"
    if fifteen.get("bias") != direction or five.get("bias") != direction:
        return None, "5M and 15M must both align with the top-mover direction"
    if two.get("bias") == opposite and two.get("confidence") in {"MEDIUM", "HIGH"}:
        return None, "2H technical trend strongly conflicts with the top-mover direction"

    stream_quote = market_cache_reader(option_key) or {}
    quality = option_contract_quality(atm, option_type, stream_quote)
    quality["max_spread_percent"] = _number(
        os.getenv("STOCK_OPTION_MAX_SPREAD_PERCENT"), 2.5
    )
    quality_ok, quality_reason = _quality_allowed(quality)
    if not quality_ok:
        return None, quality_reason

    option_flow = get_option_volume_vwap_analysis(
        option_key, side_label=instrument.get("trading_symbol") or symbol
    )
    minimum_volume = _number(os.getenv("STOCK_OPTION_MIN_VOLUME_RATIO"), 1.0)
    if option_flow.get("bias") != "BULLISH":
        return None, "selected option premium is not above a supportive VWAP trend"
    if _number(option_flow.get("volume_ratio")) < minimum_volume:
        return None, f"selected option volume ratio is below {minimum_volume:.2f}"

    technicals["atm_option_flow"] = option_flow
    technicals["option_market_quality"] = quality
    technicals["institutional_flow"] = neutral_institutional_footprint(
        "Index-level institutional footprint is not applied to an individual stock"
    )
    trend = get_option_chain_trend(symbol, direction, expiry=expiry)
    option_summary = {
        "bias": direction,
        "confidence": confidence,
        "score": score,
        "strike": float(atm["strike"]),
        "expiry": expiry,
        "entry_price": round(entry_price, 2),
        "reasons": reasons,
        "trade_action": "BUY_STOCK_OPTION",
        "transaction_type": "BUY",
        "option_type": option_type,
        "trading_symbol": instrument.get("trading_symbol"),
        "option_chain_trend": trend,
        "option_market_quality": quality,
        "mover_type": mover["mover_type"],
        "mover_change_percent": mover["change_percent"],
    }
    weighted = weighted_alignment_score(option_summary, technicals, trend)
    minimum_score = _number(os.getenv("STOCK_OPTION_MIN_WEIGHTED_SCORE"), 80.0)
    if weighted.get("grade") != "TRADE" or _number(weighted.get("score")) < minimum_score:
        return None, f"weighted score {weighted.get('score')} is below TRADE/{minimum_score:.1f}"

    return {
        "underlying_symbol": symbol,
        "underlying_instrument_key": mover["instrument_key"],
        "mover_type": mover["mover_type"],
        "mover_change_percent": mover["change_percent"],
        "direction": direction,
        "confidence": confidence,
        "signal_score": score,
        "entry_price": entry_price,
        "instrument": instrument,
        "option_summary": option_summary,
        "technicals": technicals,
        "weighted": weighted,
    }, "qualified"


def scan_stock_option_candidates(instrument_cache, request_func, market_cache_reader, log_func=print):
    equities, derivatives_by_key = _load_instruments(instrument_cache)
    if not equities:
        raise RuntimeError("No eligible NIFTY-50 equity instruments were found")
    quote_payload = request_func(
        "GET",
        UPSTOX_FULL_QUOTE_URL,
        params={"instrument_key": ",".join(row["instrument_key"] for row in equities)},
    )
    movers = rank_top_movers(equities, quote_payload)
    if not movers:
        raise RuntimeError("Could not calculate top gainer and loser from live quotes")
    log_func(
        "Stock-option movers: "
        + ", ".join(
            f"{row['mover_type']}={row['symbol']}({row['change_percent']:+.2f}%)"
            for row in movers
        )
    )
    qualified = []
    rejected = []
    for mover in movers:
        try:
            candidate, reason = _evaluate_mover(
                mover, derivatives_by_key, request_func, market_cache_reader
            )
            if candidate:
                qualified.append(candidate)
                log_func(
                    f"STOCK_OPTION {mover['symbol']} qualified: direction={candidate['direction']} "
                    f"score={candidate['weighted']['score']} contract="
                    f"{candidate['instrument'].get('trading_symbol')}"
                )
            else:
                rejected.append({"symbol": mover["symbol"], "reason": reason})
                log_func(f"STOCK_OPTION {mover['symbol']} rejected: {reason}")
        except Exception as error:
            rejected.append({"symbol": mover["symbol"], "reason": str(error)})
            log_func(f"STOCK_OPTION {mover['symbol']} analysis failed: {error}")
    qualified.sort(
        key=lambda row: (
            _number(row.get("weighted", {}).get("score")),
            abs(_number(row.get("mover_change_percent"))),
        ),
        reverse=True,
    )
    return {"movers": movers, "qualified": qualified, "rejected": rejected}
