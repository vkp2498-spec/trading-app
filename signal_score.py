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


def _banknifty_alignment_score(option_summary, technicals, option_chain_trend):
    direction = option_summary.get("bias")
    reasons = []

    # Price trend, pivots and Bollinger Bands: 35%.
    price_component = 0.0
    for key, label, weight in (
        ("fifteen_min", "15M", 15.0),
        ("five_min", "5M", 12.0),
        ("two_hour", "2H", 8.0),
    ):
        analysis = technicals.get(key, {}) or {}
        if analysis.get("bias") == "NEUTRAL":
            value = weight * 0.50
        else:
            value = (
                weight
                * direction_score(analysis.get("bias"), direction)
                * confidence_multiplier(analysis.get("confidence"))
            )
        price_component += value
        reasons.append(f"{label} price/pivot/BB={value:.1f}/{weight:.0f}")

    # VWAP, volume and completed-candle confirmation: 25%.
    five = technicals.get("five_min", {}) or {}
    momentum = float(five.get("momentum_score") or 0)
    if direction == "BEARISH":
        momentum = -momentum
    candle_component = 10.0 if momentum >= 3 else 6.5 if momentum >= 1 else 3.0 if momentum >= 0 else 0.0
    if five.get("volume_confirmed"):
        candle_component = min(10.0, candle_component + 1.5)

    flow = technicals.get("atm_option_flow", {}) or {}
    flow_bias = flow.get("bias")
    volume_ratio = float(flow.get("volume_ratio") or 0)
    if flow_bias == "BULLISH":
        flow_component = 15.0 if volume_ratio >= 1.5 else 12.0 if volume_ratio >= 1.2 else 8.0
    elif flow_bias == "NEUTRAL":
        flow_component = 4.0 if volume_ratio >= 1.2 else 1.5
    else:
        flow_component = 0.0
    confirmation_component = min(candle_component + flow_component, 25.0)
    reasons.append(
        f"VWAP/volume/candle confirmation={confirmation_component:.1f}/25 "
        f"(option volume_ratio={volume_ratio:.2f})"
    )

    # Option-chain change in OI: 25%. Snapshot and multi-run trend are kept distinct.
    chain_bias = option_summary.get("chain_bias", direction)
    chain_confidence = option_summary.get("chain_confidence", option_summary.get("confidence"))
    snapshot_component = (
        15.0 * confidence_multiplier(chain_confidence)
        if chain_bias == direction
        else 0.0
    )
    trend_bias = option_chain_trend.get("bias")
    trend_component = 10.0 if trend_bias == direction else 4.0 if trend_bias == "NEUTRAL" else 0.0
    oi_component = snapshot_component + trend_component
    reasons.append(
        f"Option-chain OI change={oi_component:.1f}/25 "
        f"(snapshot={chain_bias}/{chain_confidence}, trend={trend_bias})"
    )

    # Major-bank constituent breadth: 15%.
    breadth = technicals.get("banknifty_breadth", {}) or {}
    if breadth.get("bias") == direction:
        breadth_component = 15.0 * confidence_multiplier(breadth.get("confidence"))
    elif breadth.get("bias") == "NEUTRAL" and int(breadth.get("coverage") or 0) >= 3:
        breadth_component = 3.0
    else:
        breadth_component = 0.0
    reasons.append(
        f"Major-bank breadth={breadth_component:.1f}/15 "
        f"({breadth.get('bias', 'NEUTRAL')}/{breadth.get('confidence', 'LOW')})"
    )

    total = max(
        0.0,
        min(100.0, round(price_component + confirmation_component + oi_component + breadth_component, 1)),
    )
    grade = "TRADE" if total >= 80 else "CAUTIOUS_TRADE" if total >= 65 else "SKIP"
    return {"score": total, "grade": grade, "reasons": reasons}


