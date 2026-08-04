"""Score-assisted manual NIFTY/BANKNIFTY option entry.

Examples:
    ./trade nifty 20
    ./trade bank nifty 40
    ./trade nifty put 20
    ./trade banknifty call 60 40 --yes

One points value means a symmetric target and stop. A second points value is
an explicit stop. The model chooses CALL or PUT; an explicitly supplied side
must agree with that suggestion.
"""

import argparse
import os
import sys

import trade_bot


SIDE_ALIASES = {
    "CALL": "CALL",
    "CE": "CALL",
    "PUT": "PUT",
    "PE": "PUT",
}


def parse_request(parts):
    values = [str(value).strip().upper() for value in parts if str(value).strip()]
    if not values:
        raise ValueError("Missing index and points")

    if values[:2] == ["BANK", "NIFTY"]:
        symbol = "BANKNIFTY"
        values = values[2:]
    elif values[0] in {"BANKNIFTY", "BANK-NIFTY"}:
        symbol = "BANKNIFTY"
        values = values[1:]
    elif values[0] == "NIFTY":
        symbol = "NIFTY"
        values = values[1:]
    else:
        raise ValueError("Index must be NIFTY, BANKNIFTY, or BANK NIFTY")

    requested_side = None
    if values and values[0] in SIDE_ALIASES:
        requested_side = SIDE_ALIASES[values.pop(0)]
    if not values or len(values) > 2:
        raise ValueError("Provide target points and optionally stop points")

    try:
        target_points = float(values[0])
        stop_points = float(values[1]) if len(values) == 2 else target_points
    except ValueError as error:
        raise ValueError("Target and stop must be positive numbers") from error
    if target_points <= 0 or stop_points <= 0:
        raise ValueError("Target and stop must be greater than zero")
    return symbol, requested_side, target_points, stop_points


def option_side(direction):
    return "CALL" if direction == "BULLISH" else "PUT"


def hard_market_quality_reason(candidate):
    quality = ((candidate.get("technicals") or {}).get("option_market_quality") or {})
    if quality and not quality.get("entry_allowed", True):
        reasons = quality.get("rejection_reasons") or ["option market quality failed"]
        return "; ".join(str(reason) for reason in reasons)
    return ""


def active_index_position():
    for symbol in trade_bot.SYMBOLS:
        state = trade_bot.read_state(symbol)
        if trade_bot.state_is_active(state):
            return state
    return None


def build_manual_candidate(candidate, target_points, stop_points, command_text):
    symbol = candidate["symbol"]
    direction = candidate["direction"]
    option_summary = candidate.get("option_summary") or {}
    instrument = candidate.get("instrument")
    entry_price = trade_bot.to_float(
        candidate.get("entry_price"),
        trade_bot.to_float(option_summary.get("entry_price")),
    )
    if not instrument or entry_price <= 0:
        raise RuntimeError("Suggested option contract or premium is unavailable")

    delta = trade_bot.index_point_exit_settings(symbol)["delta"]
    levels = trade_bot.option_levels_from_index_points(
        symbol,
        entry_price,
        target_points=target_points,
        stop_points=stop_points,
        delta=delta,
    )
    score = trade_bot.candidate_weighted_score(candidate)
    return {
        "allowed": True,
        "symbol": symbol,
        "direction": direction,
        "confidence": "MANUAL_SCORE_ASSISTED",
        "signal_score": score,
        "transaction_type": "BUY",
        "instrument": instrument,
        "entry_price": entry_price,
        "target_price": levels["target_price"],
        "stop_loss_price": levels["stop_loss_price"],
        "target_percent": None,
        "stop_percent": None,
        "target_points": target_points,
        "stop_points": stop_points,
        "option_delta_used": delta,
        "exit_profile": {},
        "profit_protection_enabled_for_trade": True,
        "technicals": candidate.get("technicals") or {},
        "option_summary": option_summary,
        "weighted": candidate.get("weighted") or {"score": score, "grade": "MANUAL"},
        "entry_score": candidate.get("entry_score"),
        "entry_minimum_score": trade_bot.configured_non_negative_float(
            "MANUAL_INDEX_MIN_SCORE", 60.0
        ),
        "score_cutoff_approved": True,
        "manual_override": True,
        "manual_command": command_text,
        "capital_override": "MAX",
        "structural_invalidation": {},
    }


