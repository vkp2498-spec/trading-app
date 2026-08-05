"""End-of-day excursion analysis for live and rejected option trades.

This module is read-only with respect to trading. It combines the structured
trade and analysis journals with Upstox intraday candles and writes forensic
CSV/JSON reports under ``data``.
"""

import argparse
import gzip
import json
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TRADE_FILE = DATA_DIR / "trade_history.csv"
ANALYSIS_FILE = DATA_DIR / "analysis_history.csv"
INSTRUMENT_FILE = BASE_DIR / "upstox_complete.json.gz"
ENV_FILE = BASE_DIR / ".env"
IST = ZoneInfo("Asia/Kolkata")


def safe_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def load_env_file():
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalize_timestamp(value):
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(IST)
    return timestamp.tz_convert(IST)


def normalize_candles(frame):
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
    candles = frame.copy().sort_index()
    if candles.index.tz is None:
        candles.index = candles.index.tz_localize(IST)
    else:
        candles.index = candles.index.tz_convert(IST)
    return candles


def excursion_metrics(candles, entry_price, quantity, transaction_type="BUY"):
    """Calculate favorable/adverse movement for one directional position."""
    entry = safe_float(entry_price, 0.0)
    qty = int(safe_float(quantity, 0) or 0)
    if candles is None or candles.empty or entry <= 0 or qty <= 0:
        return {
            "max_favorable_points": None,
            "max_adverse_points": None,
            "max_favorable_pnl": None,
            "max_adverse_pnl": None,
            "favorable_time": None,
            "adverse_time": None,
        }

    is_short = str(transaction_type).upper() == "SELL"
    if is_short:
        favorable_price = float(candles["low"].min())
        adverse_price = float(candles["high"].max())
        favorable_time = candles["low"].idxmin()
        adverse_time = candles["high"].idxmax()
        favorable_points = max(entry - favorable_price, 0.0)
        adverse_points = max(adverse_price - entry, 0.0)
    else:
        favorable_price = float(candles["high"].max())
        adverse_price = float(candles["low"].min())
        favorable_time = candles["high"].idxmax()
        adverse_time = candles["low"].idxmin()
        favorable_points = max(favorable_price - entry, 0.0)
        adverse_points = max(entry - adverse_price, 0.0)

    return {
        "max_favorable_price": round(favorable_price, 2),
        "max_adverse_price": round(adverse_price, 2),
        "max_favorable_points": round(favorable_points, 2),
        "max_adverse_points": round(adverse_points, 2),
        "max_favorable_pct": round(favorable_points / entry * 100, 2),
        "max_adverse_pct": round(adverse_points / entry * 100, 2),
        "max_favorable_pnl": round(favorable_points * qty, 2),
        "max_adverse_pnl": round(-adverse_points * qty, 2),
        "favorable_time": favorable_time.isoformat(),
        "adverse_time": adverse_time.isoformat(),
    }


def first_level_touch(candles, target_price, stop_price, transaction_type="BUY"):
    target = safe_float(target_price)
    stop = safe_float(stop_price)
    if candles is None or candles.empty or not target or not stop:
        return "LEVELS_UNAVAILABLE", None
    is_short = str(transaction_type).upper() == "SELL"
    for timestamp, candle in candles.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        target_hit = low <= target if is_short else high >= target
        stop_hit = high >= stop if is_short else low <= stop
        if target_hit and stop_hit:
            return "AMBIGUOUS_SAME_CANDLE", timestamp.isoformat()
        if target_hit:
            return "TARGET_FIRST", timestamp.isoformat()
        if stop_hit:
            return "STOP_FIRST", timestamp.isoformat()
    return "NEITHER", None


