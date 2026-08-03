"""Pure live-entry and position-management filters for index options."""

from __future__ import annotations


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def opposite_direction(direction):
    return "BEARISH" if direction == "BULLISH" else "BULLISH"


def bollinger_exhaustion_reversal(
    technicals,
    *,
    min_extension_fraction=0.10,
    min_extension_atr=0.20,
    min_rejection_wick_fraction=0.25,
    min_five_minute_momentum=2.0,
):
    """Identify a confirmed 15-minute Bollinger exhaustion reversal.

    An outer-band excursion alone is not a reversal. The completed 15-minute
    candle must reject the extreme and the latest completed five-minute candle
    must confirm movement back in the proposed direction.
    """
    fifteen = technicals.get("fifteen_min", {}) or {}
    five = technicals.get("five_min", {}) or {}

    fifteen_open = _number(fifteen.get("open"))
    fifteen_high = _number(fifteen.get("high"))
    fifteen_low = _number(fifteen.get("low"))
    fifteen_close = _number(fifteen.get("close"))
    upper = _number(fifteen.get("upper_band"))
    lower = _number(fifteen.get("lower_band"))
    atr = _number(fifteen.get("atr14"))
    band_width = upper - lower

    if (
        min(fifteen_open, fifteen_high, fifteen_low, fifteen_close) <= 0
        or band_width <= 0
    ):
        return {
            "confirmed": False,
            "direction": None,
            "reason": "completed 15M Bollinger values are unavailable",
        }

    upper_extension = max(0.0, fifteen_high - upper)
    lower_extension = max(0.0, lower - fifteen_low)
    if upper_extension <= 0 and lower_extension <= 0:
        return {
            "confirmed": False,
            "direction": None,
            "reason": "15M candle did not extend beyond an outer Bollinger band",
        }

    if upper_extension >= lower_extension:
        direction = "BEARISH"
        extension = upper_extension
        rejection_wick = fifteen_high - max(fifteen_open, fifteen_close)
        closed_back_inside = fifteen_close <= upper
        reversal_body = fifteen_close < fifteen_open
    else:
        direction = "BULLISH"
        extension = lower_extension
        rejection_wick = min(fifteen_open, fifteen_close) - fifteen_low
        closed_back_inside = fifteen_close >= lower
        reversal_body = fifteen_close > fifteen_open

    candle_range = max(fifteen_high - fifteen_low, 0.01)
    wick_fraction = max(0.0, rejection_wick) / candle_range
    required_extension = max(
        band_width * max(_number(min_extension_fraction), 0.0),
        atr * max(_number(min_extension_atr), 0.0),
    )
    extension_multiple = extension / required_extension if required_extension > 0 else 0.0
    fifteen_rejected = (
        closed_back_inside
        or reversal_body
        or wick_fraction >= max(_number(min_rejection_wick_fraction), 0.0)
    )

    five_open = _number(five.get("open"))
    five_close = _number(five.get("close"))
    five_momentum = _number(five.get("momentum_score"))
    signed_momentum = five_momentum if direction == "BULLISH" else -five_momentum
    five_body_confirms = (
        five_close > five_open if direction == "BULLISH" else five_close < five_open
    )
    five_confirms = (
        signed_momentum >= max(_number(min_five_minute_momentum), 0.0)
        and (five.get("bias") == direction or five_body_confirms)
    )
    confirmed = (
        extension >= required_extension
        and fifteen_rejected
        and five_confirms
    )

    reasons = [
        f"15M extension={extension:.2f} versus required={required_extension:.2f}",
        f"15M rejection wick={wick_fraction:.2f}; closed_back_inside={closed_back_inside}",
        f"5M signed reversal momentum={signed_momentum:.1f}",
    ]
    if not confirmed:
        if extension < required_extension:
            reasons.append("outer-band extension is not sufficiently extreme")
        if not fifteen_rejected:
            reasons.append("15M candle did not reject the extreme")
        if not five_confirms:
            reasons.append("completed 5M candle did not confirm the reversal")

    return {
        "confirmed": confirmed,
        "direction": direction,
        "timeframe": "15M",
        "candle_time": fifteen.get("candle_time"),
        "extension": round(extension, 2),
        "required_extension": round(required_extension, 2),
        "extension_multiple": round(extension_multiple, 2),
        "extension_fraction": round(extension / band_width, 4),
        "rejection_wick_fraction": round(wick_fraction, 4),
        "closed_back_inside": bool(closed_back_inside),
        "reversal_body": bool(reversal_body),
        "five_minute_signed_momentum": round(signed_momentum, 2),
        "five_minute_confirmed": bool(five_confirms),
        "reasons": reasons,
    }