def parser():
    value = argparse.ArgumentParser(
        description="Get a score-assisted CALL/PUT suggestion and optionally place it."
    )
    value.add_argument("parts", nargs="+", help="index [call|put] target [stop]")
    value.add_argument("--yes", action="store_true", help="place without confirmation prompt")
    value.add_argument(
        "--suggest-only", action="store_true", help="show the suggestion without placing"
    )
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    trade_bot.load_env()
    if not trade_bot.configured_bool("ENABLE_MANUAL_INDEX_TRADING", False):
        raise RuntimeError("ENABLE_MANUAL_INDEX_TRADING is not true in .env")
    if not trade_bot.market_window_ok():
        raise RuntimeError("Manual index entries are allowed only from 09:20 to 15:25 IST")

    symbol, requested_side, target_points, stop_points = parse_request(args.parts)
    existing = active_index_position()
    if existing:
        raise RuntimeError(
            f"An index bot position is already active: {existing.get('trading_symbol')}"
        )

    candidate = trade_bot.evaluate_symbol_buy_or_sell(
        symbol,
        allow_option_sell=False,
        include_rejected=True,
    )
    if not isinstance(candidate, dict):
        print(f"{symbol}: no directional CALL/PUT suggestion is available now.")
        return 2

    score = trade_bot.candidate_weighted_score(candidate)
    minimum = trade_bot.configured_non_negative_float("MANUAL_INDEX_MIN_SCORE", 60.0)
    direction = str(candidate.get("direction") or "").upper()
    if direction not in {"BULLISH", "BEARISH"}:
        print(f"{symbol}: no directional CALL/PUT suggestion is available now.")
        return 2
    suggested_side = option_side(direction)
    trading_symbol = (candidate.get("instrument") or {}).get("trading_symbol", "N/A")

    print(f"Suggestion: {symbol} {suggested_side}")
    print(f"Score: {score:.1f}/100 (minimum {minimum:.1f})")
    print(f"Contract: {trading_symbol}")
    print(f"Target/stop: {target_points:g}/{stop_points:g} underlying points")

    if requested_side and requested_side != suggested_side:
        print(
            f"Not placed: your requested {requested_side} conflicts with the "
            f"current {suggested_side} suggestion."
        )
        return 3
    if score < minimum:
        print("Not placed: suggestion score is below the manual-entry minimum.")
        return 3
    quality_reason = hard_market_quality_reason(candidate)
    if quality_reason:
        print(f"Not placed: option liquidity/quality failed: {quality_reason}")
        return 3

    chosen = build_manual_candidate(
        candidate,
        target_points,
        stop_points,
        " ".join(args.parts),
    )
    quantity = trade_bot.order_quantity_for(
        symbol,
        chosen["instrument"],
        chosen["entry_price"],
        chosen["stop_loss_price"],
        capital_override="MAX",
    )
    if quantity <= 0:
        print("Not placed: available funds or risk budget cannot fund one whole lot.")
        return 3
    lots = quantity // int(chosen["instrument"]["lot_size"])
    print(
        f"MAX allocation preview: {quantity} quantity ({lots} lots), "
        f"estimated premium Rs {chosen['entry_price']:.2f}"
    )

    if args.suggest_only:
        return 0
    if not args.yes:
        confirmation = input("Type YES to place this market order: ").strip().upper()
        if confirmation != "YES":
            print("Order cancelled.")
            return 1

    placed = trade_bot.execute_selected_candidate(chosen)
    print("Order submitted and handed to the normal monitor." if placed else "Order not placed.")
    return 0 if placed else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
