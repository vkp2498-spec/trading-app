import argparse
import json
import os
from pathlib import Path
from datetime import time

import pandas as pd

from market_technicals import fetch_v3_intraday_minutes
from trade_bot import find_index_option_instrument, load_env
from strategy_core import now_ist

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ANALYSIS_FILE = DATA_DIR / "analysis_history.csv"

SYMBOLS = ["NIFTY", "BANKNIFTY"]
MARKET_REVIEW_END = time(15, 29)


def safe_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default
    
def classify_losing_trade(row):
    pnl = float(row.get("gross_pnl", 0) or 0)
    entry = float(row.get("entry_price", 0) or 0)
    exit_price = float(row.get("exit_price", 0) or 0)
    target = float(row.get("target_price", 0) or 0)
    stop = float(row.get("stop_loss_price", 0) or 0)

    if pnl >= 0 or entry <= 0:
        return None

    is_short = str(row.get("transaction_type") or "BUY").upper() == "SELL"
    loss_pct = (
        ((entry - exit_price) / entry) * 100
        if is_short
        else ((exit_price - entry) / entry) * 100
    )
    target_gap = (entry - target) if is_short and target < entry else (target - entry if target > entry else 0)
    stop_gap = (stop - entry) if is_short and stop > entry else (entry - stop if stop > 0 else 0)

    notes = []

    if stop_gap / entry > 0.08:
        notes.append("Stop loss was wider than 8% of entry premium")

    if loss_pct <= -7:
        notes.append("Loss exceeded 7% of entry premium")

    if target_gap / entry >= 0.08:
        notes.append("Target required strong follow-through")

    if not notes:
        notes.append("Loss appears mainly due to failed follow-through")

    return {
        "loss_pct": round(loss_pct, 2),
        "classification": " | ".join(notes),
    }

def build_loss_review(trades_df, analysis_df):
    if trades_df.empty:
        return []

    losses = trades_df[trades_df["gross_pnl"].astype(float) < 0].copy()
    if losses.empty:
        return []

    reviews = []

    for _, trade in losses.iterrows():
        symbol = trade.get("symbol")
        entry_time = str(trade.get("entry_time", ""))

        matching = analysis_df[analysis_df["symbol"] == symbol].copy() if not analysis_df.empty else analysis_df

        entry_analysis = {}
        if not matching.empty and "created_at" in matching.columns:
            matching["created_at_dt"] = pd.to_datetime(matching["created_at"], errors="coerce")
            trade_entry_dt = pd.to_datetime(entry_time, errors="coerce")

            before = matching[matching["created_at_dt"] <= trade_entry_dt]
            if not before.empty:
                entry_analysis = before.sort_values("created_at_dt").iloc[-1].to_dict()

        loss_info = classify_losing_trade(trade)

        reviews.append({
            "symbol": symbol,
            "trading_symbol": trade.get("trading_symbol"),
            "entry_time": entry_time,
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "target_price": trade.get("target_price"),
            "stop_loss_price": trade.get("stop_loss_price"),
            "gross_pnl": trade.get("gross_pnl"),
            "exit_reason": trade.get("exit_reason"),
            "loss_pct": loss_info["loss_pct"] if loss_info else None,
            "loss_classification": loss_info["classification"] if loss_info else "",
            "entry_weighted_score": entry_analysis.get("weighted_score", ""),
            "entry_weighted_grade": entry_analysis.get("weighted_grade", ""),
            "entry_llm_reason": entry_analysis.get("llm_reason", ""),
            "entry_atm_option_bias": entry_analysis.get("atm_option_bias", ""),
            "entry_atm_option_above_vwap": entry_analysis.get("atm_option_above_vwap", ""),
            "suggestion": suggest_loss_improvement(trade, entry_analysis, loss_info),
        })

    return reviews

