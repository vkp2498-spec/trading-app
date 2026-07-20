"""Decision model for buying intraday options on individual stocks."""

import math
import os
import time


WEIGHTS = {
    "price_structure": 30.0,
    "vwap_momentum": 15.0,
    "cash_volume": 10.0,
    "futures_oi": 10.0,
    "sector_market": 15.0,
    "option_chain": 10.0,
    "option_premium": 10.0,
}


def number(value, default=0.0):
    try:
        result = float(value) if value is not None else default
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def confidence_reliability(confidence):
    return {"HIGH": 1.0, "MEDIUM": 0.65, "LOW": 0.30}.get(confidence, 0.0)


def directional_signal(bias, direction):
    if bias == direction:
        return 1.0
    if bias in {"BULLISH", "BEARISH"}:
        return -1.0
    return 0.0


def _analysis_signal(analysis, direction):
    analysis = analysis or {}
    return (
        directional_signal(analysis.get("bias"), direction)
        * confidence_reliability(analysis.get("confidence"))
    )


def stock_option_directional_score(
    direction,
    technicals,
    option_chain,
    option_flow,
    futures_flow=None,
    market_context=None,
):
    """Return a signed-evidence score without renormalizing missing inputs."""
    reasons = []
    components = {}
    five = technicals.get("five_min", {}) or {}
    fifteen = technicals.get("fifteen_min", {}) or {}
    two = technicals.get("two_hour", {}) or {}

    structure_signal = (
        0.40 * _analysis_signal(five, direction)
        + 0.40 * _analysis_signal(fifteen, direction)
        + 0.20 * _analysis_signal(two, direction)
    )
    components["price_structure"] = WEIGHTS["price_structure"] * structure_signal

    momentum = number(five.get("momentum_score"))
    if direction == "BEARISH":
        momentum = -momentum
    momentum_signal = max(-1.0, min(1.0, momentum / 4.0))
    vwap_signal = directional_signal(five.get("vwap_bias"), direction)
    vwap_momentum_signal = 0.60 * momentum_signal + 0.40 * vwap_signal
    components["vwap_momentum"] = WEIGHTS["vwap_momentum"] * vwap_momentum_signal

    volume_ratio = number(five.get("volume_ratio"))
    if volume_ratio >= 1.50:
        volume_signal = 1.0
    elif volume_ratio >= 1.20:
        volume_signal = 0.75
    elif volume_ratio >= 1.00:
        volume_signal = 0.50
    else:
        volume_signal = 0.0
    if momentum_signal <= 0:
        volume_signal = 0.0
    components["cash_volume"] = WEIGHTS["cash_volume"] * volume_signal

    futures_flow = futures_flow or {}
    futures_signal = (
        directional_signal(futures_flow.get("bias"), direction)
        * confidence_reliability(futures_flow.get("confidence"))
    )
    components["futures_oi"] = WEIGHTS["futures_oi"] * futures_signal

    market_context = market_context or {}
    context_support = market_context.get("directional_support")
    if context_support is None:
        context_support = directional_signal(market_context.get("bias"), direction)
    context_reliability = number(
        market_context.get("reliability"),
        confidence_reliability(market_context.get("confidence")),
    )
    components["sector_market"] = (
        WEIGHTS["sector_market"]
        * max(-1.0, min(1.0, number(context_support)))
        * max(0.0, min(1.0, context_reliability))
    )

    option_chain = option_chain or {}
    chain_signal = (
        directional_signal(option_chain.get("bias"), direction)
        * confidence_reliability(option_chain.get("confidence"))
    )
    components["option_chain"] = WEIGHTS["option_chain"] * chain_signal

    option_flow = option_flow or {}
    premium_signal = directional_signal(option_flow.get("bias"), "BULLISH")
    premium_reliability = confidence_reliability(option_flow.get("confidence"))
    if option_flow.get("volume_confirmed"):
        premium_reliability = min(1.0, premium_reliability + 0.20)
    components["option_premium"] = (
        WEIGHTS["option_premium"] * premium_signal * premium_reliability
    )

    score = round(max(0.0, min(100.0, sum(components.values()))), 1)
    for name, weight in WEIGHTS.items():
        reasons.append(
            f"{name.replace('_', ' ').title()}={components[name]:+.1f}/{weight:.0f}"
        )
    minimum = max(number(os.getenv("STOCK_OPTION_DIRECTIONAL_MIN_SCORE"), 70.0), 0.0)
    return {
        "score": score,
        "grade": "TRADE" if score >= minimum else "SKIP",
        "minimum_score": minimum,
        "components": {key: round(value, 2) for key, value in components.items()},
        "reasons": reasons,
    }


