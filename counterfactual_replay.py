import argparse
import json
import re
from datetime import time
from pathlib import Path

import pandas as pd

from llm_decision import build_rule_based_fallback
from market_technicals import (
    convert_index_levels_to_option_premium,
    fetch_v3_historical_minutes,
    fetch_v3_intraday_minutes,
)
from post_market_review import parse_raw_json, read_analysis, replay_reason_category
from signal_score import weighted_alignment_score
from strategy_core import now_ist
from trade_bot import (
    MIN_SCORE_BY_SYMBOL,
    cautious_override_allowed,
    daily_max_loss,
    daily_profit_target,
    evaluate_trade_feasibility,
    find_index_option_instrument,
    load_env,
    max_lots_per_entry,
    option_capital_per_entry,
    option_levels_from_fill,
    order_quantity_for,
    risk_percentages,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_FILE = BASE_DIR / "logs" / "trade_bot.log"
IST = "Asia/Kolkata"
MARKET_EXIT = time(15, 15)
SIGNAL_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<clock>\d{2}:\d{2}:\d{2}) \| "
    r"(?P<symbol>NIFTY|BANKNIFTY) signal: "
    r"(?P<direction>BULLISH|BEARISH|NEUTRAL)"
)


def normalize_timestamp(value):
    timestamp = pd.to_datetime(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(IST)
    return timestamp.tz_convert(IST)


def signal_resets_from_log(date_text):
    events = {"NIFTY": [], "BANKNIFTY": []}
    if not LOG_FILE.exists():
        return events

    with LOG_FILE.open("r", errors="ignore") as handle:
        for line in handle:
            match = SIGNAL_PATTERN.search(line)
            if not match or match.group("date") != date_text:
                continue
            timestamp = normalize_timestamp(
                f"{match.group('date')} {match.group('clock')}"
            )
            events[match.group("symbol")].append(
                (timestamp, match.group("direction"))
            )
    return events


def reset_seen_between(events, start_time, end_time, blocked_direction):
    return any(
        start_time < timestamp <= end_time and direction != blocked_direction
        for timestamp, direction in events
    )


def prepare_historical_technicals(raw, direction, entry_price, transaction_type="BUY"):
    technicals = dict(raw.get("technicals", {}) or {})
    if transaction_type == "SELL":
        option_side = "PE" if direction == "BULLISH" else "CE"
    else:
        option_side = "CE" if direction == "BULLISH" else "PE"

    for key in ("two_hour", "fifteen_min", "five_min"):
        technicals[key] = convert_index_levels_to_option_premium(
            technicals.get(key, {}) or {},
            option_side=option_side,
            option_entry_price=entry_price,
            delta=0.5,
            transaction_type=transaction_type,
        )
    return technicals


def candles_for_date(instrument_key, date_text, cache):
    cache_key = (instrument_key, date_text)
    if cache_key in cache:
        return cache[cache_key]

    replay_date = pd.Timestamp(date_text).date()
    today = now_ist().date()
    if replay_date == today:
        candles = fetch_v3_intraday_minutes(instrument_key, minutes=5)
    else:
        lookback_days = max((today - replay_date).days + 2, 3)
        candles = fetch_v3_historical_minutes(
            instrument_key,
            minutes=5,
            lookback_days=lookback_days,
        )

    if candles.empty:
        cache[cache_key] = candles
        return candles

    candles = candles.copy()
    if candles.index.tz is None:
        candles.index = candles.index.tz_localize(IST)
    else:
        candles.index = candles.index.tz_convert(IST)
    candles = candles[candles.index.date == replay_date].sort_index()
    cache[cache_key] = candles
    return candles


def trailing_stop_after_favorable_move(
    entry,
    target,
    current_stop,
    favorable_price,
    transaction_type="BUY",
):
    is_short = str(transaction_type).upper() == "SELL"
    target_gap = (entry - target) if is_short else (target - entry)
    if target_gap <= 0:
        return current_stop

    progress = (
        (entry - favorable_price) / target_gap
        if is_short
        else (favorable_price - entry) / target_gap
    )
    new_stop = current_stop
    levels = []
    if progress >= 0.25:
        levels.append(round(entry * (1.01 if is_short else 0.99), 0))
    if progress >= 0.40:
        levels.append(round(entry, 0))
    if progress >= 0.60:
        levels.append(round(entry - target_gap * 0.30 if is_short else entry + target_gap * 0.30, 0))
    if progress >= 0.75:
        levels.append(round(entry - target_gap * 0.50 if is_short else entry + target_gap * 0.50, 0))
    if progress >= 0.90:
        levels.append(round(entry - target_gap * 0.70 if is_short else entry + target_gap * 0.70, 0))
    for level in levels:
        new_stop = min(new_stop, level) if is_short else max(new_stop, level)
    return new_stop


def simulate_trade(
    candles,
    signal_time,
    entry,
    target,
    stop,
    quantity,
    transaction_type="BUY",
):
    cutoff = signal_time.replace(
        hour=MARKET_EXIT.hour,
        minute=MARKET_EXIT.minute,
        second=0,
        microsecond=0,
    )
    future = candles[(candles.index > signal_time) & (candles.index <= cutoff)]
    if future.empty:
        return {
            "exit_time": None,
            "exit_price": None,
            "exit_reason": "NO_CANDLES",
            "gross_pnl": 0.0,
            "highest_price": None,
            "lowest_price": None,
        }

    current_stop = float(stop)
    highest = float(entry)
    lowest = float(entry)
    is_short = str(transaction_type).upper() == "SELL"

    def pnl(exit_price):
        move = (entry - exit_price) if is_short else (exit_price - entry)
        return round(move * quantity, 2)

    # Five-minute candles do not reveal whether high or low happened first.
    # Use the conservative assumption: an existing stop is hit before target
    # when both levels appear inside the same candle. A newly trailed stop only
    # applies from the next candle.
    for timestamp, candle in future.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        highest = max(highest, high)
        lowest = min(lowest, low)

        stop_hit = high >= current_stop if is_short else low <= current_stop
        target_hit = low <= target if is_short else high >= target
        if stop_hit:
            reason = "STOP_AND_TARGET_SAME_CANDLE" if target_hit else "STOP_LOSS"
            exit_price = current_stop
            return {
                "exit_time": timestamp.isoformat(),
                "exit_price": round(exit_price, 2),
                "exit_reason": reason,
                "gross_pnl": pnl(exit_price),
                "highest_price": round(highest, 2),
                "lowest_price": round(lowest, 2),
            }

        if target_hit:
            return {
                "exit_time": timestamp.isoformat(),
                "exit_price": round(target, 2),
                "exit_reason": "TARGET",
                "gross_pnl": pnl(target),
                "highest_price": round(highest, 2),
                "lowest_price": round(lowest, 2),
            }

        current_stop = trailing_stop_after_favorable_move(
            entry,
            target,
            current_stop,
            lowest if is_short else highest,
            transaction_type=transaction_type,
        )

    exit_time = future.index[-1]
    exit_price = float(future.iloc[-1]["close"])
    return {
        "exit_time": exit_time.isoformat(),
        "exit_price": round(exit_price, 2),
        "exit_reason": "SQUAREOFF",
        "gross_pnl": pnl(exit_price),
        "highest_price": round(highest, 2),
        "lowest_price": round(lowest, 2),
    }


def grade_candidate(symbol, raw):
    option_summary = dict(raw.get("option_summary", {}) or {})
    direction = option_summary.get("bias")
    transaction_type = str(option_summary.get("transaction_type") or "BUY").upper()
    confidence = option_summary.get("confidence")
    option_score = float(option_summary.get("score") or 0)
    entry = float(option_summary.get("entry_price") or 0)

    if direction not in {"BULLISH", "BEARISH"}:
        return None, "Signal is not directional"
    if confidence != "HIGH" or abs(option_score) < 4:
        return None, "Option-chain signal is not strong HIGH confidence"
    if entry <= 0:
        return None, "Missing expected option entry"

    technicals = prepare_historical_technicals(
        raw,
        direction,
        entry,
        transaction_type=transaction_type,
    )
    option_trend = option_summary.get("option_chain_trend", {}) or {}
    weighted = weighted_alignment_score(option_summary, technicals, option_trend)
    option_summary["weighted_alignment"] = weighted

    cautious = False
    if weighted["grade"] == "SKIP":
        if cautious_override_allowed(direction, weighted, technicals):
            cautious = True
            option_summary["cautious_override"] = True
        else:
            return None, f"Weighted score rejected: {weighted}"
    elif weighted["grade"] == "CAUTIOUS_TRADE":
        minimum = MIN_SCORE_BY_SYMBOL.get(symbol, 65)
        if float(weighted.get("score") or 0) < minimum:
            return None, f"Score {weighted.get('score')} is below {symbol} minimum {minimum}"
        cautious = True
        option_summary["cautious_trade"] = True

    target_percent, stop_percent = risk_percentages(cautious=cautious)
    target, stop = option_levels_from_fill(
        entry,
        target_percent,
        stop_percent,
        transaction_type=transaction_type,
    )
    feasibility = evaluate_trade_feasibility(
        direction,
        entry,
        target,
        stop,
        technicals,
        transaction_type=transaction_type,
    )
    technicals["trade_feasibility"] = feasibility
    option_summary["trade_feasibility"] = feasibility
    if not feasibility.get("allowed"):
        return None, "Entry feasibility rejected: " + "; ".join(feasibility.get("reasons", []))

    target = float(feasibility["adjusted_target_price"])
    option_summary["target_price"] = target
    option_summary["stop_loss_price"] = stop
    fallback = build_rule_based_fallback(option_summary, technicals)
    if not fallback.get("execute_trade"):
        return None, f"Deterministic decision rejected: {fallback.get('reason')}"

    return {
        "option_summary": option_summary,
        "technicals": technicals,
        "direction": direction,
        "transaction_type": transaction_type,
        "option_type": option_summary.get("option_type"),
        "entry_price": entry,
        "target_price": target,
        "stop_loss_price": float(stop),
        "weighted_score": weighted.get("score"),
        "weighted_grade": weighted.get("grade"),
        "cautious": cautious,
    }, "Qualified under revised rules"


def replay_day(date_text):
    analysis = read_analysis(date_text).sort_values("timestamp_dt")
    reset_events = signal_resets_from_log(date_text)
    candle_cache = {}
    decisions = []
    trades = []
    global_open_until = None
    loss_guards = {"NIFTY": None, "BANKNIFTY": None}

    for _, journal_row in analysis.iterrows():
        symbol = journal_row.get("symbol")
        if symbol not in {"NIFTY", "BANKNIFTY"}:
            continue

        signal_time = normalize_timestamp(journal_row.get("timestamp"))
        raw = parse_raw_json(journal_row)
        option_summary = raw.get("option_summary", {}) or {}
        direction = option_summary.get("bias")
        transaction_type = str(option_summary.get("transaction_type") or "BUY").upper()
        base = {
            "signal_time": signal_time.isoformat(),
            "symbol": symbol,
            "direction": direction,
            "transaction_type": transaction_type,
            "original_weighted_score": (option_summary.get("weighted_alignment", {}) or {}).get("score"),
            "original_weighted_grade": (option_summary.get("weighted_alignment", {}) or {}).get("grade"),
        }

        realized_before_signal = sum(
            float(trade.get("gross_pnl") or 0)
            for trade in trades
            if normalize_timestamp(trade["exit_time"]) <= signal_time
        )
        profit_limit = daily_profit_target()
        loss_limit = daily_max_loss()
        if profit_limit > 0 and realized_before_signal >= profit_limit:
            decisions.append(
                {
                    **base,
                    "decision": "PAPER_MODE",
                    "reason": (
                        f"Simulated realized P&L {realized_before_signal:.2f} reached "
                        f"daily profit target {profit_limit:.2f}"
                    ),
                }
            )
            continue
        if loss_limit > 0 and realized_before_signal <= -abs(loss_limit):
            decisions.append(
                {
                    **base,
                    "decision": "PAPER_MODE",
                    "reason": (
                        f"Simulated realized P&L {realized_before_signal:.2f} reached "
                        f"daily max loss {-abs(loss_limit):.2f}"
                    ),
                }
            )
            continue

        if global_open_until is not None and signal_time <= global_open_until:
            decisions.append(
                {
                    **base,
                    "decision": "SKIP",
                    "reason": "Existing global NIFTY/BANKNIFTY simulated position still open",
                }
            )
            continue

        guard = loss_guards[symbol]
        if guard and direction == guard["direction"]:
            reset_seen = reset_seen_between(
                reset_events.get(symbol, []),
                guard["exit_time"],
                signal_time,
                guard["direction"],
            )
            elapsed = (signal_time - guard["exit_time"]).total_seconds() / 60
            if not reset_seen or elapsed < 30:
                reason = "No signal reset after losing exit" if not reset_seen else f"Re-entry cooldown active ({elapsed:.1f}m/30m)"
                decisions.append({**base, "decision": "SKIP", "reason": reason})
                continue

        qualified, reason = grade_candidate(symbol, raw)
        if not qualified:
            decisions.append({**base, "decision": "SKIP", "reason": reason})
            continue

        try:
            option_type = option_summary.get("option_type")
            if option_type not in {"CE", "PE"}:
                if transaction_type == "SELL":
                    option_type = "PE" if direction == "BULLISH" else "CE"
                else:
                    option_type = "CE" if direction == "BULLISH" else "PE"
            instrument = find_index_option_instrument(
                symbol,
                option_summary.get("expiry"),
                option_summary.get("strike"),
                option_type,
            )
            candles = candles_for_date(
                instrument["instrument_key"],
                date_text,
                candle_cache,
            )
        except Exception as error:
            decisions.append({**base, "decision": "ERROR", "reason": str(error)})
            continue

        entry = qualified["entry_price"]
        target = qualified["target_price"]
        stop = qualified["stop_loss_price"]
        quantity = order_quantity_for(
            symbol,
            instrument,
            entry,
            stop,
            transaction_type=transaction_type,
        )
        if quantity <= 0:
            decisions.append(
                {
                    **base,
                    "decision": "SKIP",
                    "reason": (
                        "Configured option capital is insufficient for one whole lot"
                    ),
                }
            )
            continue

        outcome = simulate_trade(
            candles,
            signal_time,
            entry,
            target,
            stop,
            quantity,
            transaction_type=transaction_type,
        )
        if outcome["exit_time"] is None:
            decisions.append({**base, "decision": "ERROR", "reason": "No candles after signal"})
            continue

        exit_time = normalize_timestamp(outcome["exit_time"])
        global_open_until = exit_time
        trade = {
            **base,
            "decision": "TRADE",
            "reason": reason,
            "trading_symbol": instrument.get("trading_symbol"),
            "instrument_key": instrument.get("instrument_key"),
            "entry_price": entry,
            "target_price": target,
            "initial_stop_loss_price": stop,
            "quantity": quantity,
            "lots": quantity // int(instrument["lot_size"]),
            "configured_max_lots": max_lots_per_entry(),
            "option_capital_per_entry": option_capital_per_entry(),
            "revised_weighted_score": qualified["weighted_score"],
            "revised_weighted_grade": qualified["weighted_grade"],
            **outcome,
        }
        trades.append(trade)
        decisions.append(trade)

        if outcome["gross_pnl"] < 0 and outcome["exit_reason"] in {
            "STOP_LOSS",
            "STOP_AND_TARGET_SAME_CANDLE",
        }:
            loss_guards[symbol] = {
                "direction": direction,
                "exit_time": exit_time,
            }
        else:
            loss_guards[symbol] = None

    decisions_df = pd.DataFrame(decisions)
    trades_df = pd.DataFrame(trades)
    if not decisions_df.empty:
        decisions_df["rejection_category"] = decisions_df.apply(
            lambda row: replay_reason_category(row.get("reason"), row.get("decision")),
            axis=1,
        )
    total_pnl = round(float(trades_df["gross_pnl"].sum()), 2) if not trades_df.empty else 0.0
    wins = int((trades_df["gross_pnl"] > 0).sum()) if not trades_df.empty else 0
    losses = int((trades_df["gross_pnl"] < 0).sum()) if not trades_df.empty else 0
    summary = {
        "date": date_text,
        "analysis_rows": int(len(analysis)),
        "simulated_trades": int(len(trades_df)),
        "wins": wins,
        "losses": losses,
        "win_percent": round((wins / len(trades_df)) * 100, 1) if len(trades_df) else 0.0,
        "estimated_gross_pnl": total_pnl,
        "by_symbol": (
            trades_df.groupby("symbol")["gross_pnl"].agg(["count", "sum"]).round(2).to_dict("index")
            if not trades_df.empty
            else {}
        ),
        "decision_counts": (
            decisions_df["decision"].value_counts().to_dict()
            if not decisions_df.empty
            else {}
        ),
        "rejection_counts": (
            decisions_df.loc[
                decisions_df["decision"].astype(str).str.upper() != "TRADE",
                "rejection_category",
            ].value_counts().to_dict()
            if not decisions_df.empty
            else {}
        ),
        "assumptions": [
            "Uses saved signal snapshots and five-minute option candles.",
            "Uses expected signal entry, not order-book slippage.",
            "Brokerage, taxes, spread, and market impact are excluded.",
            "When target and stop occur in one candle, stop is assumed first.",
            "A newly trailed stop becomes active from the following candle.",
            "Cautious decisions use deterministic fallback; a fresh LLM call is not replayed.",
            "Daily limits use simulated realized P&L; intrabar combined unrealized P&L is not reconstructed.",
        ],
    }
    return decisions_df, trades_df, summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=now_ist().strftime("%Y-%m-%d"))
    args = parser.parse_args()

    load_env()
    DATA_DIR.mkdir(exist_ok=True)
    decisions, trades, summary = replay_day(args.date)

    decisions_file = DATA_DIR / f"counterfactual_decisions_{args.date}.csv"
    trades_file = DATA_DIR / f"counterfactual_trades_{args.date}.csv"
    summary_file = DATA_DIR / f"counterfactual_summary_{args.date}.json"
    decisions.to_csv(decisions_file, index=False)
    trades.to_csv(trades_file, index=False)
    summary_file.write_text(json.dumps(summary, indent=2, sort_keys=True))

    print(json.dumps(summary, indent=2, sort_keys=True))
    print()
    if trades.empty:
        print("No revised-rule trades were simulated.")
    else:
        columns = [
            "signal_time",
            "symbol",
            "trading_symbol",
            "entry_price",
            "target_price",
            "initial_stop_loss_price",
            "quantity",
            "exit_time",
            "exit_price",
            "exit_reason",
            "gross_pnl",
        ]
        print(trades[columns].to_string(index=False))
    print()
    print(f"Saved decisions: {decisions_file}")
    print(f"Saved trades: {trades_file}")
    print(f"Saved summary: {summary_file}")


if __name__ == "__main__":
    main()