def classify_market_regime(
    technicals,
    *,
    compression_width_percent=0.18,
    extreme_atr_percent=0.35,
):
    """Classify the current completed-candle environment without future data."""
    five = technicals.get("five_min", {}) or {}
    fifteen = technicals.get("fifteen_min", {}) or {}
    two = technicals.get("two_hour", {}) or {}
    close = _number(five.get("close"))
    atr = _number(five.get("atr14"))
    middle = _number(five.get("middle_band"))
    upper = _number(five.get("upper_band"))
    lower = _number(five.get("lower_band"))
    atr_percent = atr / close * 100 if close > 0 and atr > 0 else 0.0
    width_percent = (
        (upper - lower) / middle * 100
        if middle > 0 and upper > lower
        else 0.0
    )
    five_bias = five.get("bias")
    fifteen_bias = fifteen.get("bias")
    two_bias = two.get("bias")
    aligned_direction = (
        five_bias
        if five_bias in {"BULLISH", "BEARISH"} and fifteen_bias == five_bias
        else None
    )
    momentum = abs(_number(five.get("momentum_score")))
    vwap_aligned = five.get("vwap_bias") == aligned_direction
    outside_band = close > upper or close < lower if upper > lower else False
    reasons = [
        f"5M ATR={atr_percent:.3f}%",
        f"5M Bollinger width={width_percent:.3f}%",
    ]

    if atr_percent >= max(_number(extreme_atr_percent), 0.01):
        regime = "EXTREME_VOLATILITY"
        reasons.append("Five-minute realised volatility is above the extreme threshold")
    elif width_percent and width_percent <= max(_number(compression_width_percent), 0.01):
        regime = "COMPRESSION"
        reasons.append("Bollinger width shows low-volatility compression")
    elif aligned_direction and outside_band and momentum >= 3:
        regime = "VOLATILITY_EXPANSION"
        reasons.append(f"5M/15M align {aligned_direction} with a band expansion")
    elif aligned_direction and momentum >= 3 and vwap_aligned:
        regime = "TREND"
        reasons.append(f"5M/15M and VWAP align {aligned_direction}")
    else:
        regime = "RANGE"
        reasons.append("Trend and volatility-expansion conditions are incomplete")

    confidence = "HIGH" if regime in {"TREND", "VOLATILITY_EXPANSION"} and two_bias in {aligned_direction, "NEUTRAL"} else "MEDIUM"
    if regime in {"RANGE", "COMPRESSION"}:
        confidence = "LOW" if not aligned_direction else "MEDIUM"
    return {
        "regime": regime,
        "confidence": confidence,
        "direction": aligned_direction or "NEUTRAL",
        "atr_percent": round(atr_percent, 4),
        "bollinger_width_percent": round(width_percent, 4),
        "reasons": reasons,
    }