def suggest_loss_improvement(trade, analysis, loss_info):
    suggestions = []

    atm_bias = str(analysis.get("atm_option_bias", ""))
    above_vwap = str(analysis.get("atm_option_above_vwap", ""))

    if atm_bias == "BEARISH" or above_vwap == "False":
        suggestions.append("Avoid entry when ATM option flow is weak or below VWAP.")

    score = float(analysis.get("weighted_score") or 0)
    if score < 70 and trade.get("symbol") == "NIFTY":
        suggestions.append("For NIFTY, require weighted score >= 70.")

    exit_reason = str(trade.get("exit_reason", ""))
    if exit_reason == "STOP_LOSS":
        suggestions.append("Review whether trailing stop or sentiment exit could have reduced the loss earlier.")

    if loss_info and loss_info.get("loss_pct", 0) <= -7:
        suggestions.append("Consider tighter stop for cautious trades.")

    if not suggestions:
        suggestions.append("No obvious rule issue; likely normal losing trade within strategy risk.")

    return " ".join(suggestions)

def read_analysis(date_text):
    if not ANALYSIS_FILE.exists():
        raise RuntimeError(f"Missing file: {ANALYSIS_FILE}")

    df = pd.read_csv(ANALYSIS_FILE)
    if df.empty:
        return df

    df["timestamp_dt"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp_dt"])
    df["review_date"] = df["timestamp_dt"].dt.date.astype(str)
    return df[df["review_date"] == date_text].copy()


def parse_raw_json(row):
    try:
        return json.loads(row.get("raw_json") or "{}")
    except Exception:
        return {}


def normalize_ts(ts):
    out = pd.to_datetime(ts)
    if out.tzinfo is None:
        out = out.tz_localize("Asia/Kolkata")
    else:
        out = out.tz_convert("Asia/Kolkata")
    return out


def fetch_option_5m_after_signal(symbol, option_summary, signal_ts):
    direction = option_summary.get("bias")
    if direction not in {"BULLISH", "BEARISH"}:
        return pd.DataFrame(), None, "No directional option signal"

    option_type = option_summary.get("option_type") or (
        "CE" if direction == "BULLISH" else "PE"
    )
    expiry = option_summary.get("expiry")
    strike = option_summary.get("strike")

    instrument = find_index_option_instrument(symbol, expiry, strike, option_type)
    candles = fetch_v3_intraday_minutes(instrument["instrument_key"], minutes=5)

    if candles.empty:
        return candles, instrument, "No option candles returned"

    candles = candles.copy()
    if candles.index.tz is None:
        candles.index = candles.index.tz_localize("Asia/Kolkata")
    else:
        candles.index = candles.index.tz_convert("Asia/Kolkata")

    signal_ts = normalize_ts(signal_ts)
    end_ts = signal_ts.replace(
        hour=MARKET_REVIEW_END.hour,
        minute=MARKET_REVIEW_END.minute,
        second=0,
        microsecond=0,
    )

    candles = candles[(candles.index > signal_ts) & (candles.index <= end_ts)]
    return candles, instrument, None


def evaluate_missed_trade(
    candles,
    entry_price,
    target_price,
    stop_loss_price,
    transaction_type="BUY",
):
    if candles.empty:
        return {
            "missed_trade_outcome": "NO_CANDLES",
            "outcome_time": None,
            "max_high_after_signal": None,
            "min_low_after_signal": None,
            "max_favorable_pct": None,
            "max_adverse_pct": None,
        }

    entry = safe_float(entry_price)
    target = safe_float(target_price)
    stop = safe_float(stop_loss_price)

    if not entry or not target or not stop:
        return {
            "missed_trade_outcome": "MISSING_LEVELS",
            "outcome_time": None,
            "max_high_after_signal": round(float(candles["high"].max()), 2),
            "min_low_after_signal": round(float(candles["low"].min()), 2),
            "max_favorable_pct": None,
            "max_adverse_pct": None,
        }

    is_short = str(transaction_type).upper() == "SELL"
    outcome = "NO_CLEAR_EDGE"
    outcome_time = None

    for ts, candle in candles.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])

        target_hit = low <= target if is_short else high >= target
        stop_hit = high >= stop if is_short else low <= stop

        if target_hit and stop_hit:
            outcome = "AMBIGUOUS_TARGET_AND_STOP_SAME_CANDLE"
            outcome_time = ts.isoformat()
            break

        if target_hit:
            outcome = "MISSED_WINNER"
            outcome_time = ts.isoformat()
            break

        if stop_hit:
            outcome = "CORRECT_REJECT"
            outcome_time = ts.isoformat()
            break

    max_high = float(candles["high"].max())
    min_low = float(candles["low"].min())

    quantity = 65
    if entry >= 500:
        quantity = 30

    if is_short:
        expected_target_profit = round((entry - target) * quantity, 2)
        expected_stop_loss_loss = round((entry - stop) * quantity, 2)
        max_possible_profit = round((entry - min_low) * quantity, 2)
        max_possible_loss = round((entry - max_high) * quantity, 2)
        max_favorable_pct = ((entry - min_low) / entry) * 100
        max_adverse_pct = ((entry - max_high) / entry) * 100
    else:
        expected_target_profit = round((target - entry) * quantity, 2)
        expected_stop_loss_loss = round((stop - entry) * quantity, 2)
        max_possible_profit = round((max_high - entry) * quantity, 2)
        max_possible_loss = round((min_low - entry) * quantity, 2)
        max_favorable_pct = ((max_high - entry) / entry) * 100
        max_adverse_pct = ((min_low - entry) / entry) * 100

    if outcome == "MISSED_WINNER":
        missed_expected_profit = expected_target_profit
    else:
        missed_expected_profit = 0

    return {
        "missed_trade_outcome": outcome,
        "outcome_time": outcome_time,
        "max_high_after_signal": round(max_high, 2),
        "min_low_after_signal": round(min_low, 2),
        "max_favorable_pct": round(max_favorable_pct, 2),
        "max_adverse_pct": round(max_adverse_pct, 2),
        "expected_target_profit": expected_target_profit,
        "expected_stop_loss_loss": expected_stop_loss_loss,
        "max_possible_profit": max_possible_profit,
        "max_possible_loss": max_possible_loss,
        "missed_expected_profit": missed_expected_profit,
    }