def post_exit_trade_diagnosis(
    candles,
    entry_price,
    exit_price,
    target_price,
    stop_price,
    realized_pnl,
    transaction_type="BUY",
):
    """Explain the session path after an executed trade was closed.

    The best post-exit move is deliberately measured only in candles completed
    before the first candle that touches the planned stop. A stop and favorable
    move inside the same one-minute candle have unknown tick order, so that
    candle is not credited as a confirmed favorable move.
    """
    entry = safe_float(entry_price)
    exit_value = safe_float(exit_price)
    target = safe_float(target_price)
    stop = safe_float(stop_price)
    pnl = safe_float(realized_pnl, 0.0) or 0.0
    empty_result = {
        "post_exit_session_candles": 0,
        "post_exit_best_price_before_stop": None,
        "post_exit_max_move_points_before_stop": None,
        "post_exit_stop_touched": None,
        "post_exit_stop_touch_time": None,
        "post_exit_target_touch_time": None,
        "extra_stop_points_to_target": None,
        "required_stop_price_to_target": None,
        "loss_path_classification": "DATA_UNAVAILABLE",
        "loss_path_note": "Post-exit one-minute candles are unavailable.",
    }
    if candles is None or candles.empty or not entry or not exit_value:
        return empty_result

    frame = normalize_candles(candles)
    is_short = str(transaction_type).upper() == "SELL"
    best_price = exit_value
    stop_touch_time = None

    for timestamp, candle in frame.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        stop_hit = bool(stop) and (high >= stop if is_short else low <= stop)
        if stop_hit:
            stop_touch_time = timestamp.isoformat()
            break
        best_price = min(best_price, low) if is_short else max(best_price, high)

    max_move = (
        max(exit_value - best_price, 0.0)
        if is_short
        else max(best_price - exit_value, 0.0)
    )
    result = {
        **empty_result,
        "post_exit_session_candles": int(len(frame)),
        "post_exit_best_price_before_stop": round(best_price, 2),
        "post_exit_max_move_points_before_stop": round(max_move, 2),
        "post_exit_stop_touched": stop_touch_time is not None if stop else None,
        "post_exit_stop_touch_time": stop_touch_time,
    }

    if pnl >= 0:
        result.update(
            {
                "loss_path_classification": "NOT_A_LOSS",
                "loss_path_note": "Winning or flat trade; stop-relaxation analysis is not applicable.",
            }
        )
        return result

    if not target or not stop:
        result.update(
            {
                "loss_path_classification": "LEVELS_UNAVAILABLE",
                "loss_path_note": "The original target or stop was not recorded.",
            }
        )
        return result

    adverse_price = entry
    stop_seen_before_target = False
    ambiguous_target_candle = False
    target_touch_time = None
    for timestamp, candle in frame.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        target_hit = low <= target if is_short else high >= target
        stop_hit = high >= stop if is_short else low <= stop
        adverse_price = max(adverse_price, high) if is_short else min(adverse_price, low)
        if target_hit:
            target_touch_time = timestamp.isoformat()
            ambiguous_target_candle = stop_hit and not stop_seen_before_target
            break
        if stop_hit:
            stop_seen_before_target = True

    result["post_exit_target_touch_time"] = target_touch_time
    if target_touch_time is None:
        result.update(
            {
                "loss_path_classification": "HARD_LOSS",
                "loss_path_note": (
                    "Hard loss: the original target was not reached after exit before market close; "
                    "the observed path does not support widening the stop."
                ),
            }
        )
        return result

    required_stop = adverse_price
    extra_stop = (
        max(required_stop - stop, 0.0)
        if is_short
        else max(stop - required_stop, 0.0)
    )
    result.update(
        {
            "extra_stop_points_to_target": round(extra_stop, 2),
            "required_stop_price_to_target": round(required_stop, 2),
        }
    )
    if ambiguous_target_candle:
        result.update(
            {
                "loss_path_classification": "AMBIGUOUS_SAME_CANDLE",
                "loss_path_note": (
                    "Target and stop were both inside the same one-minute candle; "
                    "tick order is unknown, so the required relaxation is only an upper-bound estimate."
                ),
            }
        )
    elif stop_seen_before_target or extra_stop > 0:
        result.update(
            {
                "loss_path_classification": "TARGET_AFTER_RELAXING_STOP",
                "loss_path_note": (
                    f"The target was reached later, but surviving the observed path required "
                    f"approximately {extra_stop:.2f} additional stop points."
                ),
            }
        )
    else:
        result.update(
            {
                "loss_path_classification": "TARGET_WITHOUT_RELAXATION",
                "loss_path_note": (
                    "The original target was reached after exit without the recorded stop being breached."
                ),
            }
        )
    return result