def entry_structure_for_direction(technicals, direction, *, retest_buffer_atr=0.25):
    """Describe a completed-candle breakout, retest, pullback, or continuation."""
    five = technicals.get("five_min", {}) or {}
    fifteen = technicals.get("fifteen_min", {}) or {}
    close = _number(five.get("close"))
    high = _number(five.get("high"), close)
    low = _number(five.get("low"), close)
    previous_close = _number(five.get("prev_close"), close)
    pivot = _number(five.get("pivot"))
    middle = _number(five.get("middle_band"))
    vwap = _number(five.get("vwap"))
    atr = max(_number(five.get("atr14")), 0.01)
    buffer_value = atr * max(_number(retest_buffer_atr), 0.0)
    momentum = _number(five.get("momentum_score"))
    signed_momentum = momentum if direction == "BULLISH" else -momentum
    five_bias = five.get("bias")
    five_confidence = five.get("confidence", "LOW")
    fifteen_bias = fifteen.get("bias")
    fifteen_aligned = fifteen_bias == direction
    five_aligned = five_bias == direction
    five_neutral = five_bias in {None, "NEUTRAL"}
    five_opposite = five_bias == opposite_direction(direction)
    reference = None
    structure = "NONE"
    reasons = []

    if direction == "BULLISH":
        if pivot > 0 and previous_close <= pivot < close:
            structure, reference = "BREAKOUT", pivot
        elif pivot > 0 and previous_close > pivot and low <= pivot + buffer_value and close > pivot:
            structure, reference = "RETEST_HOLD", pivot
        elif middle > 0 and low <= middle + buffer_value and close > middle and signed_momentum >= 2:
            structure, reference = "PULLBACK_HOLD", middle
        elif five_aligned and fifteen_aligned and vwap > 0 and close > vwap and signed_momentum >= 3:
            structure, reference = "TREND_CONTINUATION", vwap
    elif direction == "BEARISH":
        if pivot > 0 and previous_close >= pivot > close:
            structure, reference = "BREAKOUT", pivot
        elif pivot > 0 and previous_close < pivot and high >= pivot - buffer_value and close < pivot:
            structure, reference = "RETEST_HOLD", pivot
        elif middle > 0 and high >= middle - buffer_value and close < middle and signed_momentum >= 2:
            structure, reference = "PULLBACK_HOLD", middle
        elif five_aligned and fifteen_aligned and vwap > 0 and close < vwap and signed_momentum >= 3:
            structure, reference = "TREND_CONTINUATION", vwap

    neutral_timing_confirmed = bool(
        five_neutral and structure != "NONE" and signed_momentum >= 2
    )
    timing_confirmed = bool(five_aligned or neutral_timing_confirmed)
    qualified = bool(
        structure != "NONE"
        and fifteen_aligned
        and timing_confirmed
    )
    watch_eligible = bool(
        fifteen_aligned
        and (
            (five_neutral and not neutral_timing_confirmed)
            or (five_opposite and five_confidence == "LOW")
        )
    )
    if structure == "NONE":
        reasons.append("No completed-candle breakout, retest, pullback hold, or strong continuation")
    else:
        reasons.append(f"{structure} around {reference:.2f}")
    if not fifteen_aligned:
        reasons.append(
            f"15M direction is not aligned: bias={fifteen_bias or 'UNAVAILABLE'}; "
            f"required={direction}"
        )
    elif five_neutral and neutral_timing_confirmed:
        reasons.append(
            f"5M is neutral but timing is confirmed by {structure} with "
            f"signed momentum={signed_momentum:.1f}"
        )
    elif five_neutral:
        reasons.append(
            f"5M is neutral and timing is unconfirmed: structure={structure}; "
            f"signed momentum={signed_momentum:.1f}"
        )
    elif five_opposite:
        reasons.append(
            f"5M opposes {direction}: bias={five_bias}; "
            f"confidence={five_confidence}; signed momentum={signed_momentum:.1f}"
        )
    return {
        "type": structure,
        "qualified": qualified,
        "watch_eligible": watch_eligible,
        "direction": direction,
        "reference": round(reference, 2) if reference else None,
        "signed_momentum": round(signed_momentum, 2),
        "fifteen_minute_aligned": fifteen_aligned,
        "five_minute_bias": five_bias or "UNAVAILABLE",
        "five_minute_confidence": five_confidence,
        "five_minute_timing_confirmed": timing_confirmed,
        "reasons": reasons,
    }


