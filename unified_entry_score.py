"""Unified Vamsi entry score combining signals and former strategy gates."""

from __future__ import annotations


UNIFIED_SCORE_VERSION = "VAMSI_UNIFIED_ENTRY_V1"
COMPONENT_WEIGHTS = {
    "core_alignment": 25.0,
    "direction_and_structure": 25.0,
    "breadth": 20.0,
    "institutional": 10.0,
    "market_regime": 10.0,
    "trade_feasibility": 10.0,
}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clamp(value, minimum, maximum):
    return max(float(minimum), min(float(maximum), float(value)))


def _opposite(direction):
    return "BEARISH" if direction == "BULLISH" else "BULLISH"


def _alignment_points(snapshot, direction, maximum):
    bias = str((snapshot or {}).get("bias") or "NEUTRAL").upper()
    if bias == direction:
        return float(maximum)
    if bias in {"NEUTRAL", "NONE", "UNAVAILABLE", ""}:
        return float(maximum) * 0.5
    return 0.0


def direction_and_structure_component(technicals, direction):
    fifteen = _alignment_points(technicals.get("fifteen_min"), direction, 8.0)
    five = _alignment_points(technicals.get("five_min"), direction, 5.0)
    two_hour = _alignment_points(technicals.get("two_hour"), direction, 4.0)
    structure = technicals.get("entry_structure", {}) or {}
    if structure.get("qualified"):
        structure_points = 8.0
    elif structure.get("watch_eligible"):
        structure_points = 4.0
    elif structure.get("type") not in {None, "", "NONE"}:
        structure_points = 2.0
    else:
        structure_points = 0.0
    total = fifteen + five + two_hour + structure_points
    return total, {
        "fifteen_minute": round(fifteen, 2),
        "five_minute": round(five, 2),
        "two_hour": round(two_hour, 2),
        "entry_structure": round(structure_points, 2),
        "structure_type": structure.get("type") or "NONE",
        "structure_qualified": bool(structure.get("qualified")),
    }


def oriented_context_component(context, direction, maximum):
    context = context or {}
    bias = str(context.get("bias") or "NEUTRAL").upper()
    raw_score = context.get("score")
    if raw_score is None:
        if bias == direction:
            raw_score = 70.0
        elif bias == _opposite(direction):
            raw_score = -70.0
        else:
            raw_score = 0.0
    oriented = _clamp(_number(raw_score), -100.0, 100.0)
    if direction == "BEARISH":
        oriented *= -1.0
    points = ((oriented + 100.0) / 200.0) * float(maximum)
    return _clamp(points, 0.0, maximum), round(oriented, 2)


def market_regime_component(technicals, direction, days_to_expiry=None):
    regime = technicals.get("market_regime", {}) or {}
    name = str(regime.get("regime") or "RANGE").upper()
    regime_direction = str(regime.get("direction") or "NEUTRAL").upper()
    if regime_direction == _opposite(direction):
        points = 0.0
    elif name in {"TREND", "VOLATILITY_EXPANSION"}:
        points = 10.0 if regime_direction == direction else 8.0
    elif name == "RANGE":
        points = 5.0
    elif name == "COMPRESSION":
        points = 2.0
    elif name == "EXTREME_VOLATILITY":
        points = 0.0
    else:
        points = 4.0
    expiry_penalty = 0.0
    if days_to_expiry is not None and _number(days_to_expiry, 99) <= 2:
        expiry_penalty = min(points, 2.0)
        points -= expiry_penalty
    return points, {
        "regime": name,
        "regime_direction": regime_direction,
        "days_to_expiry": days_to_expiry,
        "expiry_penalty": round(expiry_penalty, 2),
    }


