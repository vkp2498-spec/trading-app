"""Pure rules for Ganesh's NIFTY/BANKNIFTY opening-gap reversal strategy."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from math import sqrt


BULLISH = "BULLISH"
BEARISH = "BEARISH"
NEUTRAL = "NEUTRAL"
GAP_UP = "GAP_UP"
GAP_DOWN = "GAP_DOWN"
NO_GAP = "NO_GAP"


def opening_gap(previous_close, today_open, minimum_gap_percent=0.20):
    previous_close = float(previous_close or 0)
    today_open = float(today_open or 0)
    if previous_close <= 0 or today_open <= 0:
        raise ValueError("previous close and today's open must be positive")
    points = today_open - previous_close
    percent = points / previous_close * 100.0
    threshold = abs(float(minimum_gap_percent))
    direction = GAP_UP if percent >= threshold else GAP_DOWN if percent <= -threshold else NO_GAP
    return {
        "direction": direction,
        "points": round(points, 2),
        "percent": round(percent, 4),
    }


def classic_pivots(previous_high, previous_low, previous_close):
    high = float(previous_high)
    low = float(previous_low)
    close = float(previous_close)
    if high <= 0 or low <= 0 or close <= 0 or high < low:
        raise ValueError("previous-day OHLC is invalid")
    pivot = (high + low + close) / 3.0
    return {
        "P": round(pivot, 2),
        "R1": round(2 * pivot - low, 2),
        "S1": round(2 * pivot - high, 2),
        "R2": round(pivot + high - low, 2),
        "S2": round(pivot - high + low, 2),
        "R3": round(high + 2 * (pivot - low), 2),
        "S3": round(low - 2 * (high - pivot), 2),
    }


def bollinger_bands(closes, period=20, standard_deviations=2.0):
    values = [float(value) for value in closes if value is not None]
    period = int(period)
    if period < 2 or len(values) < period:
        return None
    window = values[-period:]
    middle = sum(window) / period
    variance = sum((value - middle) ** 2 for value in window) / period
    deviation = sqrt(variance)
    multiplier = float(standard_deviations)
    return {
        "middle": round(middle, 2),
        "upper": round(middle + multiplier * deviation, 2),
        "lower": round(middle - multiplier * deviation, 2),
    }


def active_two_hour_start(timestamp):
    """Return NSE-aligned starts: 09:15, 11:15 and 13:15 IST."""
    current = timestamp
    session_open = current.replace(hour=9, minute=15, second=0, microsecond=0)
    if current < session_open:
        return session_open
    elapsed = max((current - session_open).total_seconds(), 0)
    bucket = min(int(elapsed // (2 * 60 * 60)), 2)
    return session_open + timedelta(hours=2 * bucket)


def candle_colour(open_price, current_price, confirmation_buffer_points=0.0):
    open_price = float(open_price)
    current_price = float(current_price)
    buffer_points = max(float(confirmation_buffer_points), 0.0)
    if current_price > open_price + buffer_points:
        return "GREEN"
    if current_price < open_price - buffer_points:
        return "RED"
    return "NEUTRAL"


def transition_for_gap(gap_direction):
    if gap_direction == GAP_DOWN:
        return {
            "initial_colour": "RED",
            "reversal_colour": "GREEN",
            "direction": BULLISH,
            "option_type": "CE",
        }
    if gap_direction == GAP_UP:
        return {
            "initial_colour": "GREEN",
            "reversal_colour": "RED",
            "direction": BEARISH,
            "option_type": "PE",
        }
    return None


def continuation_for_gap(gap_direction):
    if gap_direction == GAP_UP:
        return {
            "direction": BULLISH,
            "option_type": "CE",
            "acceptance_colour": "GREEN",
        }
    if gap_direction == GAP_DOWN:
        return {
            "direction": BEARISH,
            "option_type": "PE",
            "acceptance_colour": "RED",
        }
    return None


def advance_entry_confirmation(
    state,
    candle_start,
    colour,
    gap_direction,
    required_scans=2,
    buffer_confirmed=False,
):
    """Advance the intrabar colour state without triggering on first observation."""
    updated = dict(state or {})
    transition = transition_for_gap(gap_direction)
    if not transition:
        return updated, False
    candle_key = candle_start.isoformat() if hasattr(candle_start, "isoformat") else str(candle_start)
    if updated.get("current_candle_start") != candle_key:
        updated.update(
            {
                "current_candle_start": candle_key,
                "initial_colour_observed": False,
                "reversal_confirmation_scans": 0,
                "previous_confirmed_colour": None,
                "phase": "WAITING_FOR_INITIAL_COLOUR",
            }
        )

    initial = transition["initial_colour"]
    reversal = transition["reversal_colour"]
    if colour == initial:
        updated["initial_colour_observed"] = True
        updated["previous_confirmed_colour"] = initial
        updated["reversal_confirmation_scans"] = 0
        updated["phase"] = "WAITING_FOR_REVERSAL"
        return updated, False

    if not updated.get("initial_colour_observed"):
        updated["phase"] = "WAITING_FOR_INITIAL_COLOUR"
        return updated, False

    if colour == reversal:
        count = int(updated.get("reversal_confirmation_scans") or 0) + 1
        updated["reversal_confirmation_scans"] = count
        updated["phase"] = "REVERSAL_CONFIRMATION"
        confirmed = bool(buffer_confirmed) or count >= max(int(required_scans), 1)
        if confirmed:
            updated["previous_confirmed_colour"] = reversal
            updated["reversal_triggered"] = True
            updated["phase"] = "ENTRY_PENDING"
        return updated, confirmed

    updated["reversal_confirmation_scans"] = 0
    return updated, False


def advance_exit_confirmation(state, colour, trade_direction, required_scans=2):
    updated = dict(state or {})
    opposite = "RED" if trade_direction == BULLISH else "GREEN"
    if colour == opposite:
        count = int(updated.get("exit_confirmation_scans") or 0) + 1
        updated["exit_confirmation_scans"] = count
        return updated, count >= max(int(required_scans), 1)
    updated["exit_confirmation_scans"] = 0
    return updated, False


def nearest_target(direction, current_price, bollinger_middle, pivots, minimum_distance=0.0):
    current = float(current_price)
    candidates = []
    if bollinger_middle is not None:
        candidates.append(("BB_MIDDLE", float(bollinger_middle)))
    labels = ("P", "R1", "R2", "R3") if direction == BULLISH else ("P", "S1", "S2", "S3")
    candidates.extend((label, float(pivots[label])) for label in labels if pivots.get(label) is not None)
    minimum = max(float(minimum_distance), 0.0)
    if direction == BULLISH:
        valid = [
            (label, level)
            for label, level in candidates
            if level > current and level - current >= minimum
        ]
        ordered = sorted(valid, key=lambda item: item[1])
    else:
        valid = [
            (label, level)
            for label, level in candidates
            if level < current and current - level >= minimum
        ]
        ordered = sorted(valid, key=lambda item: item[1], reverse=True)
    if not ordered:
        return None
    label, level = ordered[0]
    distance = abs(level - current)
    return {"type": label, "level": round(level, 2), "distance": round(distance, 2)}


def nearest_continuation_target(
    direction,
    current_price,
    bollinger_bands_value,
    pivots,
    minimum_distance=0.0,
):
    """Return the nearest continuation objective beyond the accepted breakout."""
    current = float(current_price)
    bands = bollinger_bands_value or {}
    candidates = []
    band_name = "upper" if direction == BULLISH else "lower"
    if bands.get(band_name) is not None:
        candidates.append((f"BB_{band_name.upper()}", float(bands[band_name])))
    labels = ("P", "R1", "R2", "R3") if direction == BULLISH else ("P", "S1", "S2", "S3")
    candidates.extend((label, float(pivots[label])) for label in labels if pivots.get(label) is not None)
    minimum = max(float(minimum_distance), 0.0)
    if direction == BULLISH:
        valid = [
            (label, level)
            for label, level in candidates
            if level > current and level - current >= minimum
        ]
        ordered = sorted(valid, key=lambda item: item[1])
    else:
        valid = [
            (label, level)
            for label, level in candidates
            if level < current and current - level >= minimum
        ]
        ordered = sorted(valid, key=lambda item: item[1], reverse=True)
    if not ordered:
        return None
    label, level = ordered[0]
    return {
        "type": label,
        "level": round(level, 2),
        "distance": round(abs(level - current), 2),
    }


def score_gap_continuation(
    snapshot,
    breadth,
    option_chain,
    option_flow,
    institutional,
    minimum_score=75.0,
    minimum_volume_ratio=1.20,
    maximum_extension_range=0.75,
    retest_tolerance_range=0.10,
):
    """Score a completed-candle gap-acceptance setup on a 100-point scale."""
    gap_direction = ((snapshot or {}).get("gap") or {}).get("direction")
    transition = continuation_for_gap(gap_direction)
    result = {
        "allowed": False,
        "score": 0.0,
        "direction": transition.get("direction") if transition else NEUTRAL,
        "option_type": transition.get("option_type") if transition else None,
        "reasons": [],
        "blockers": [],
        "retest_confirmed": False,
    }
    if not transition:
        result["blockers"].append("opening gap is not directional")
        return result

    opening = (snapshot or {}).get("opening_range") or {}
    latest_five = (snapshot or {}).get("latest_completed_5m") or {}
    required = ("open", "high", "low", "close")
    if not opening.get("complete") or any(opening.get(key) is None for key in required):
        result["blockers"].append("first 15-minute opening range is incomplete")
        return result
    if not latest_five.get("complete") or any(latest_five.get(key) is None for key in required):
        result["blockers"].append("completed 5-minute confirmation candle is unavailable")
        return result

    direction = transition["direction"]
    bullish = direction == BULLISH
    previous_close = float((snapshot or {}).get("previous_close") or 0)
    today_open = float((snapshot or {}).get("today_open") or 0)
    opening_close = float(opening["close"])
    boundary = float(opening["high"] if bullish else opening["low"])
    five_close = float(latest_five["close"])
    opening_range = max(float(opening["high"]) - float(opening["low"]), 0.01)

    accepted = (
        opening_close > today_open and opening_close > previous_close
        if bullish
        else opening_close < today_open and opening_close < previous_close
    )
    if not accepted:
        result["blockers"].append("first 15-minute candle did not accept the opening gap")
    else:
        result["score"] += 25.0
        result["reasons"].append("15M gap acceptance=25/25")

    breakout = five_close > boundary if bullish else five_close < boundary
    if not breakout:
        result["blockers"].append("completed 5-minute candle has not broken the opening range")
    else:
        result["score"] += 15.0
        tolerance = opening_range * max(float(retest_tolerance_range), 0.0)
        retest = (
            float(latest_five["low"]) <= boundary + tolerance
            if bullish
            else float(latest_five["high"]) >= boundary - tolerance
        )
        result["retest_confirmed"] = bool(retest)
        if retest:
            result["score"] += 10.0
            result["reasons"].append("5M opening-range breakout/retest=25/25")
        else:
            result["reasons"].append("5M opening-range breakout without retest=15/25")

    extension = max(five_close - boundary, 0.0) if bullish else max(boundary - five_close, 0.0)
    extension_ratio = extension / opening_range
    result["extension_range_multiple"] = round(extension_ratio, 3)
    if extension_ratio > max(float(maximum_extension_range), 0.0):
        result["blockers"].append(
            f"entry is extended {extension_ratio:.2f} opening ranges beyond the breakout"
        )

    desired = direction
    breadth = breadth or {}
    breadth_bias = str(breadth.get("bias") or NEUTRAL).upper()
    breadth_confidence = str(breadth.get("confidence") or "LOW").upper()
    if breadth_bias == desired:
        breadth_points = {"HIGH": 20.0, "MEDIUM": 16.0, "LOW": 8.0}.get(
            breadth_confidence, 8.0
        )
    elif breadth_bias == NEUTRAL:
        breadth_points = 5.0
    else:
        breadth_points = 0.0
        if breadth_confidence in {"MEDIUM", "HIGH"}:
            result["blockers"].append("constituent breadth strongly opposes the gap direction")
    result["score"] += breadth_points
    result["reasons"].append(f"constituent breadth={breadth_points:.1f}/20")

    flow = option_flow or {}
    flow_close = float(flow.get("close") or 0)
    flow_vwap = float(flow.get("vwap") or 0)
    flow_slope = float(flow.get("vwap_slope") or 0)
    volume_ratio = float(flow.get("volume_ratio") or 0)
    flow_aligned = flow_close > 0 and flow_vwap > 0 and flow_close >= flow_vwap and flow_slope >= 0
    volume_aligned = bool(flow.get("volume_confirmed")) and volume_ratio >= float(minimum_volume_ratio)
    if not flow_aligned:
        result["blockers"].append("near-expiry ATM option is not above a flat/rising VWAP")
    if not volume_aligned:
        result["blockers"].append(
            f"near-expiry ATM option volume ratio {volume_ratio:.2f} is below {float(minimum_volume_ratio):.2f}"
        )
    if flow_aligned and volume_aligned:
        result["score"] += 15.0
        result["reasons"].append("ATM option VWAP/volume=15/15")

    chain = option_chain or {}
    chain_bias = str(chain.get("direction") or chain.get("bias") or NEUTRAL).upper()
    chain_confidence = str(chain.get("confidence") or "LOW").upper()
    if chain_bias == desired:
        chain_points = 10.0
    elif chain_bias == NEUTRAL:
        chain_points = 5.0
    else:
        chain_points = 0.0
        if chain_confidence == "HIGH":
            result["blockers"].append("HIGH-confidence option chain opposes continuation")
    result["score"] += chain_points
    result["reasons"].append(f"option chain={chain_points:.1f}/10")

    institutional = institutional or {}
    institutional_bias = str(institutional.get("bias") or NEUTRAL).upper()
    institutional_confidence = str(institutional.get("confidence") or "LOW").upper()
    if institutional_bias == desired:
        institutional_points = 5.0
    elif institutional_bias == NEUTRAL:
        institutional_points = 2.5
    else:
        institutional_points = 0.0
        if institutional_confidence in {"MEDIUM", "HIGH"}:
            result["blockers"].append("institutional footprint strongly opposes continuation")
    result["score"] += institutional_points
    result["reasons"].append(f"institutional context={institutional_points:.1f}/5")

    result["score"] = round(min(result["score"], 100.0), 1)
    if result["score"] < float(minimum_score):
        result["blockers"].append(
            f"continuation score {result['score']:.1f} is below {float(minimum_score):.1f}"
        )
    result["allowed"] = not result["blockers"]
    return result


def atm_strike(spot_price, interval=50):
    interval = int(interval)
    if interval <= 0:
        raise ValueError("strike interval must be positive")
    return int(round(float(spot_price) / interval) * interval)


def target_reached(direction, spot_price, target_level):
    if direction == BULLISH:
        return float(spot_price) >= float(target_level)
    return float(spot_price) <= float(target_level)


def within_entry_window(timestamp, start=time(9, 30), end=time(15, 25)):
    return start <= timestamp.time() <= end