def weighted_alignment_score(option_summary, technicals, option_chain_trend):
    direction = option_summary.get("bias")

    if direction not in {"BULLISH", "BEARISH"}:
        return {
            "score": 0,
            "grade": "SKIP",
            "reasons": ["Option chain is not directional"],
        }

    if option_summary.get("symbol") == "BANKNIFTY":
        return _banknifty_alignment_score(option_summary, technicals, option_chain_trend)

    weights = {
        "option_chain": 35,
        "fifteen_min": 25,
        "two_hour": 10,
        "five_min": 15,
        "atm_option_flow": 15,
    }

    reasons = []

    chain_bias = option_summary.get("chain_bias", direction)
    chain_confidence = option_summary.get(
        "chain_confidence", option_summary.get("confidence")
    )
    option_component = (
        weights["option_chain"] * confidence_multiplier(chain_confidence)
        if chain_bias == direction
        else 0.0
    )
    reasons.append(
        f"Option-chain component={option_component:.1f}/{weights['option_chain']} "
        f"({chain_bias}/{chain_confidence})"
    )

    fifteen = technicals.get("fifteen_min", {}) or {}
    fifteen_component = (
        weights["fifteen_min"]
        * direction_score(fifteen.get("bias"), direction)
        * confidence_multiplier(fifteen.get("confidence"))
    )
    reasons.append(f"15M component={fifteen_component:.1f}/{weights['fifteen_min']}")

    two = technicals.get("two_hour", {}) or {}
    two_component = (
        weights["two_hour"]
        * direction_score(two.get("bias"), direction)
        * confidence_multiplier(two.get("confidence"))
    )
    reasons.append(f"2H component={two_component:.1f}/{weights['two_hour']}")

    five = technicals.get("five_min", {}) or {}
    momentum_score = float(five.get("momentum_score") or 0)
    if direction == "BEARISH":
        momentum_score = -momentum_score

    momentum_component = 0
    if momentum_score >= 3:
        momentum_component = weights["five_min"]
    elif momentum_score >= 1:
        momentum_component = weights["five_min"] * 0.65
    elif momentum_score >= 0:
        momentum_component = weights["five_min"] * 0.30

    if five.get("volume_confirmed"):
        momentum_component = min(weights["five_min"], momentum_component + 2)

    reasons.append(f"5M momentum/volume component={momentum_component:.1f}/{weights['five_min']}")

    option_flow = technicals.get("atm_option_flow", {}) or {}

    flow_component = 0
    flow_bias = option_flow.get("bias")
    volume_ratio = float(option_flow.get("volume_ratio") or 0)

    # Volume confirmation is graded. A ratio barely above 1.0 should not earn
    # the same score as an option trading at 1.5x or 2x its recent volume.
    if flow_bias == "BULLISH":
        if volume_ratio >= 1.50:
            flow_component = weights["atm_option_flow"]
        elif volume_ratio >= 1.20:
            flow_component = weights["atm_option_flow"] * 0.80
        elif volume_ratio >= 1.00:
            flow_component = weights["atm_option_flow"] * 0.50
        else:
            flow_component = weights["atm_option_flow"] * 0.35
    elif flow_bias == "NEUTRAL":
        flow_component = weights["atm_option_flow"] * (0.25 if volume_ratio >= 1.20 else 0.10)

    reasons.append(
        f"ATM option VWAP/volume component={flow_component:.1f}/{weights['atm_option_flow']} "
        f"(volume_ratio={volume_ratio:.2f})"
    )

    quality = technicals.get("option_market_quality", {}) or {}
    quality_adjustment = 0.0
    spread_percent = quality.get("spread_percent")
    if spread_percent is not None:
        if float(spread_percent) <= 1.0:
            quality_adjustment += 2.0
            reasons.append(f"Option spread is liquid ({float(spread_percent):.2f}%) (+2)")
        elif float(spread_percent) > float(quality.get("max_spread_percent") or 2.5):
            quality_adjustment -= 5.0
            reasons.append(f"Option spread is wide ({float(spread_percent):.2f}%) (-5)")

    depth_bias = quality.get("depth_bias")
    if depth_bias == direction:
        quality_adjustment += 2.0
        reasons.append(f"ATM option depth supports {direction} (+2)")
    elif depth_bias in {"BULLISH", "BEARISH"} and depth_bias != direction:
        quality_adjustment -= 3.0
        reasons.append(f"ATM option depth conflicts with {direction} (-3)")

    if quality.get("delta") is not None:
        quality_adjustment += 1.0
        reasons.append(f"ATM option Greeks available (delta={float(quality['delta']):.3f}) (+1)")
    if quality_adjustment:
        flow_component = max(0.0, min(weights["atm_option_flow"], flow_component + quality_adjustment))
        reasons.append(f"ATM option liquidity/Greeks adjustment={quality_adjustment:+.1f}")

    trend_bonus = 0
    if option_chain_trend.get("bias") == direction:
        trend_bonus = 5
        reasons.append("Option-chain trend confirms direction (+5 bonus)")
    elif option_chain_trend.get("bias") not in {direction, "NEUTRAL"}:
        trend_bonus = -10
        reasons.append("Option-chain trend conflicts direction (-10 penalty)")

    institutional = technicals.get("institutional_flow", {}) or {}
    institutional_bias = institutional.get("bias")
    institutional_confidence = institutional.get("confidence")
    institutional_adjustment = 0

    if institutional_bias == direction:
        institutional_adjustment = {
            "HIGH": 10,
            "MEDIUM": 6,
            "LOW": 3,
        }.get(institutional_confidence, 0)
        reasons.append(
            f"Institutional footprint aligns ({institutional_confidence}) "
            f"(+{institutional_adjustment} bonus)"
        )
    elif institutional_bias in {"BULLISH", "BEARISH"}:
        institutional_adjustment = {
            "HIGH": -15,
            "MEDIUM": -10,
            "LOW": -5,
        }.get(institutional_confidence, 0)
        reasons.append(
            f"Institutional footprint conflicts ({institutional_confidence}) "
            f"({institutional_adjustment} penalty)"
        )
    else:
        reasons.append("Institutional footprint is neutral (no adjustment)")

    breadth_adjustment = 0.0
    nifty_breadth = technicals.get("nifty_breadth", {}) or {}
    if nifty_breadth:
        if nifty_breadth.get("bias") == direction:
            breadth_adjustment = {
                "HIGH": 6.0,
                "MEDIUM": 4.0,
                "LOW": 1.0,
            }.get(nifty_breadth.get("confidence"), 0.0)
            reasons.append(
                f"NIFTY constituent breadth aligns ({nifty_breadth.get('confidence')}) "
                f"(+{breadth_adjustment:.0f})"
            )
        elif nifty_breadth.get("bias") in {"BULLISH", "BEARISH"}:
            breadth_adjustment = {
                "HIGH": -10.0,
                "MEDIUM": -7.0,
                "LOW": -2.0,
            }.get(nifty_breadth.get("confidence"), 0.0)
            reasons.append(
                f"NIFTY constituent breadth conflicts ({nifty_breadth.get('confidence')}) "
                f"({breadth_adjustment:.0f})"
            )

    total = (
        option_component
        + fifteen_component
        + two_component
        + momentum_component
        + flow_component
        + trend_bonus
        + institutional_adjustment
        + breadth_adjustment
    )
    total = max(0, min(100, round(total, 1)))

    if total >= 80:
        grade = "TRADE"
    elif total >= 65:
        grade = "CAUTIOUS_TRADE"
    else:
        grade = "SKIP"

    return {
        "score": total,
        "grade": grade,
        "reasons": reasons,
    }
