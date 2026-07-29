def direction_score(value, desired_direction):
    if desired_direction not in {"BULLISH", "BEARISH"}:
        return 0.0

    if value == desired_direction:
        return 1.0

    if value == "NEUTRAL" or value is None:
        return 0.5

    return 0.0


def confidence_multiplier(confidence):
    return {
        "HIGH": 1.0,
        "MEDIUM": 0.7,
        "LOW": 0.4,
    }.get(confidence, 0.4)


def banknifty_neutral_chain_direction(technicals):
    """Return a direction only for unusually strong non-chain confirmation."""
    five = technicals.get("five_min", {}) or {}
    fifteen = technicals.get("fifteen_min", {}) or {}
    two = technicals.get("two_hour", {}) or {}
    breadth = technicals.get("banknifty_breadth", {}) or {}

    direction = five.get("bias")
    blockers = []
    if direction not in {"BULLISH", "BEARISH"} or fifteen.get("bias") != direction:
        blockers.append("5M and 15M price structures are not directionally aligned")
    if five.get("confidence") not in {"MEDIUM", "HIGH"}:
        blockers.append("5M confidence is below MEDIUM")
    if fifteen.get("confidence") not in {"MEDIUM", "HIGH"}:
        blockers.append("15M confidence is below MEDIUM")
    if two.get("bias") not in {direction, "NEUTRAL"} and two.get("confidence") in {"MEDIUM", "HIGH"}:
        blockers.append("2H structure materially opposes the proposed direction")
    if breadth.get("bias") != direction or breadth.get("confidence") not in {"MEDIUM", "HIGH"}:
        blockers.append("major-bank breadth does not confirm with MEDIUM/HIGH confidence")
    momentum = float(five.get("momentum_score") or 0)
    if direction == "BEARISH":
        momentum = -momentum
    if momentum < 3:
        blockers.append("5M momentum is not strongly aligned")

    return (direction if not blockers else None), blockers


def nifty_neutral_chain_direction(technicals):
    """Infer NIFTY direction only when non-chain evidence is unusually strong."""
    five = technicals.get("five_min", {}) or {}
    fifteen = technicals.get("fifteen_min", {}) or {}
    two = technicals.get("two_hour", {}) or {}
    breadth = technicals.get("nifty_breadth", {}) or {}

    direction = five.get("bias")
    blockers = []
    if direction not in {"BULLISH", "BEARISH"} or fifteen.get("bias") != direction:
        blockers.append("5M and 15M price structures are not directionally aligned")
    if five.get("confidence") not in {"MEDIUM", "HIGH"}:
        blockers.append("5M confidence is below MEDIUM")
    if fifteen.get("confidence") not in {"MEDIUM", "HIGH"}:
        blockers.append("15M confidence is below MEDIUM")
    if two.get("bias") not in {direction, "NEUTRAL"} and two.get("confidence") in {
        "MEDIUM",
        "HIGH",
    }:
        blockers.append("2H structure materially opposes the proposed direction")
    if breadth.get("bias") != direction or breadth.get("confidence") not in {"MEDIUM", "HIGH"}:
        blockers.append("NIFTY constituent breadth does not confirm with MEDIUM/HIGH confidence")

    momentum = float(five.get("momentum_score") or 0)
    if direction == "BEARISH":
        momentum = -momentum
    if momentum < 3:
        blockers.append("5M momentum is not strongly aligned")

    return (direction if not blockers else None), blockers


def _alignment_component(analysis, direction, weight):
    analysis = analysis or {}
    return (
        float(weight)
        * direction_score(analysis.get("bias"), direction)
        * confidence_multiplier(analysis.get("confidence"))
    )


def _option_chain_component(option_summary, option_chain_trend, direction):
    chain_bias = option_summary.get("chain_bias", direction)
    chain_confidence = option_summary.get(
        "chain_confidence", option_summary.get("confidence")
    )
    snapshot = (
        15.0
        * direction_score(chain_bias, direction)
        * confidence_multiplier(chain_confidence)
    )
    trend_bias = (option_chain_trend or {}).get("bias", "NEUTRAL")
    trend = 10.0 * direction_score(trend_bias, direction)
    return snapshot + trend, chain_bias, chain_confidence, trend_bias


def _vwap_component(technicals, direction):
    five = technicals.get("five_min", {}) or {}
    option_flow = technicals.get("atm_option_flow", {}) or {}
    underlying = 7.5 * direction_score(five.get("vwap_bias"), direction)
    # A long CE or PE should strengthen when its own premium is above VWAP.
    premium = 7.5 * direction_score(option_flow.get("bias"), "BULLISH")
    return underlying + premium


