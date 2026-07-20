"""Live NIFTY-50 intraday stock-option scanner.

The scanner is intentionally decision-only. It screens every configured
NIFTY-50 equity in one quote request, shortlists the strongest bullish and
bearish intraday structures, and fully evaluates only those stock options.
Order placement and position state remain owned by trade_bot.py.
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
    get_stock_futures_oi_analysis,
)
from option_chain_trend import get_option_chain_trend, record_option_chain_snapshot
from stock_option_score import (
    stock_option_directional_score,
    stock_option_tradeability,
)
from stock_futures_scanner import NIFTY50_SYMBOLS
from strategy_core import option_chain_signal, option_contract_quality


IST = ZoneInfo("Asia/Kolkata")
UPSTOX_FULL_QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
UPSTOX_OPTION_CONTRACT_URL = "https://api.upstox.com/v2/option/contract"
UPSTOX_OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"

MARKET_CONTEXT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANK": "NSE_INDEX|Nifty Bank",
    "IT": "NSE_INDEX|Nifty IT",
    "AUTO": "NSE_INDEX|Nifty Auto",
    "PHARMA": "NSE_INDEX|Nifty Pharma",
    "FINANCE": "NSE_INDEX|Nifty Fin Service",
    "FMCG": "NSE_INDEX|Nifty FMCG",
    "METAL": "NSE_INDEX|Nifty Metal",
}
SECTOR_BY_SYMBOL = {
    **{symbol: "BANK" for symbol in ("AXISBANK", "HDFCBANK", "ICICIBANK", "INDUSINDBK", "KOTAKBANK", "SBIN")},
    **{symbol: "IT" for symbol in ("HCLTECH", "INFY", "TCS", "TECHM", "WIPRO")},
    **{symbol: "AUTO" for symbol in ("BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO", "M&M", "MARUTI", "TATAMOTORS")},
    **{symbol: "PHARMA" for symbol in ("APOLLOHOSP", "CIPLA", "DRREDDY", "SUNPHARMA")},
    **{symbol: "FINANCE" for symbol in ("BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "JIOFIN", "SBILIFE", "SHRIRAMFIN")},
    **{symbol: "FMCG" for symbol in ("BRITANNIA", "HINDUNILVR", "ITC", "NESTLEIND", "TATACONSUM")},
    **{symbol: "METAL" for symbol in ("HINDALCO", "JSWSTEEL", "TATASTEEL")},
}


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
    if not eligible:
        raise RuntimeError(
            f"No stock-option expiry has at least {int(minimum_days)} days remaining"
        )
    return eligible[0].isoformat()


def _load_instruments(instrument_cache):
    with gzip.open(instrument_cache, "rt", encoding="utf-8") as handle:
        rows = json.load(handle)

    equities = []
    derivatives_by_key = {}
    futures_by_symbol = {}
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
            instrument_type = str(row.get("instrument_type") or "").upper()
            underlying = str(row.get("underlying_symbol") or "").upper()
            expiry = _expiry_date(row.get("expiry"))
            if instrument_type in {"FUT", "FUTSTK"} and underlying and expiry:
                current = futures_by_symbol.get(underlying)
                current_expiry = _expiry_date(current.get("expiry")) if current else None
                if expiry >= datetime.now(IST).date() and (
                    current_expiry is None or expiry < current_expiry
                ):
                    futures_by_symbol[underlying] = row
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
    return equities, derivatives_by_key, futures_by_symbol


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
    last_price = _quote_price(quote)
    net_change = quote.get("net_change")
    if net_change is not None and last_price > 0:
        previous_close = last_price - _number(net_change)
        if previous_close > 0:
            return previous_close
    return _number(
        (quote.get("ohlc") or {}).get("close")
        or quote.get("close_price")
        or quote.get("cp")
    )


def _quote_change_percent(quote):
    last = _quote_price(quote)
    previous = _previous_close(quote)
    return (last - previous) / previous * 100 if last > 0 and previous > 0 else None


def market_context_for(mover, quote_payload):
    """Measure broad-market, sector and stock-relative confirmation."""
    quotes = _quote_rows(quote_payload)
    direction = mover["direction"]
    sign = 1.0 if direction == "BULLISH" else -1.0
    nifty_change = _quote_change_percent(quotes.get(MARKET_CONTEXT_KEYS["NIFTY"], {}))
    sector_name = SECTOR_BY_SYMBOL.get(mover["symbol"])
    sector_key = MARKET_CONTEXT_KEYS.get(sector_name)
    sector_change = _quote_change_percent(quotes.get(sector_key, {})) if sector_key else None
    stock_change = _number(mover.get("change_percent"))

    contributions = []
    if nifty_change is not None:
        contributions.append(0.25 * max(-1.0, min(1.0, sign * nifty_change / 0.35)))
    if sector_change is not None:
        contributions.append(0.50 * max(-1.0, min(1.0, sign * sector_change / 0.35)))
        relative = sign * (stock_change - sector_change)
        contributions.append(0.25 * max(-1.0, min(1.0, relative / 0.50)))
    elif nifty_change is not None:
        relative = sign * (stock_change - nifty_change)
        contributions.append(0.50 * max(-1.0, min(1.0, relative / 0.75)))

    support = sum(contributions)
    reliability = min(1.0, len(contributions) / 3.0)
    bias = (
        direction
        if support >= 0.20
        else ("BEARISH" if direction == "BULLISH" else "BULLISH")
        if support <= -0.20
        else "NEUTRAL"
    )
    confidence = "HIGH" if abs(support) >= 0.65 else "MEDIUM" if abs(support) >= 0.35 else "LOW"
    return {
        "bias": bias,
        "confidence": confidence,
        "directional_support": round(support, 3),
        "reliability": round(reliability, 3),
        "nifty_change_percent": round(nifty_change, 3) if nifty_change is not None else None,
        "sector": sector_name,
        "sector_change_percent": round(sector_change, 3) if sector_change is not None else None,
        "stock_relative_change_percent": round(
            stock_change - (sector_change if sector_change is not None else nifty_change), 3
        ) if sector_change is not None or nifty_change is not None else None,
        "reasons": [
            f"NIFTY={nifty_change:+.2f}%" if nifty_change is not None else "NIFTY context unavailable",
            f"{sector_name}={sector_change:+.2f}%" if sector_change is not None else "Sector context unavailable",
            f"Directional context support={support:+.2f}",
        ],
    }


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
    minimum_move = max(_number(os.getenv("STOCK_OPTION_MIN_MOVER_PERCENT"), 0.25), 0)
    movers = []
    if ranked[-1]["change_percent"] >= minimum_move:
        movers.append(
            {**ranked[-1], "mover_type": "TOP_GAINER", "direction": "BULLISH"}
        )
    if ranked[0]["change_percent"] <= -minimum_move:
        movers.append(
            {**ranked[0], "mover_type": "TOP_LOSER", "direction": "BEARISH"}
        )
    return movers


def rank_intraday_stock_setups(equities, quote_payload):
    """Return all equities that exhibit a strong directional intraday structure."""
    quotes = _quote_rows(quote_payload)
    minimum_move = max(
        _number(os.getenv("STOCK_OPTION_MIN_INTRADAY_MOVE_PERCENT"), 0.75),
        0.05,
    )
    range_edge = min(
        max(_number(os.getenv("STOCK_OPTION_INTRADAY_RANGE_EDGE"), 0.65), 0.50),
        0.95,
    )
    setups = []
    for equity in equities:
        quote = quotes.get(equity["instrument_key"], {})
        ohlc = quote.get("ohlc", {}) or {}
        last_price = _quote_price(quote)
        previous_close = _previous_close(quote)
        open_price = _number(ohlc.get("open") or quote.get("open_price"))
        high_price = _number(ohlc.get("high") or quote.get("high_price"))
        low_price = _number(ohlc.get("low") or quote.get("low_price"))
        average_price = _number(
            quote.get("average_price")
            or quote.get("atp")
            or (quote.get("market_data") or {}).get("atp")
        )
        if (
            min(last_price, previous_close, open_price, high_price, low_price, average_price)
            <= 0
            or high_price <= low_price
        ):
            continue

        change_percent = (last_price - previous_close) / previous_close * 100
        range_position = min(
            max((last_price - low_price) / (high_price - low_price), 0.0),
            1.0,
        )
        direction = None
        if (
            change_percent >= minimum_move
            and last_price > open_price
            and last_price > average_price
            and range_position >= range_edge
        ):
            direction = "BULLISH"
        elif (
            change_percent <= -minimum_move
            and last_price < open_price
            and last_price < average_price
            and range_position <= 1.0 - range_edge
        ):
            direction = "BEARISH"
        if not direction:
            continue

        directional_range_position = (
            range_position if direction == "BULLISH" else 1.0 - range_position
        )
        move_component = min(abs(change_percent) / minimum_move, 3.0) / 3.0 * 45.0
        range_component = directional_range_position * 25.0
        open_distance = abs(last_price - open_price) / open_price * 100
        average_distance = abs(last_price - average_price) / average_price * 100
        open_component = min(open_distance / minimum_move, 2.0) / 2.0 * 15.0
        average_component = min(average_distance / minimum_move, 2.0) / 2.0 * 15.0
        intraday_score = min(
            move_component + range_component + open_component + average_component,
            100.0,
        )
        setups.append(
            {
                **equity,
                "direction": direction,
                "mover_type": f"STRONG_{direction}",
                "last_price": round(last_price, 2),
                "previous_close": round(previous_close, 2),
                "open_price": round(open_price, 2),
                "high_price": round(high_price, 2),
                "low_price": round(low_price, 2),
                "average_price": round(average_price, 2),
                "change_percent": round(change_percent, 3),
                "range_position": round(range_position, 3),
                "intraday_score": round(intraday_score, 1),
            }
        )
    return sorted(setups, key=lambda row: row["intraday_score"], reverse=True)


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


def _select_tradeable_contract(
    symbol,
    expected_direction,
    atm,
    chain,
    derivatives_by_key,
    market_cache_reader,
):
    """Choose ATM or one-step ITM using executable market quality."""
    option_type = "CE" if expected_direction == "BULLISH" else "PE"
    atm_index = int((chain["strike"] - float(atm["strike"])).abs().idxmin())
    itm_index = atm_index - 1 if option_type == "CE" else atm_index + 1
    row_specs = [("ATM", atm_index)]
    if 0 <= itm_index < len(chain):
        row_specs.append(("ITM1", itm_index))

    evaluated = []
    for contract_kind, index in row_specs:
        row = chain.loc[index]
        option_key = row.get(f"{option_type}_instrument_key")
        instrument = derivatives_by_key.get(option_key)
        entry_price = _number(row.get(f"{option_type}_ltp"))
        if not option_key or not instrument or entry_price <= 0:
            continue
        stream_quote = market_cache_reader(option_key) or {}
        quality = option_contract_quality(row, option_type, stream_quote)
        lot_size = max(int(_number(instrument.get("lot_size"), 1)), 1)
        tradeability = stock_option_tradeability(
            symbol=symbol,
            quality=quality,
            chain_volume=row.get(f"{option_type}_volume"),
            lot_size=lot_size,
            stream_quote=stream_quote,
        )
        evaluated.append(
            {
                "contract_kind": contract_kind,
                "row": row,
                "instrument": instrument,
                "option_key": option_key,
                "entry_price": entry_price,
                "quality": quality,
                "tradeability": tradeability,
            }
        )

    allowed = [item for item in evaluated if item["tradeability"]["allowed"]]
    if not allowed:
        details = []
        for item in evaluated:
            blockers = "; ".join(item["tradeability"]["blockers"]) or "score too low"
            details.append(f"{item['contract_kind']}: {blockers}")
        return None, "tradeability rejected: " + (" | ".join(details) or "no ATM/ITM contract")

    allowed.sort(
        key=lambda item: (
            item["tradeability"]["score"],
            -_number(item["tradeability"].get("midpoint_spread_percent"), 999),
            _number(item["row"].get(f"{option_type}_volume")),
            item["contract_kind"] == "ATM",
        ),
        reverse=True,
    )
    return allowed[0], "qualified"


def _evaluate_mover(
    mover,
    derivatives_by_key,
    futures_contract,
    market_context,
    request_func,
    market_cache_reader,
):
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
    # The underlying setup selects CALL versus PUT. A neutral option chain now
    # contributes zero evidence instead of vetoing an otherwise strong setup.
    option_type = "CE" if expected_direction == "BULLISH" else "PE"
    selected, selection_reason = _select_tradeable_contract(
        symbol,
        expected_direction,
        atm,
        chain,
        derivatives_by_key,
        market_cache_reader,
    )
    if not selected:
        return None, selection_reason
    selected_row = selected["row"]
    option_key = selected["option_key"]
    instrument = selected["instrument"]
    entry_price = selected["entry_price"]
    quality = selected["quality"]
    tradeability = selected["tradeability"]

    technicals = get_instrument_technical_analysis(mover["instrument_key"])
    fifteen = technicals.get("fifteen_min", {}) or {}
    five = technicals.get("five_min", {}) or {}
    two = technicals.get("two_hour", {}) or {}
    opposite = "BEARISH" if expected_direction == "BULLISH" else "BULLISH"
    if fifteen.get("bias") != expected_direction or five.get("bias") != expected_direction:
        return None, "5M and 15M must both align with the intraday direction"
    if five.get("vwap_bias") != expected_direction:
        return None, "underlying 5M price structure and VWAP do not agree"
    if two.get("bias") == opposite and two.get("confidence") in {"MEDIUM", "HIGH"}:
        return None, "2H technical trend strongly conflicts with the intraday direction"
    if _number(market_context.get("directional_support")) <= -0.65:
        return None, "sector/broad-market context strongly contradicts the stock direction"

    option_flow = get_option_volume_vwap_analysis(
        option_key, side_label=instrument.get("trading_symbol") or symbol
    )
    futures_flow = (
        get_stock_futures_oi_analysis(futures_contract["instrument_key"])
        if futures_contract and futures_contract.get("instrument_key")
        else {
            "bias": "NEUTRAL",
            "confidence": "LOW",
            "reasons": ["No current stock-future contract was found"],
        }
    )

    technicals["atm_option_flow"] = option_flow
    technicals["option_market_quality"] = quality
    technicals["stock_option_tradeability"] = tradeability
    technicals["stock_futures_flow"] = futures_flow
    technicals["market_context"] = market_context
    technicals["institutional_flow"] = neutral_institutional_footprint(
        "Index-level institutional footprint is not applied to an individual stock"
    )
    trend = get_option_chain_trend(symbol, expected_direction, expiry=expiry)
    option_summary = {
        "bias": direction,
        "confidence": confidence,
        "score": score,
        "strike": float(selected_row["strike"]),
        "expiry": expiry,
        "entry_price": round(entry_price, 2),
        "reasons": reasons,
        "trade_action": "BUY_STOCK_OPTION",
        "transaction_type": "BUY",
        "option_type": option_type,
        "contract_kind": selected["contract_kind"],
        "trading_symbol": instrument.get("trading_symbol"),
        "option_chain_trend": trend,
        "option_market_quality": quality,
        "tradeability": tradeability,
        "mover_type": mover["mover_type"],
        "mover_change_percent": mover["change_percent"],
        "intraday_score": mover.get("intraday_score"),
        "intraday_range_position": mover.get("range_position"),
    }
    weighted = stock_option_directional_score(
        direction=expected_direction,
        technicals=technicals,
        option_chain=recommendation,
        option_flow=option_flow,
        futures_flow=futures_flow,
        market_context=market_context,
    )
    if weighted["grade"] != "TRADE":
        return None, (
            f"directional score {weighted['score']:.1f} is below "
            f"{weighted['minimum_score']:.1f}; chain={direction}/{confidence}"
        )

    return {
        "underlying_symbol": symbol,
        "underlying_instrument_key": mover["instrument_key"],
        "mover_type": mover["mover_type"],
        "mover_change_percent": mover["change_percent"],
        "intraday_score": mover.get("intraday_score"),
        "intraday_range_position": mover.get("range_position"),
        "direction": expected_direction,
        "confidence": confidence,
        "signal_score": score,
        "entry_price": entry_price,
        "instrument": instrument,
        "option_summary": option_summary,
        "technicals": technicals,
        "weighted": weighted,
    }, "qualified"


def scan_stock_option_candidates(instrument_cache, request_func, market_cache_reader, log_func=print):
    equities, derivatives_by_key, futures_by_symbol = _load_instruments(instrument_cache)
    if not equities:
        raise RuntimeError("No eligible NIFTY-50 equity instruments were found")
    quote_payload = request_func(
        "GET",
        UPSTOX_FULL_QUOTE_URL,
        params={
            "instrument_key": ",".join(
                dict.fromkeys(
                    [row["instrument_key"] for row in equities]
                    + list(MARKET_CONTEXT_KEYS.values())
                )
            )
        },
    )
    quotes = _quote_rows(quote_payload)
    quoted_count = sum(
        1 for equity in equities if equity["instrument_key"] in quotes
    )
    setups = rank_intraday_stock_setups(equities, quote_payload)
    bullish = [row for row in setups if row["direction"] == "BULLISH"]
    bearish = [row for row in setups if row["direction"] == "BEARISH"]
    shortlist_limit = max(
        int(_number(os.getenv("STOCK_OPTION_SHORTLIST_PER_SIDE"), 2)),
        1,
    )
    movers = bullish[:shortlist_limit] + bearish[:shortlist_limit]
    movers.sort(key=lambda row: row["intraday_score"], reverse=True)
    log_func(
        "Stock-option NIFTY50 scan: "
        f"universe={len(equities)} quoted={quoted_count} "
        f"strong_bullish={len(bullish)} strong_bearish={len(bearish)} "
        f"shortlisted={len(movers)}"
    )
    if not movers:
        log_func("STOCK_OPTION no trade: no strong NIFTY-50 intraday setup passed the quote gates.")
        return {"movers": [], "qualified": [], "rejected": []}
    log_func(
        "Stock-option shortlist: "
        + ", ".join(
            f"{row['symbol']}={row['direction']} change={row['change_percent']:+.2f}% "
            f"range={row['range_position']:.2f} score={row['intraday_score']:.1f}"
            for row in movers
        )
    )
    qualified = []
    rejected = []
    for mover in movers:
        try:
            candidate, reason = _evaluate_mover(
                mover,
                derivatives_by_key,
                futures_by_symbol.get(mover["symbol"]),
                market_context_for(mover, quote_payload),
                request_func,
                market_cache_reader,
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