def classify_blocker(raw):
    option_summary = raw.get("option_summary", {}) or {}
    technicals = raw.get("technicals", {}) or {}
    llm_decision = raw.get("llm_decision", {}) or {}

    weighted = option_summary.get("weighted_alignment", {}) or {}
    atm_flow = technicals.get("atm_option_flow", {}) or {}
    two = technicals.get("two_hour", {}) or technicals.get("four_hour", {}) or {}
    fifteen = technicals.get("fifteen_min", {}) or {}
    five = technicals.get("five_min", {}) or {}
    institutional = technicals.get("institutional_flow", {}) or {}

    direction = option_summary.get("bias")

    blockers = []

    if weighted.get("grade") == "SKIP":
        blockers.append("WEIGHTED_SCORE_LOW")

    if two.get("bias") in {"BULLISH", "BEARISH"} and direction in {"BULLISH", "BEARISH"}:
        if two.get("bias") != direction:
            blockers.append("2H_CONFLICT")

    if fifteen.get("bias") in {"NEUTRAL", None, ""}:
        blockers.append("15M_NEUTRAL_OR_WEAK")
    elif fifteen.get("bias") != direction:
        blockers.append("15M_CONFLICT")

    if five.get("bias") in {"BULLISH", "BEARISH"} and direction in {"BULLISH", "BEARISH"}:
        if five.get("bias") != direction:
            blockers.append("5M_CONFLICT")

    if (
        institutional.get("confidence") == "HIGH"
        and institutional.get("bias") in {"BULLISH", "BEARISH"}
        and institutional.get("bias") != direction
    ):
        blockers.append("INSTITUTIONAL_FLOW_CONFLICT")

    if atm_flow:
        if atm_flow.get("bias") in {"BEARISH", "NEUTRAL"}:
            blockers.append("ATM_OPTION_FLOW_WEAK")
        if not atm_flow.get("volume_confirmed"):
            blockers.append("ATM_OPTION_VOLUME_WEAK")

    reason = str(llm_decision.get("reason") or "").lower()
    if "conflict" in reason:
        blockers.append("LLM_CONFLICT_REJECTION")

    if not blockers:
        blockers.append("OTHER")

    return sorted(set(blockers))