def trade_feasibility_component(feasibility):
    feasibility = feasibility or {}
    target_candidates = feasibility.get("technical_target_candidates") or []
    target_points = 2.0 if target_candidates else 0.0

    minimum_rr = max(_number(feasibility.get("minimum_reward_risk"), 0.8), 0.01)
    reward_risk = max(_number(feasibility.get("technical_reward_risk")), 0.0)
    reward_risk_points = _clamp(reward_risk / minimum_rr, 0.0, 1.0) * 6.0

    extension = feasibility.get("entry_extension_percent")
    maximum_extension = max(
        _number(feasibility.get("maximum_entry_extension_percent"), 1.5),
        0.01,
    )
    if extension is None:
        extension_points = 1.0
    else:
        extension = _number(extension)
        if extension <= maximum_extension:
            extension_points = 2.0
        else:
            excess_ratio = (extension - maximum_extension) / maximum_extension
            extension_points = _clamp(2.0 * (1.0 - excess_ratio), 0.0, 2.0)

    total = target_points + reward_risk_points + extension_points
    return total, {
        "technical_target": round(target_points, 2),
        "reward_risk": round(reward_risk_points, 2),
        "entry_extension": round(extension_points, 2),
        "observed_reward_risk": round(reward_risk, 2),
        "minimum_reward_risk": round(minimum_rr, 2),
        "former_gate_allowed": bool(feasibility.get("allowed")),
    }


def unified_entry_score(
    base_alignment,
    technicals,
    institutional,
    direction,
    *,
    feasibility=None,
    days_to_expiry=None,
    live_gate=None,
):
    """Return the one 0-100 score used for Vamsi entry and research."""
    base_score = _clamp((base_alignment or {}).get("score", 0.0), 0.0, 100.0)
    core_points = base_score / 100.0 * COMPONENT_WEIGHTS["core_alignment"]
    direction_points, direction_detail = direction_and_structure_component(
        technicals,
        direction,
    )
    breadth = (
        technicals.get("banknifty_breadth", {})
        or technicals.get("nifty_breadth", {})
        or {}
    )
    breadth_points, breadth_oriented = oriented_context_component(
        breadth,
        direction,
        COMPONENT_WEIGHTS["breadth"],
    )
    institutional_points, institutional_oriented = oriented_context_component(
        institutional,
        direction,
        COMPONENT_WEIGHTS["institutional"],
    )
    regime_points, regime_detail = market_regime_component(
        technicals,
        direction,
        days_to_expiry,
    )
    feasibility_points, feasibility_detail = trade_feasibility_component(feasibility)

    components = {
        "core_alignment": round(core_points, 2),
        "direction_and_structure": round(direction_points, 2),
        "breadth": round(breadth_points, 2),
        "institutional": round(institutional_points, 2),
        "market_regime": round(regime_points, 2),
        "trade_feasibility": round(feasibility_points, 2),
    }
    total = round(_clamp(sum(components.values()), 0.0, 100.0), 1)
    grade = "TRADE" if total >= 65 else "CAUTIOUS_TRADE" if total >= 55 else "SKIP"
    return {
        "score": total,
        "score_kind": "UNIFIED_SIGNAL_AND_GATE_SCORE_NOT_PROBABILITY",
        "score_version": UNIFIED_SCORE_VERSION,
        "probability_calibrated": False,
        "grade": grade,
        "components": components,
        "weights": dict(COMPONENT_WEIGHTS),
        "details": {
            "base_alignment_score": round(base_score, 1),
            "direction_and_structure": direction_detail,
            "breadth_oriented_score": breadth_oriented,
            "institutional_oriented_score": institutional_oriented,
            "market_regime": regime_detail,
            "trade_feasibility": feasibility_detail,
            "former_live_gate_allowed": bool((live_gate or {}).get("allowed")),
            "former_live_gate_reason": (live_gate or {}).get("reason"),
        },
        "reasons": [
            f"Core alignment={components['core_alignment']:.1f}/25",
            f"Direction and structure={components['direction_and_structure']:.1f}/25",
            f"Breadth={components['breadth']:.1f}/20 (oriented={breadth_oriented:.1f})",
            f"Institutional={components['institutional']:.1f}/10 (oriented={institutional_oriented:.1f})",
            f"Market regime={components['market_regime']:.1f}/10",
            f"Trade feasibility={components['trade_feasibility']:.1f}/10",
        ],
    }
