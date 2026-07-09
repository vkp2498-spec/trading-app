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

    reasons = []

    option_conf = confidence_multiplier(option_summary.get("confidence"))
    option_component = 40 * option_conf
    reasons.append(f"Option-chain component={option_component:.1f}/40")

    fifteen = technicals.get("fifteen_min", {}) or {}
    fifteen_component = 25 * direction_score(fifteen.get("bias"), direction) * confidence_multiplier(fifteen.get("confidence"))
    reasons.append(f"15M component={fifteen_component:.1f}/25")

    four = technicals.get("four_hour", {}) or {}
    four_component = 20 * direction_score(four.get("bias"), direction) * confidence_multiplier(four.get("confidence"))
    reasons.append(f"4H component={four_component:.1f}/20")

    five = technicals.get("five_min", {}) or {}
    momentum_score = float(five.get("momentum_score") or 0)
    if direction == "BEARISH":
        momentum_score = -momentum_score

    momentum_component = 0
    if momentum_score >= 3:
        momentum_component = 10
    elif momentum_score >= 1:
        momentum_component = 6
    elif momentum_score >= 0:
        momentum_component = 3

    if five.get("volume_confirmed"):
        momentum_component = min(10, momentum_component + 2)

    reasons.append(f"5M momentum/volume component={momentum_component:.1f}/10")

    vwap_component = 0
    if five.get("vwap_bias") == direction:
        vwap_component = 5
    elif five.get("vwap_bias") == "NEUTRAL":
        vwap_component = 2.5

    reasons.append(f"VWAP component={vwap_component:.1f}/5")

    trend_bonus = 0
    if option_chain_trend.get("bias") == direction:
        trend_bonus = 5
        reasons.append("Option-chain trend confirms direction (+5 bonus)")
    elif option_chain_trend.get("bias") not in {direction, "NEUTRAL"}:
        trend_bonus = -10
        reasons.append("Option-chain trend conflicts direction (-10 penalty)")

    total = option_component + fifteen_component + four_component + momentum_component + vwap_component + trend_bonus
    total = max(0, min(100, round(total, 1)))

    if total >= 80:
        grade = "TRADE"
    elif total >= 60:
        grade = "CAUTIOUS_TRADE"
    else:
        grade = "SKIP"

    return {
        "score": total,
        "grade": grade,
        "reasons": reasons,
    }