def build_review(date_text):
    df = read_analysis(date_text)
    rows = []

    for _, row in df.iterrows():
        symbol = row.get("symbol")
        if symbol not in SYMBOLS:
            continue

        raw = parse_raw_json(row)
        option_summary = raw.get("option_summary", {}) or {}
        technicals = raw.get("technicals", {}) or {}
        llm_decision = raw.get("llm_decision", {}) or {}

        direction = option_summary.get("bias")
        confidence = option_summary.get("confidence")
        entry_price = option_summary.get("entry_price")
        target_price = option_summary.get("target_price")
        stop_loss_price = option_summary.get("stop_loss_price")
        weighted = option_summary.get("weighted_alignment", {}) or {}
        atm_flow = technicals.get("atm_option_flow", {}) or {}
        institutional = technicals.get("institutional_flow", {}) or {}

        signal_ts = row.get("timestamp")
        candles = pd.DataFrame()
        instrument = None
        fetch_error = None
        outcome = {}

        if direction in {"BULLISH", "BEARISH"}:
            try:
                candles, instrument, fetch_error = fetch_option_5m_after_signal(
                    symbol=symbol,
                    option_summary=option_summary,
                    signal_ts=signal_ts,
                )
                outcome = evaluate_missed_trade(
                    candles=candles,
                    entry_price=entry_price,
                    target_price=target_price,
                    stop_loss_price=stop_loss_price,
                    transaction_type=option_summary.get("transaction_type", "BUY"),
                )
            except Exception as e:
                fetch_error = str(e)
                outcome = {
                    "missed_trade_outcome": "ERROR",
                    "outcome_time": None,
                    "max_high_after_signal": None,
                    "min_low_after_signal": None,
                    "max_favorable_pct": None,
                    "max_adverse_pct": None,
                }
        else:
            outcome = {
                "missed_trade_outcome": "NOT_DIRECTIONAL",
                "outcome_time": None,
                "max_high_after_signal": None,
                "min_low_after_signal": None,
                "max_favorable_pct": None,
                "max_adverse_pct": None,
            }

        rows.append(
            {
                "timestamp": signal_ts,
                "symbol": symbol,
                "direction": direction,
                "option_confidence": confidence,
                "option_score": option_summary.get("score"),
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss_price": stop_loss_price,
                "weighted_score": weighted.get("score"),
                "weighted_grade": weighted.get("grade"),
                "two_hour_bias": row.get("two_hour_bias") or row.get("four_hour_bias"),
                "two_hour_confidence": row.get("two_hour_confidence") or row.get("four_hour_confidence"),
                "fifteen_min_bias": row.get("fifteen_min_bias"),
                "fifteen_min_confidence": row.get("fifteen_min_confidence"),
                "five_min_bias": (technicals.get("five_min", {}) or {}).get("bias"),
                "five_min_confidence": (technicals.get("five_min", {}) or {}).get("confidence"),
                "atm_option_flow_bias": atm_flow.get("bias"),
                "atm_option_close": atm_flow.get("close"),
                "atm_option_vwap": atm_flow.get("vwap"),
                "atm_option_volume_ratio": atm_flow.get("volume_ratio"),
                "atm_option_volume_confirmed": atm_flow.get("volume_confirmed"),
                "institutional_bias": institutional.get("bias"),
                "institutional_confidence": institutional.get("confidence"),
                "institutional_score": institutional.get("score"),
                "institutional_persistence": institutional.get("persistence_component"),
                "llm_execute_trade": llm_decision.get("execute_trade"),
                "llm_confidence": llm_decision.get("confidence"),
                "llm_reason": llm_decision.get("reason"),
                "blockers": ", ".join(classify_blocker(raw)),
                "instrument_key": instrument.get("instrument_key") if instrument else None,
                "trading_symbol": instrument.get("trading_symbol") if instrument else None,
                "candle_fetch_error": fetch_error,
                **outcome,
            }
        )

    return pd.DataFrame(rows)

def numeric_column(df, column_name):
    if column_name not in df.columns:
        return pd.Series([0] * len(df), index=df.index, dtype="float64")
    return pd.to_numeric(df[column_name], errors="coerce").fillna(0)