def _read_rows_for_date(path, timestamp_column, date_text):
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path)
    if frame.empty or timestamp_column not in frame.columns:
        return pd.DataFrame()
    timestamps = pd.to_datetime(frame[timestamp_column], errors="coerce", utc=True)
    frame = frame.loc[timestamps.notna()].copy()
    frame["_timestamp_ist"] = timestamps.loc[timestamps.notna()].dt.tz_convert(IST)
    return frame[frame["_timestamp_ist"].dt.date.astype(str) == date_text].copy()


def read_trades(date_text):
    return _read_rows_for_date(TRADE_FILE, "entry_time", date_text)


def read_rejected_signals(date_text):
    frame = _read_rows_for_date(ANALYSIS_FILE, "timestamp", date_text)
    if frame.empty:
        return frame
    execute = frame.get(
        "llm_execute_trade", pd.Series(index=frame.index, dtype="object")
    ).astype(str).str.lower()
    return frame[~execute.isin({"true", "1", "yes"})].copy()


def parse_raw_json(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def load_instruments():
    if not INSTRUMENT_FILE.exists():
        from trade_bot import ensure_instruments_file

        ensure_instruments_file()
    with gzip.open(INSTRUMENT_FILE, "rt", encoding="utf-8") as handle:
        rows = json.load(handle)
    return rows


def instrument_lookup(instruments):
    by_symbol = {}
    by_key = {}
    for row in instruments:
        key = row.get("instrument_key") or row.get("expired_instrument_key")
        trading_symbol = str(row.get("trading_symbol") or "").strip().upper()
        if key:
            by_key[key] = row
        if trading_symbol:
            by_symbol[trading_symbol] = row
    return by_symbol, by_key


def resolve_instrument(trading_symbol, option_summary, by_symbol, by_key):
    quality = (option_summary or {}).get("option_market_quality", {}) or {}
    instrument_key = quality.get("instrument_key")
    if instrument_key and instrument_key in by_key:
        return by_key[instrument_key]
    wanted_symbol = str(
        trading_symbol or (option_summary or {}).get("trading_symbol") or ""
    ).strip().upper()
    if wanted_symbol and wanted_symbol in by_symbol:
        return by_symbol[wanted_symbol]

    symbol = str((option_summary or {}).get("symbol") or "").upper()
    if symbol in {"NIFTY", "BANKNIFTY"}:
        from trade_bot import find_index_option_instrument

        return find_index_option_instrument(
            symbol,
            option_summary.get("expiry"),
            option_summary.get("strike"),
            option_summary.get("option_type"),
        )
    return None


def fetch_candles_for_date(instrument_key, date_text, minutes, cache):
    cache_key = (instrument_key, date_text, minutes)
    if cache_key in cache:
        return cache[cache_key]

    from market_technicals import (
        UPSTOX_BASE,
        _parse_candles,
        fetch_v3_intraday_minutes,
        upstox_headers,
    )

    today_text = datetime.now(IST).date().isoformat()
    if date_text == today_text:
        frame = fetch_v3_intraday_minutes(instrument_key, minutes=minutes)
    else:
        import requests

        url = (
            f"{UPSTOX_BASE}/v3/historical-candle/{instrument_key}/minutes/"
            f"{minutes}/{date_text}/{date_text}"
        )
        response = requests.get(url, headers=upstox_headers(), timeout=30)
        if response.status_code >= 300:
            raise RuntimeError(
                f"Upstox candle API failed {response.status_code}: {response.text[:300]}"
            )
        frame = _parse_candles(response.json())
    cache[cache_key] = normalize_candles(frame)
    return cache[cache_key]


def _candle_window(candles, start, end, minutes):
    if candles.empty or start is None or end is None:
        return candles.iloc[0:0]
    start_floor = start.floor(f"{minutes}min")
    end_floor = end.floor(f"{minutes}min")
    return candles[(candles.index >= start_floor) & (candles.index <= end_floor)].copy()


def analyze_executed_trades(trades, by_symbol, by_key, candle_cache, post_exit_candles):
    rows = []
    for _, trade in trades.iterrows():
        entry_time = normalize_timestamp(trade.get("entry_time"))
        exit_time = normalize_timestamp(trade.get("exit_time"))
        instrument = resolve_instrument(trade.get("trading_symbol"), {}, by_symbol, by_key)
        base = {
            "symbol": trade.get("symbol"),
            "trading_symbol": trade.get("trading_symbol"),
            "entry_time": trade.get("entry_time"),
            "exit_time": trade.get("exit_time"),
            "entry_price": safe_float(trade.get("entry_price")),
            "exit_price": safe_float(trade.get("exit_price")),
            "quantity": int(safe_float(trade.get("quantity"), 0) or 0),
            "target_price": safe_float(trade.get("target_price")),
            "stop_loss_price": safe_float(trade.get("stop_loss_price")),
            "original_stop_loss_price": safe_float(
                trade.get("original_stop_loss_price")
            ),
            "profit_protection_stage": int(
                safe_float(trade.get("profit_protection_stage"), 0) or 0
            ),
            "profit_booking_price": safe_float(trade.get("profit_booking_price")),
            "exit_reason": trade.get("exit_reason"),
            "realized_pnl": safe_float(trade.get("gross_pnl"), 0.0),
            "transaction_type": str(trade.get("transaction_type") or "BUY").upper(),
        }
        if not instrument or entry_time is None or exit_time is None:
            rows.append({**base, "analysis_error": "Instrument or timestamps unavailable"})
            continue
        try:
            one_minute = fetch_candles_for_date(
                instrument["instrument_key"], entry_time.date().isoformat(), 1, candle_cache
            )
            in_trade = _candle_window(one_minute, entry_time, exit_time, 1)
            metrics = excursion_metrics(
                in_trade, base["entry_price"], base["quantity"], base["transaction_type"]
            )
            level_outcome, level_time = first_level_touch(
                in_trade,
                base["target_price"],
                base["original_stop_loss_price"] or base["stop_loss_price"],
                base["transaction_type"],
            )

            five_minute = fetch_candles_for_date(
                instrument["instrument_key"], entry_time.date().isoformat(), 5, candle_cache
            )
            post_start = exit_time.ceil("5min")
            post_end = post_start + pd.Timedelta(minutes=5 * max(post_exit_candles - 1, 0))
            post_window = _candle_window(five_minute, post_start, post_end, 5)
            post_metrics = excursion_metrics(
                post_window, base["entry_price"], base["quantity"], base["transaction_type"]
            )
            post_outcome, post_outcome_time = first_level_touch(
                post_window,
                base["target_price"],
                base["original_stop_loss_price"] or base["stop_loss_price"],
                base["transaction_type"],
            )
            session_close = pd.Timestamp(
                year=exit_time.year,
                month=exit_time.month,
                day=exit_time.day,
                hour=15,
                minute=30,
                tz=IST,
            )
            post_exit_session = _candle_window(
                one_minute,
                exit_time.ceil("1min"),
                session_close,
                1,
            )
            diagnosis = post_exit_trade_diagnosis(
                post_exit_session,
                base["entry_price"],
                base["exit_price"],
                base["target_price"],
                base["original_stop_loss_price"] or base["stop_loss_price"],
                base["realized_pnl"],
                base["transaction_type"],
            )
            realized = base["realized_pnl"] or 0.0
            peak_pnl = metrics.get("max_favorable_pnl") or 0.0
            post_peak_pnl = post_metrics.get("max_favorable_pnl")
            rows.append(
                {
                    **base,
                    **metrics,
                    "planned_level_outcome_during_trade": level_outcome,
                    "planned_level_time_during_trade": level_time,
                    "stop_history_quality": (
                        "ORIGINAL_STOP_RECORDED"
                        if base["original_stop_loss_price"] is not None
                        else "FINAL_STOP_ONLY"
                    ),
                    "profit_given_back_from_peak": round(max(peak_pnl - realized, 0.0), 2),
                    "post_exit_candles": int(len(post_window)),
                    "post_exit_best_price": post_metrics.get("max_favorable_price"),
                    "post_exit_worst_price": post_metrics.get("max_adverse_price"),
                    "post_exit_best_pnl_from_entry": post_peak_pnl,
                    "post_exit_worst_pnl_from_entry": post_metrics.get("max_adverse_pnl"),
                    "post_exit_best_time": post_metrics.get("favorable_time"),
                    "post_exit_planned_level_outcome": post_outcome,
                    "post_exit_planned_level_time": post_outcome_time,
                    **diagnosis,
                    "recovered_to_entry_after_exit": bool(
                        realized < 0 and post_peak_pnl is not None and post_peak_pnl >= 0
                    ),
                    "post_exit_improved_vs_realized": bool(
                        post_peak_pnl is not None and post_peak_pnl > realized
                    ),
                    "analysis_error": None,
                }
            )
        except Exception as error:
            rows.append({**base, "analysis_error": str(error)})
    return pd.DataFrame(rows)


def analyze_rejected_signals(signals, by_symbol, by_key, candle_cache, forward_candles):
    rows = []
    for _, signal in signals.iterrows():
        raw = parse_raw_json(signal.get("raw_json"))
        option_summary = raw.get("option_summary", {}) or {}
        direction = option_summary.get("bias")
        if direction not in {"BULLISH", "BEARISH"}:
            continue
        signal_time = normalize_timestamp(signal.get("timestamp"))
        instrument = resolve_instrument(
            option_summary.get("trading_symbol"), option_summary, by_symbol, by_key
        )
        base = {
            "signal_time": signal.get("timestamp"),
            "symbol": signal.get("symbol"),
            "direction": direction,
            "trading_symbol": option_summary.get("trading_symbol"),
            "entry_price": safe_float(option_summary.get("entry_price")),
            "target_price": safe_float(option_summary.get("target_price")),
            "stop_loss_price": safe_float(option_summary.get("stop_loss_price")),
            "weighted_score": safe_float(
                (option_summary.get("weighted_alignment", {}) or {}).get("score")
            ),
            "weighted_grade": (
                option_summary.get("weighted_alignment", {}) or {}
            ).get("grade"),
            "rejection_reason": (raw.get("llm_decision", {}) or {}).get("reason"),
        }
        if not instrument or signal_time is None or not base["entry_price"]:
            rows.append({**base, "analysis_error": "Instrument, time, or entry unavailable"})
            continue
        quantity = int(safe_float(instrument.get("lot_size"), 0) or 0)
        try:
            candles = fetch_candles_for_date(
                instrument["instrument_key"], signal_time.date().isoformat(), 5, candle_cache
            )
            start = signal_time.floor("5min")
            end = start + pd.Timedelta(minutes=5 * max(forward_candles - 1, 0))
            forward = _candle_window(candles, start, end, 5)
            metrics = excursion_metrics(forward, base["entry_price"], quantity, "BUY")
            outcome, outcome_time = first_level_touch(
                forward, base["target_price"], base["stop_loss_price"], "BUY"
            )
            classification = {
                "TARGET_FIRST": "MISSED_WINNER",
                "STOP_FIRST": "CORRECT_REJECT",
                "AMBIGUOUS_SAME_CANDLE": "AMBIGUOUS",
                "NEITHER": "NO_CLEAR_EDGE",
                "LEVELS_UNAVAILABLE": "LEVELS_UNAVAILABLE",
            }.get(outcome, outcome)
            rows.append(
                {
                    **base,
                    "one_lot_quantity": quantity,
                    "forward_candles": int(len(forward)),
                    **metrics,
                    "forward_outcome": classification,
                    "forward_outcome_time": outcome_time,
                    "analysis_error": None,
                }
            )
        except Exception as error:
            rows.append({**base, "analysis_error": str(error)})
    return pd.DataFrame(rows)


def build_summary(date_text, executed, rejected, forward_candles, post_exit_candles):
    valid_executed = (
        executed[executed.get("analysis_error").isna()]
        if not executed.empty and "analysis_error" in executed
        else executed
    )
    valid_rejected = (
        rejected[rejected.get("analysis_error").isna()]
        if not rejected.empty and "analysis_error" in rejected
        else rejected
    )
    summary = {
        "date": date_text,
        "executed_trades": int(len(executed)),
        "executed_analyzed": int(len(valid_executed)),
        "realized_pnl": round(
            float(pd.to_numeric(executed.get("realized_pnl"), errors="coerce").fillna(0).sum()),
            2,
        ) if not executed.empty else 0.0,
        "sum_peak_unrealized_pnl": round(
            float(pd.to_numeric(valid_executed.get("max_favorable_pnl"), errors="coerce").fillna(0).sum()),
            2,
        ) if not valid_executed.empty else 0.0,
        "sum_profit_given_back": round(
            float(pd.to_numeric(valid_executed.get("profit_given_back_from_peak"), errors="coerce").fillna(0).sum()),
            2,
        ) if not valid_executed.empty else 0.0,
        "losing_trades_recovered_after_exit": int(
            valid_executed.get(
                "recovered_to_entry_after_exit",
                pd.Series(index=valid_executed.index, dtype="bool"),
            ).fillna(False).astype(bool).sum()
        ) if not valid_executed.empty else 0,
        "rejected_directional_signals_analyzed": int(len(valid_rejected)),
        "rejected_forward_outcomes": (
            valid_rejected.get("forward_outcome", pd.Series(dtype="object"))
            .fillna("UNKNOWN")
            .value_counts()
            .to_dict()
        ),
        "forward_window_five_minute_candles": forward_candles,
        "post_exit_window_five_minute_candles": post_exit_candles,
        "caveat": (
            "OHLC candles do not reveal tick order inside one candle. Excursions are hindsight "
            "diagnostics, not proof that an exit or rejection was wrong."
        ),
    }
    return summary


def _print_table(title, frame, columns):
    print(f"\n{title}")
    if frame.empty:
        print("No rows.")
        return
    available = [column for column in columns if column in frame.columns]
    print(frame[available].to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description="Analyze live trades and rejected signals")
    parser.add_argument("--date", default=datetime.now(IST).date().isoformat())
    parser.add_argument("--forward-candles", type=int, default=6)
    parser.add_argument("--post-exit-candles", type=int, default=6)
    args = parser.parse_args()

    load_env_file()
    DATA_DIR.mkdir(exist_ok=True)
    trades = read_trades(args.date)
    signals = read_rejected_signals(args.date)
    instruments = load_instruments()
    by_symbol, by_key = instrument_lookup(instruments)
    candle_cache = {}

    executed = analyze_executed_trades(
        trades, by_symbol, by_key, candle_cache, max(args.post_exit_candles, 1)
    )
    rejected = analyze_rejected_signals(
        signals, by_symbol, by_key, candle_cache, max(args.forward_candles, 1)
    )
    summary = build_summary(
        args.date,
        executed,
        rejected,
        max(args.forward_candles, 1),
        max(args.post_exit_candles, 1),
    )

    executed_file = DATA_DIR / f"executed_trade_forensics_{args.date}.csv"
    rejected_file = DATA_DIR / f"rejected_signal_forensics_{args.date}.csv"
    summary_file = DATA_DIR / f"trade_forensics_summary_{args.date}.json"
    executed.to_csv(executed_file, index=False)
    rejected.to_csv(rejected_file, index=False)
    summary_file.write_text(json.dumps(summary, indent=2, sort_keys=True))

    print(json.dumps(summary, indent=2, sort_keys=True))
    _print_table(
        "EXECUTED TRADE EXCURSIONS",
        executed,
        [
            "symbol", "trading_symbol", "realized_pnl", "max_favorable_pnl",
            "max_adverse_pnl", "profit_given_back_from_peak", "exit_reason",
            "post_exit_best_pnl_from_entry", "post_exit_planned_level_outcome",
        ],
    )
    _print_table(
        "REJECTED SIGNAL FORWARD OUTCOMES",
        rejected,
        [
            "signal_time", "symbol", "direction", "trading_symbol", "weighted_score",
            "forward_outcome", "max_favorable_pnl", "max_adverse_pnl",
        ],
    )
    print(f"\nSaved executed trades: {executed_file}")
    print(f"Saved rejected signals: {rejected_file}")
    print(f"Saved summary: {summary_file}")


if __name__ == "__main__":
    main()