def _support_resistance_component(technicals, direction):
    return _alignment_component(technicals.get("five_min"), direction, 8.0) + _alignment_component(
        technicals.get("fifteen_min"), direction, 7.0
    )


def _volume_component(technicals):
    flow = technicals.get("atm_option_flow", {}) or {}
    ratio = float(flow.get("volume_ratio") or 0)
    if ratio >= 1.50:
        return 10.0, ratio
    if ratio >= 1.20:
        return 8.0, ratio
    if ratio >= 1.00:
        return 5.0, ratio
    if ratio >= 0.75:
        return 2.0, ratio
    return 0.0, ratio


def _volatility_component(technicals, direction):
    regime = technicals.get("market_regime", {}) or {}
    name = regime.get("regime", "RANGE")
    regime_direction = regime.get("direction", "NEUTRAL")
    if regime_direction not in {direction, "NEUTRAL"}:
        return 0.0, name
    if name in {"TREND", "VOLATILITY_EXPANSION"} and regime_direction == direction:
        return 10.0, name
    if name == "RANGE":
        return (7.0 if regime_direction == direction else 5.0), name
    if name == "COMPRESSION":
        return 2.0, name
    return 0.0, name


def _fifteen_min_candle_component(technicals, direction):
    candle = technicals.get("fifteen_min", {}) or {}
    try:
        open_price = float(candle.get("open"))
        high = float(candle.get("high"))
        low = float(candle.get("low"))
        close = float(candle.get("close"))
    except (TypeError, ValueError):
        return 0.0, 0.0
    candle_range = high - low
    if candle_range <= 0:
        return 0.0, 0.0
    directional = close > open_price if direction == "BULLISH" else close < open_price
    if not directional:
        return 0.0, 0.0
    body_ratio = abs(close - open_price) / candle_range
    close_location = (close - low) / candle_range
    closes_at_edge = close_location >= 0.75 if direction == "BULLISH" else close_location <= 0.25
    if body_ratio >= 0.55 and closes_at_edge:
        return 5.0, body_ratio
    if body_ratio >= 0.35:
        return 3.0, body_ratio
    return 1.0, body_ratio


def weighted_alignment_score(option_summary, technicals, option_chain_trend):
    """Score the setup once; callers use this as the strategy cutoff.

    Component weights total 100 exactly. Liquidity, risk limits and broker
    validity remain separate execution safeguards rather than score inputs.
    """
    direction = option_summary.get("bias")
    if direction not in {"BULLISH", "BEARISH"}:
        return {"score": 0, "grade": "SKIP", "reasons": ["Direction is not defined"]}

    chain, chain_bias, chain_confidence, trend_bias = _option_chain_component(
        option_summary, option_chain_trend, direction
    )
    higher_timeframe = _alignment_component(
        technicals.get("two_hour"), direction, 20.0
    )
    vwap = _vwap_component(technicals, direction)
    support_resistance = _support_resistance_component(technicals, direction)
    volume, volume_ratio = _volume_component(technicals)
    volatility, regime = _volatility_component(technicals, direction)
    candle, body_ratio = _fifteen_min_candle_component(technicals, direction)

    components = {
        "option_chain": round(chain, 1),
        "higher_timeframe_trend": round(higher_timeframe, 1),
        "vwap": round(vwap, 1),
        "support_resistance": round(support_resistance, 1),
        "volume": round(volume, 1),
        "volatility": round(volatility, 1),
        "fifteen_min_candlestick": round(candle, 1),
    }
    total = max(0.0, min(100.0, round(sum(components.values()), 1)))
    direct_cutoff = 75.0
    grade = "TRADE" if total >= direct_cutoff else "CAUTIOUS_TRADE" if total >= 65 else "SKIP"
    reasons = [
        f"Option chain={components['option_chain']:.1f}/25 "
        f"(snapshot={chain_bias}/{chain_confidence}, trend={trend_bias})",
        f"Higher-timeframe trend={components['higher_timeframe_trend']:.1f}/20",
        f"VWAP={components['vwap']:.1f}/15",
        f"Support/resistance={components['support_resistance']:.1f}/15",
        f"Volume={components['volume']:.1f}/10 (option volume_ratio={volume_ratio:.2f})",
        f"Volatility={components['volatility']:.1f}/10 ({regime})",
        f"15M candlestick={components['fifteen_min_candlestick']:.1f}/5 "
        f"(body_ratio={body_ratio:.2f})",
    ]
    return {
        "score": total,
        "grade": grade,
        "components": components,
        "weights": {
            "option_chain": 25,
            "higher_timeframe_trend": 20,
            "vwap": 15,
            "support_resistance": 15,
            "volume": 10,
            "volatility": 10,
            "fifteen_min_candlestick": 5,
        },
        "reasons": reasons,
    }