def replay_reason_category(reason, decision=None):
    """Convert a replay decision reason into a stable audit category."""
    decision_text = str(decision or "").upper()
    reason_text = str(reason or "").lower()

    if decision_text == "TRADE":
        return "TRADE"
    if decision_text == "PAPER_MODE":
        return "DAILY_LIMIT"
    if decision_text == "ERROR":
        return "DATA_ERROR"
    if "weighted score rejected" in reason_text or "below nifty minimum" in reason_text or "below banknifty minimum" in reason_text:
        return "WEIGHTED_SCORE_LOW"
    if "minimum reward/risk" in reason_text or "reward/risk" in reason_text:
        return "TECHNICAL_RR_TOO_LOW"
    if (
        "too far above analyzed" in reason_text
        or "above analyzed entry" in reason_text
        or "entry extension" in reason_text
    ):
        return "ENTRY_TOO_EXTENDED"
    if "no reachable technical target" in reason_text or "no reachable target" in reason_text:
        return "NO_REACHABLE_TARGET"
    if "existing simulated position" in reason_text:
        return "EXISTING_POSITION"
    if "signal reset" in reason_text or "cooldown" in reason_text:
        return "REENTRY_GUARD"
    if "one lot exceeds risk budget" in reason_text:
        return "RISK_BUDGET"
    if "signal is not directional" in reason_text:
        return "NOT_DIRECTIONAL"
    if "option-chain signal is not strong" in reason_text or "missing expected option entry" in reason_text:
        return "OPTION_SIGNAL_WEAK"
    if "deterministic decision rejected" in reason_text:
        return "DETERMINISTIC_REJECT"
    return "OTHER"


def _audit_timestamp(series):
    return pd.to_datetime(series, errors="coerce", utc=True)


def enrich_replay_decisions(decisions_df, review_df):
    """Attach rejection categories and observed post-signal outcomes to replay decisions."""
    if decisions_df is None or decisions_df.empty:
        return pd.DataFrame()

    decisions = decisions_df.copy()
    decisions["rejection_category"] = decisions.apply(
        lambda row: replay_reason_category(row.get("reason"), row.get("decision")),
        axis=1,
    )
    decisions["_audit_time"] = _audit_timestamp(decisions.get("signal_time"))

    if review_df is None or review_df.empty:
        return decisions.drop(columns=["_audit_time"], errors="ignore")

    review = review_df.copy()
    review["_audit_time"] = _audit_timestamp(review.get("timestamp"))
    outcome_columns = [
        "symbol",
        "_audit_time",
        "missed_trade_outcome",
        "missed_expected_profit",
        "max_favorable_pct",
        "max_adverse_pct",
        "candle_fetch_error",
    ]
    outcome_columns = [column for column in outcome_columns if column in review.columns]
    review = review[outcome_columns].drop_duplicates(["symbol", "_audit_time"], keep="last")
    decisions = decisions.merge(review, on=["symbol", "_audit_time"], how="left")
    return decisions.drop(columns=["_audit_time"], errors="ignore")


def build_rejection_quality(decisions_df, review_df):
    """Summarize whether each revised rejection rule avoided losses or missed winners."""
    decisions = enrich_replay_decisions(decisions_df, review_df)
    if decisions.empty:
        return pd.DataFrame()

    rejected = decisions[decisions["decision"].astype(str).str.upper() != "TRADE"].copy()
    if rejected.empty:
        return pd.DataFrame()

    rejected["missed_trade_outcome"] = rejected.get(
        "missed_trade_outcome", pd.Series(index=rejected.index, dtype="object")
    ).fillna("UNKNOWN")
    rejected["missed_expected_profit"] = numeric_column(
        rejected, "missed_expected_profit"
    )

    rows = []
    for category, part in rejected.groupby("rejection_category", dropna=False):
        correct = int((part["missed_trade_outcome"] == "CORRECT_REJECT").sum())
        missed = int((part["missed_trade_outcome"] == "MISSED_WINNER").sum())
        no_edge = int((part["missed_trade_outcome"] == "NO_CLEAR_EDGE").sum())
        decisive = correct + missed
        precision = round((correct / decisive) * 100, 1) if decisive else None
        rows.append(
            {
                "rule": str(category),
                "checks": int(len(part)),
                "correct_rejects": correct,
                "missed_winners": missed,
                "no_clear_edge": no_edge,
                "rejection_precision_pct": precision,
                "raw_missed_signal_value": round(float(part["missed_expected_profit"].sum()), 2),
            }
        )

    return pd.DataFrame(rows).sort_values(
        ["checks", "rule"], ascending=[False, True]
    ).reset_index(drop=True)


