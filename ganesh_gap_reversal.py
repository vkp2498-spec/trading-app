"""Pure rules for Ganesh's NIFTY opening-gap reversal strategy."""

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


def atm_strike(spot_price, interval=50):
    interval = int(interval)
    if interval <= 0:
        raise ValueError("strike interval must be positive")
    return int(round(float(spot_price) / interval) * interval)


def target_reached(direction, spot_price, target_level):
    if direction == BULLISH:
        return float(spot_price) >= float(target_level)
    return float(spot_price) <= float(target_level)


def within_entry_window(timestamp, start=time(9, 30), end=time(15, 15)):
    return start <= timestamp.time() <= end
