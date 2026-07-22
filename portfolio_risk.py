"""Pure portfolio-risk and correlation rules used by the live trade bot."""

BANK_STOCKS = {
    "AUBANK",
    "AXISBANK",
    "BANKBARODA",
    "CANBK",
    "FEDERALBNK",
    "HDFCBANK",
    "ICICIBANK",
    "IDFCFIRSTB",
    "INDUSINDBK",
    "KOTAKBANK",
    "PNB",
    "SBIN",
}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def state_is_active(state):
    return bool(
        state
        and state.get("instrument_key")
        and state.get("status")
        not in {"", "CLOSED", "REJECTED", "CANCELLED", "CANCELED"}
    )


def remaining_position_risk(state):
    """Return rupee risk from the current stop; a locked profit has zero risk."""
    if not state_is_active(state):
        return 0.0

    entry = _number(state.get("entry_price"))
    stop = _number(state.get("stop_loss_price"))
    quantity = abs(_number(state.get("quantity")))
    if entry <= 0 or stop <= 0 or quantity <= 0:
        return 0.0

    transaction = str(state.get("entry_transaction_type") or "BUY").upper()
    per_unit = stop - entry if transaction == "SELL" else entry - stop
    return round(max(per_unit, 0.0) * quantity, 2)


def position_has_locked_profit(state):
    if not state_is_active(state):
        return False
    entry = _number(state.get("entry_price"))
    stop = _number(state.get("stop_loss_price"))
    if entry <= 0 or stop <= 0:
        return False
    transaction = str(state.get("entry_transaction_type") or "BUY").upper()
    return stop <= entry if transaction == "SELL" else stop >= entry


def proposed_position_risk(entry_price, stop_loss_price, quantity, transaction_type="BUY"):
    entry = _number(entry_price)
    stop = _number(stop_loss_price)
    quantity = abs(_number(quantity))
    transaction = str(transaction_type or "BUY").upper()
    per_unit = stop - entry if transaction == "SELL" else entry - stop
    return round(max(per_unit, 0.0) * quantity, 2)


def total_open_risk(states):
    return round(sum(remaining_position_risk(state) for state in states), 2)


def aggregate_risk_decision(
    states,
    entry_price,
    stop_loss_price,
    quantity,
    transaction_type,
    risk_limit,
    buffer_percent=0.0,
):
    unknown = [
        state
        for state in states
        if state_is_active(state)
        and (
            _number(state.get("entry_price")) <= 0
            or _number(state.get("stop_loss_price")) <= 0
            or abs(_number(state.get("quantity"))) <= 0
        )
    ]
    current = total_open_risk(states)
    proposed = proposed_position_risk(
        entry_price,
        stop_loss_price,
        quantity,
        transaction_type,
    )
    buffer = proposed * max(_number(buffer_percent), 0.0) / 100.0
    projected = round(current + proposed + buffer, 2)
    limit = max(_number(risk_limit), 0.0)
    allowed = not unknown and proposed > 0 and (limit <= 0 or projected <= limit)
    reason = "portfolio risk accepted"
    if unknown:
        reason = "an active bot state has incomplete entry/stop/quantity risk data"
    elif proposed <= 0:
        reason = "proposed stop does not define positive rupee risk"
    elif limit > 0 and projected > limit:
        reason = (
            f"projected open risk Rs {projected:.2f} exceeds limit Rs {limit:.2f} "
            f"(current Rs {current:.2f} + proposed Rs {proposed:.2f} + "
            f"buffer Rs {buffer:.2f})"
        )
    return {
        "allowed": allowed,
        "reason": reason,
        "current_risk": current,
        "proposed_risk": proposed,
        "buffer_risk": round(buffer, 2),
        "projected_risk": projected,
        "risk_limit": limit,
    }


def _score(item):
    return _number(item.get("weighted_score"), _number(item.get("score")))


def correlation_decision(
    proposed,
    states,
    same_direction_index_min_score=80.0,
    max_same_direction_positions=2,
    ignore_profit_locked=True,
):
    """Reject concentrated entries while leaving existing positions untouched."""
    direction = str(proposed.get("direction") or "").upper()
    if direction not in {"BULLISH", "BEARISH"}:
        return {"allowed": False, "reason": "proposed direction is not tradeable"}

    active = [state for state in states if state_is_active(state)]
    if ignore_profit_locked:
        active = [state for state in active if not position_has_locked_profit(state)]

    same_direction = [
        state
        for state in active
        if str(state.get("direction") or "").upper() == direction
    ]
    proposed_symbol = str(proposed.get("symbol") or "").upper()

    # NIFTY and BANKNIFTY may coexist in one direction only when both are strong.
    if proposed_symbol in {"NIFTY", "BANKNIFTY"}:
        other_symbol = "BANKNIFTY" if proposed_symbol == "NIFTY" else "NIFTY"
        other = next(
            (
                state
                for state in same_direction
                if str(state.get("symbol") or "").upper() == other_symbol
            ),
            None,
        )
        required = max(_number(same_direction_index_min_score), 0.0)
        if other and min(_score(proposed), _score(other)) < required:
            return {
                "allowed": False,
                "reason": (
                    f"same-direction NIFTY/BANKNIFTY exposure requires both scores >= "
                    f"{required:.1f}; proposed={_score(proposed):.1f}, "
                    f"existing={_score(other):.1f}"
                ),
            }

    maximum = max(int(_number(max_same_direction_positions, 2)), 1)
    if len(same_direction) >= maximum:
        return {
            "allowed": False,
            "reason": (
                f"maximum {maximum} open at-risk {direction.lower()} positions already reached"
            ),
        }

    return {"allowed": True, "reason": "correlation exposure accepted"}