def build_decision_funnel(review_df, decisions_df):
    """Build a compact stage-by-stage view of the revised decision pipeline."""
    decisions = enrich_replay_decisions(decisions_df, review_df)
    total_checks = (
        int(len(decisions))
        if not decisions.empty
        else int(len(review_df)) if review_df is not None else 0
    )

    if review_df is None or review_df.empty:
        strong_signals = 0
    else:
        direction = review_df.get("direction", pd.Series(index=review_df.index, dtype="object"))
        confidence = review_df.get("option_confidence", pd.Series(index=review_df.index, dtype="object"))
        strong_signals = int(
            (direction.isin(["BULLISH", "BEARISH"]) & confidence.eq("HIGH")).sum()
        )

    if decisions.empty:
        passed_weighted = 0
        passed_feasibility = 0
        simulated_trades = 0
    else:
        initial_blocks = {"NOT_DIRECTIONAL", "OPTION_SIGNAL_WEAK", "WEIGHTED_SCORE_LOW"}
        feasibility_blocks = {
            "TECHNICAL_RR_TOO_LOW",
            "ENTRY_TOO_EXTENDED",
            "NO_REACHABLE_TARGET",
            "RISK_BUDGET",
        }
        eligible = decisions[~decisions["rejection_category"].isin(initial_blocks)]
        passed_weighted = int(len(eligible))
        passed_feasibility = int(
            (~eligible["rejection_category"].isin(feasibility_blocks)).sum()
        )
        simulated_trades = int(decisions["decision"].astype(str).str.upper().eq("TRADE").sum())

    return pd.DataFrame(
        [
            {"stage": "Analysis checks", "count": total_checks},
            {"stage": "Strong directional signals", "count": strong_signals},
            {"stage": "Passed weighted score", "count": passed_weighted},
            {"stage": "Passed entry feasibility", "count": passed_feasibility},
            {"stage": "Revised replay trades", "count": simulated_trades},
        ]
    )


def build_symbol_audit(review_df, decisions_df, replay_trades_df, actual_trades_df):
    decisions = enrich_replay_decisions(decisions_df, review_df)
    rows = []

    for symbol in SYMBOLS:
        review_part = review_df[review_df["symbol"] == symbol] if review_df is not None and not review_df.empty else pd.DataFrame()
        decision_part = decisions[decisions["symbol"] == symbol] if not decisions.empty else pd.DataFrame()
        replay_part = replay_trades_df[replay_trades_df["symbol"] == symbol] if replay_trades_df is not None and not replay_trades_df.empty else pd.DataFrame()
        actual_part = actual_trades_df[actual_trades_df["symbol"] == symbol] if actual_trades_df is not None and not actual_trades_df.empty else pd.DataFrame()

        rows.append(
            {
                "symbol": symbol,
                "checks": int(len(review_part)),
                "revised_trades": int(len(replay_part)),
                "actual_trades": int(len(actual_part)),
                "actual_pnl": round(float(numeric_column(actual_part, "gross_pnl").sum()), 2),
                "replay_pnl": round(float(numeric_column(replay_part, "gross_pnl").sum()), 2),
                "correct_rejects": int((decision_part.get("missed_trade_outcome") == "CORRECT_REJECT").sum()) if "missed_trade_outcome" in decision_part else 0,
                "missed_winners": int((decision_part.get("missed_trade_outcome") == "MISSED_WINNER").sum()) if "missed_trade_outcome" in decision_part else 0,
            }
        )
    return pd.DataFrame(rows)