def stock_option_tradeability(
    symbol,
    quality,
    chain_volume,
    lot_size,
    stream_quote=None,
):
    """Fail closed on liquidity, depth, expiry-event and compliance hazards."""
    quality = quality or {}
    stream_quote = stream_quote or {}
    blockers = []
    reasons = []
    components = {}

    blocked = {
        item.strip().upper()
        for key in (
            "STOCK_OPTION_BLOCKED_SYMBOLS",
            "STOCK_OPTION_EVENT_BLOCKED_SYMBOLS",
            "STOCK_OPTION_FNO_BAN_SYMBOLS",
        )
        for item in os.getenv(key, "").split(",")
        if item.strip()
    }
    if symbol.upper() in blocked:
        blockers.append(f"{symbol} is present in a configured event/ban block list")

    bid = number(quality.get("bid_price"))
    ask = number(quality.get("ask_price"))
    if bid <= 0 or ask <= 0 or ask < bid:
        blockers.append("valid bid and ask prices are unavailable")
        components["quote"] = 0.0
    else:
        components["quote"] = 20.0

    midpoint = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
    midpoint_spread = (ask - bid) / midpoint * 100 if midpoint > 0 else None
    maximum_spread = max(number(os.getenv("STOCK_OPTION_MAX_SPREAD_PERCENT"), 2.0), 0.1)
    if midpoint_spread is None or midpoint_spread > maximum_spread:
        blockers.append(
            "midpoint spread is unavailable"
            if midpoint_spread is None
            else f"midpoint spread {midpoint_spread:.2f}% exceeds {maximum_spread:.2f}%"
        )
        components["spread"] = 0.0
    else:
        components["spread"] = 25.0

    depth_multiple = max(number(os.getenv("STOCK_OPTION_MIN_DEPTH_MULTIPLE"), 2.0), 1.0)
    required_depth = max(int(lot_size * depth_multiple), int(lot_size))
    bid_qty = int(number(quality.get("bid_qty")))
    ask_qty = int(number(quality.get("ask_qty")))
    if bid_qty < required_depth or ask_qty < required_depth:
        blockers.append(
            f"bid/ask depth {bid_qty}/{ask_qty} is below required {required_depth}"
        )
        components["depth"] = 0.0
    else:
        components["depth"] = 25.0

    minimum_volume_lots = max(
        number(os.getenv("STOCK_OPTION_MIN_CHAIN_VOLUME_LOTS"), 3.0), 1.0
    )
    required_volume = max(int(lot_size * minimum_volume_lots), int(lot_size))
    if number(chain_volume) < required_volume:
        blockers.append(
            f"option volume {int(number(chain_volume))} is below required {required_volume}"
        )
        components["volume"] = 0.0
    else:
        components["volume"] = 15.0

    delta = quality.get("delta")
    minimum_delta = number(os.getenv("STOCK_OPTION_MIN_DELTA"), 0.20)
    maximum_delta = number(os.getenv("STOCK_OPTION_MAX_DELTA"), 0.80)
    if delta is None:
        components["greeks"] = 0.0
        reasons.append("delta unavailable; no Greeks credit")
    elif not minimum_delta <= abs(number(delta)) <= maximum_delta:
        blockers.append(
            f"delta {number(delta):.3f} is outside {minimum_delta:.2f}-{maximum_delta:.2f}"
        )
        components["greeks"] = 0.0
    else:
        components["greeks"] = 15.0

    received_at = number(stream_quote.get("received_at"))
    maximum_age = max(number(os.getenv("STOCK_OPTION_MAX_QUOTE_AGE_SECONDS"), 15.0), 1.0)
    if received_at > 0 and time.time() - received_at > maximum_age:
        blockers.append("streaming option quote is stale")

    score = round(sum(components.values()), 1)
    minimum_score = max(number(os.getenv("STOCK_OPTION_MIN_TRADEABILITY_SCORE"), 75.0), 0.0)
    allowed = not blockers and score >= minimum_score
    reasons.extend(
        [f"{name.title()}={value:.0f}" for name, value in components.items()]
    )
    return {
        "allowed": allowed,
        "score": score,
        "minimum_score": minimum_score,
        "midpoint_spread_percent": round(midpoint_spread, 3) if midpoint_spread is not None else None,
        "required_depth": required_depth,
        "required_volume": required_volume,
        "blockers": blockers,
        "reasons": reasons,
    }