def live_entry_gate(
    direction,
    technicals,
    weighted_score,
    *,
    enabled=True,
    range_minimum_score=85.0,
    continuation_minimum_score=85.0,
):
    if not enabled:
        return {"allowed": True, "reason": "live regime/structure gate disabled"}
    regime = technicals.get("market_regime", {}) or classify_market_regime(technicals)
    structure = technicals.get("entry_structure", {}) or entry_structure_for_direction(
        technicals, direction
    )
    breadth = (
        technicals.get("banknifty_breadth", {})
        or technicals.get("nifty_breadth", {})
        or {}
    )
    regime_name = regime.get("regime")
    score = _number(weighted_score)
    if regime_name == "EXTREME_VOLATILITY":
        return {"allowed": False, "reason": "extreme-volatility regime", "regime": regime, "structure": structure}
    if regime_name == "COMPRESSION":
        return {"allowed": False, "reason": "compression regime has not expanded", "regime": regime, "structure": structure}
    if regime.get("direction") not in {direction, "NEUTRAL"}:
        return {"allowed": False, "reason": "market regime conflicts with trade direction", "regime": regime, "structure": structure}
    if (
        breadth.get("bias") == opposite_direction(direction)
        and breadth.get("confidence") in {"MEDIUM", "HIGH"}
    ):
        return {"allowed": False, "reason": "constituent breadth materially conflicts with trade direction", "regime": regime, "structure": structure}
    if not structure.get("qualified"):
        return {
            "allowed": False,
            "watch_eligible": bool(structure.get("watch_eligible")),
            "reason": "; ".join(
                structure.get("reasons") or ["entry structure is not qualified"]
            ),
            "regime": regime,
            "structure": structure,
        }
    if regime_name == "RANGE" and score < _number(range_minimum_score, 85.0):
        return {"allowed": False, "reason": f"range regime requires score >= {_number(range_minimum_score, 85.0):.1f}", "regime": regime, "structure": structure}
    if regime_name == "RANGE" and breadth.get("bias") != direction:
        return {"allowed": False, "reason": "range-regime entry requires aligned constituent breadth", "regime": regime, "structure": structure}
    if structure.get("type") == "TREND_CONTINUATION" and score < _number(continuation_minimum_score, 85.0):
        return {"allowed": False, "reason": f"continuation entry requires score >= {_number(continuation_minimum_score, 85.0):.1f}", "regime": regime, "structure": structure}
    return {"allowed": True, "reason": f"{regime_name} with {structure.get('type')}", "regime": regime, "structure": structure}


def structural_invalidation(technicals, direction, *, atr_buffer=0.20):
    """Return the nearest defensible underlying invalidation beyond structure."""
    five = technicals.get("five_min", {}) or {}
    close = _number(five.get("close"))
    atr = max(_number(five.get("atr14")), 0.01)
    candidates = []
    if direction == "BULLISH":
        for label, value in (
            ("recent swing low", five.get("recent_swing_low")),
            ("pivot", five.get("pivot")),
            ("Bollinger middle", five.get("middle_band")),
            ("VWAP", five.get("vwap")),
        ):
            value = _number(value)
            if 0 < value < close:
                candidates.append((value, label))
        if not candidates:
            return None
        reference, label = max(candidates)
        stop = reference - atr * max(_number(atr_buffer), 0.0)
    else:
        for label, value in (
            ("recent swing high", five.get("recent_swing_high")),
            ("pivot", five.get("pivot")),
            ("Bollinger middle", five.get("middle_band")),
            ("VWAP", five.get("vwap")),
        ):
            value = _number(value)
            if value > close:
                candidates.append((value, label))
        if not candidates:
            return None
        reference, label = min(candidates)
        stop = reference + atr * max(_number(atr_buffer), 0.0)
    return {
        "entry_underlying": round(close, 2),
        "stop_underlying": round(stop, 2),
        "reference": label,
        "reference_value": round(reference, 2),
        "atr": round(atr, 2),
    }


def underlying_exit_reason(
    state,
    underlying_ltp,
    elapsed_minutes,
    *,
    structural_enabled=True,
    time_stop_enabled=True,
    time_stop_minutes=20.0,
    minimum_progress_percent=15.0,
):
    ltp = _number(underlying_ltp)
    if ltp <= 0:
        return None
    direction = state.get("direction")
    structural_stop = _number(state.get("underlying_structural_stop"))
    if structural_enabled and structural_stop > 0:
        breached = ltp <= structural_stop if direction == "BULLISH" else ltp >= structural_stop
        if breached:
            return "STRUCTURAL_STOP"
    if not time_stop_enabled or _number(elapsed_minutes) < _number(time_stop_minutes, 20.0):
        return None
    entry = _number(state.get("underlying_entry_price"))
    target_points = _number(state.get("target_points"))
    if entry <= 0 or target_points <= 0:
        return None
    favorable = ltp - entry if direction == "BULLISH" else entry - ltp
    progress = favorable / target_points * 100
    if progress < _number(minimum_progress_percent, 15.0):
        return "TIME_STOP"
    return None