def build_trade_forensics(actual_trades_df, review_df, decisions_df):
    """Match actual trades with the nearest saved signal and revised replay decision."""
    if actual_trades_df is None or actual_trades_df.empty:
        return pd.DataFrame()

    review = review_df.copy() if review_df is not None else pd.DataFrame()
    decisions = enrich_replay_decisions(decisions_df, review_df)
    if not review.empty:
        review["_match_time"] = _audit_timestamp(review.get("timestamp"))
    if not decisions.empty:
        decisions["_match_time"] = _audit_timestamp(decisions.get("signal_time"))

    rows = []
    for _, trade in actual_trades_df.iterrows():
        symbol = trade.get("symbol")
        entry_time = pd.to_datetime(trade.get("entry_time"), errors="coerce", utc=True)
        signal = {}
        revised = {}

        if pd.notna(entry_time) and not review.empty:
            candidates = review[(review["symbol"] == symbol) & (review["_match_time"] <= entry_time)].copy()
            if not candidates.empty:
                candidates["_gap"] = entry_time - candidates["_match_time"]
                nearest = candidates.sort_values("_gap").iloc[0]
                if nearest["_gap"] <= pd.Timedelta(minutes=15):
                    signal = nearest.to_dict()

        if pd.notna(entry_time) and not decisions.empty:
            candidates = decisions[(decisions["symbol"] == symbol) & (decisions["_match_time"] <= entry_time)].copy()
            if not candidates.empty:
                candidates["_gap"] = entry_time - candidates["_match_time"]
                nearest = candidates.sort_values("_gap").iloc[0]
                if nearest["_gap"] <= pd.Timedelta(minutes=15):
                    revised = nearest.to_dict()

        actual_entry = safe_float(trade.get("entry_price"))
        expected_entry = safe_float(signal.get("entry_price"))
        entry_slippage_pct = None
        if actual_entry and expected_entry:
            entry_slippage_pct = round(((actual_entry - expected_entry) / expected_entry) * 100, 2)

        rows.append(
            {
                "symbol": symbol,
                "trading_symbol": trade.get("trading_symbol"),
                "entry_time": trade.get("entry_time"),
                "exit_time": trade.get("exit_time"),
                "actual_entry": actual_entry,
                "expected_entry": expected_entry,
                "entry_slippage_pct": entry_slippage_pct,
                "exit_price": safe_float(trade.get("exit_price")),
                "target_price": safe_float(trade.get("target_price")),
                "stop_loss_price": safe_float(trade.get("stop_loss_price")),
                "gross_pnl": safe_float(trade.get("gross_pnl"), 0),
                "exit_reason": trade.get("exit_reason"),
                "entry_weighted_score": signal.get("weighted_score"),
                "entry_weighted_grade": signal.get("weighted_grade"),
                "atm_option_flow": signal.get("atm_option_flow_bias"),
                "atm_option_vwap": signal.get("atm_option_vwap"),
                "atm_option_volume_ratio": signal.get("atm_option_volume_ratio"),
                "revised_decision": revised.get("decision"),
                "revised_category": revised.get("rejection_category"),
                "revised_reason": revised.get("reason"),
                "analysis_matched": bool(signal),
            }
        )

    return pd.DataFrame(rows)


