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


def weighted_alignment_score(option_summary, technicals, option_chain_trend):
    direction = option_summary.get("bias")

    if direction not in {"BULLISH", "BEARISH"}:
        return {
            "score": 0,
            "grade": "SKIP",
            "reasons": ["Option chain is not directional"],
        }

    weights = {
        "option_chain": 35,
        "fifteen_min": 25,
        "two_hour": 10,
        "five_min": 15,
        "atm_option_flow": 15,
    }

    reasons = []

    option_conf = confidence_multiplier(option_summary.get("confidence"))
    option_component = weights["option_chain"] * option_conf
    reasons.append(f"Option-chain component={option_component:.1f}/{weights['option_chain']}")

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
    if option_flow.get("bias") == "BULLISH" and option_flow.get("volume_confirmed"):
        flow_component = weights["atm_option_flow"]
    elif option_flow.get("bias") == "BULLISH":
        flow_component = weights["atm_option_flow"] * 0.70
    elif option_flow.get("bias") == "NEUTRAL":
        flow_component = weights["atm_option_flow"] * 0.30
    else:
        flow_component = 0

    reasons.append(f"ATM option VWAP/volume component={flow_component:.1f}/{weights['atm_option_flow']}")

    trend_bonus = 0
    if option_chain_trend.get("bias") == direction:
        trend_bonus = 5
        reasons.append("Option-chain trend confirms direction (+5 bonus)")
    elif option_chain_trend.get("bias") not in {direction, "NEUTRAL"}:
        trend_bonus = -10
        reasons.append("Option-chain trend conflicts direction (-10 penalty)")

    total = option_component + fifteen_component + two_component + momentum_component + flow_component + trend_bonus
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