def summarize(review_df, date_text):
    if review_df.empty:
        return {
            "date": date_text,
            "message": "No analysis rows found",
        }

    directional = review_df[review_df["direction"].isin(["BULLISH", "BEARISH"])]
    rejected = review_df[review_df["llm_execute_trade"].astype(str).str.lower() != "true"]

    blocker_counts = {}
    for blockers in review_df["blockers"].dropna():
        for blocker in str(blockers).split(", "):
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1

    outcome_counts = (
        review_df["missed_trade_outcome"]
        .fillna("UNKNOWN")
        .value_counts()
        .to_dict()
    )

    missed_expected_profit_total = round(
    numeric_column(review_df, "missed_expected_profit").sum(),
    2,
    )

    max_possible_profit_total = round(
        numeric_column(review_df, "max_possible_profit").sum(),
        2,
    )

    by_symbol = {}
    for symbol, part in review_df.groupby("symbol"):
        weighted_values = numeric_column(part, "weighted_score")
        favorable_values = numeric_column(part, "max_favorable_pct")

        by_symbol[symbol] = {
            "total_checks": int(len(part)),
            "directional_signals": int(part["direction"].isin(["BULLISH", "BEARISH"]).sum())
            if "direction" in part.columns
            else 0,
            "missed_winners": int((part["missed_trade_outcome"] == "MISSED_WINNER").sum())
            if "missed_trade_outcome" in part.columns
            else 0,
            "correct_rejects": int((part["missed_trade_outcome"] == "CORRECT_REJECT").sum())
            if "missed_trade_outcome" in part.columns
            else 0,
            "no_clear_edge": int((part["missed_trade_outcome"] == "NO_CLEAR_EDGE").sum())
            if "missed_trade_outcome" in part.columns
            else 0,
            "avg_weighted_score": round(float(weighted_values.mean()), 2)
            if weighted_values.notna().any()
            else None,
            "best_favorable_pct": round(float(favorable_values.max()), 2)
            if favorable_values.notna().any()
            else None,
        }

    if "missed_trade_outcome" in review_df.columns:
        missed = review_df[review_df["missed_trade_outcome"] == "MISSED_WINNER"].copy()
    else:
        missed = pd.DataFrame()

    if not missed.empty:
        missed["max_favorable_pct_num"] = numeric_column(missed, "max_favorable_pct")
        missed = missed.sort_values("max_favorable_pct_num", ascending=False)

    return {
        "date": date_text,
        "total_analysis_rows": int(len(review_df)),
        "directional_signals": int(len(directional)),
        "rejected_signals": int(len(rejected)),
        "outcome_counts": outcome_counts,
        "blocker_counts": dict(sorted(blocker_counts.items(), key=lambda x: x[1], reverse=True)),
        "by_symbol": by_symbol,
        "missed_expected_profit_total": missed_expected_profit_total,
        "max_possible_profit_total": max_possible_profit_total,
        "top_missed_opportunities": missed[
            [
                "timestamp",
                "symbol",
                "direction",
                "trading_symbol",
                "entry_price",
                "target_price",
                "stop_loss_price",
                "weighted_score",
                "max_favorable_pct",
                "expected_target_profit",
                "max_possible_profit",
                "missed_expected_profit",
                "llm_reason",
            ]
        ].head(5).to_dict(orient="records")
        if not missed.empty
        else [],
        "interpretation_hint": (
            "MISSED_WINNER means target was touched before stop after the rejected signal. "
            "CORRECT_REJECT means stop was touched before target. "
            "NO_CLEAR_EDGE means neither target nor stop was touched before review cutoff."
        ),
    }


def ask_llm_for_insights(summary):
    if OpenAI is None:
        return "OpenAI package is not installed."

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "OPENAI_API_KEY not set."

    model = os.getenv("OPENAI_REVIEW_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4.1-nano")
    client = OpenAI(api_key=api_key)

    prompt = {
        "task": (
            "Analyze this post-market trading bot review. "
            "Say whether the bot was too conservative, correctly defensive, or missed material opportunities. "
            "Suggest only evidence-based changes. Do not overfit from one day."
        ),
        "summary": summary,
    }

    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "You are a strict trading-system reviewer. "
                    "You do not give financial advice. "
                    "You evaluate whether the automated rules behaved logically from the provided evidence."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt, sort_keys=True),
            },
        ],
    )

    return response.output_text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=now_ist().strftime("%Y-%m-%d"))
    parser.add_argument("--llm", action="store_true")
    args = parser.parse_args()

    load_env()
    DATA_DIR.mkdir(exist_ok=True)

    review_df = build_review(args.date)
    summary = summarize(review_df, args.date)

    csv_file = DATA_DIR / f"post_market_review_{args.date}.csv"
    summary_file = DATA_DIR / f"post_market_summary_{args.date}.json"
    llm_file = DATA_DIR / f"post_market_llm_insights_{args.date}.txt"

    review_df.to_csv(csv_file, index=False)
    summary_file.write_text(json.dumps(summary, indent=2, sort_keys=True))

    print(f"Saved review CSV: {csv_file}")
    print(f"Saved summary JSON: {summary_file}")
    print()
    print(json.dumps(summary, indent=2, sort_keys=True))

    if args.llm:
        print()
        print("Requesting LLM insights...")
        insights = ask_llm_for_insights(summary)
        llm_file.write_text(insights)
        print(f"Saved LLM insights: {llm_file}")
        print()
        print(insights)


if __name__ == "__main__":
    main()
