"""Deterministic, plain-language summaries for the forensic dashboard."""

import json
import re
from collections import Counter
from pathlib import Path

import pandas as pd


SIGNAL_PATTERN = re.compile(
    r"\|\s+(NIFTY|BANKNIFTY) signal:\s+"
    r"(BULLISH|BEARISH|NEUTRAL), confidence=(HIGH|MEDIUM|LOW), score=(-?[\d.]+)"
)
NO_TRADE_PATTERN = re.compile(r"\|\s+(NIFTY|BANKNIFTY) no trade:\s+(.+)$")
CANDIDATE_PATTERN = re.compile(
    r"\|\s+(NIFTY|BANKNIFTY) (?:BUY|SELL) candidate: "
    r"allowed=False score=[^ ]+ reason=(.+?)(?: contract=|$)"
)


def safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if pd.notna(result) else default
    except (TypeError, ValueError):
        return default


def _date_rows(path, timestamp_column, date_text):
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        frame = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if frame.empty or timestamp_column not in frame.columns:
        return pd.DataFrame()
    timestamps = pd.to_datetime(frame[timestamp_column], errors="coerce", utc=True)
    frame = frame.loc[timestamps.notna()].copy()
    frame["_date"] = timestamps.loc[timestamps.notna()].dt.tz_convert(
        "Asia/Kolkata"
    ).dt.date.astype(str)
    return frame[frame["_date"] == date_text].copy()


def read_analysis_rows(path, date_text):
    return _date_rows(path, "timestamp", date_text)


def read_log_lines(path, date_text):
    path = Path(path)
    if not path.exists():
        return []
    prefix = f"{date_text} "
    try:
        return [line.strip() for line in path.read_text(errors="replace").splitlines() if line.startswith(prefix)]
    except OSError:
        return []


def blocker_category(reason):
    text = str(reason or "").lower()
    if not text:
        return "OTHER"
    rules = [
        ("DAILY_OUTCOME_GUARD", ("daily first-outcome guard", "stop after first")),
        ("LOSS_REENTRY_GUARD", ("same-direction re-entry", "re-entry cooldown", "signal reset")),
        ("WEIGHTED_SCORE_LOW", ("weighted score", "score below symbol minimum", "score too low")),
        ("TECHNICAL_RR_LOW", ("reward/risk", "technical rr")),
        ("SIGNAL_NOT_HIGH", ("not directional high", "signal is not directional")),
        ("OPEN_POSITION", ("open position", "position active", "existing position")),
        ("OPTION_MARKET_QUALITY", ("option market quality", "spread", "liquidity")),
        ("ENTRY_FEASIBILITY", ("entry feasibility", "entry extension", "reachable technical target")),
        ("RISK_OR_FUNDS", ("risk limit", "insufficient funds", "margin")),
    ]
    for category, needles in rules:
        if any(needle in text for needle in needles):
            return category
    return "OTHER"


def parse_log_evidence(lines):
    signals = []
    blockers = []
    for line in lines:
        signal = SIGNAL_PATTERN.search(line)
        if signal:
            signals.append(
                {
                    "symbol": signal.group(1),
                    "direction": signal.group(2),
                    "confidence": signal.group(3),
                    "score": safe_float(signal.group(4)),
                }
            )
        no_trade = NO_TRADE_PATTERN.search(line)
        if no_trade:
            reason = no_trade.group(2)
            blockers.append(
                {
                    "symbol": no_trade.group(1),
                    "category": blocker_category(reason),
                    "reason": reason,
                }
            )
        candidate = CANDIDATE_PATTERN.search(line)
        if candidate:
            reason = candidate.group(2)
            blockers.append(
                {
                    "symbol": candidate.group(1),
                    "category": blocker_category(reason),
                    "reason": reason,
                }
            )
    return pd.DataFrame(signals), pd.DataFrame(blockers)


def analysis_signal_mix(analysis):
    if analysis.empty:
        return pd.DataFrame()
    columns = {
        "symbol": "symbol",
        "option_chain_bias": "direction",
        "option_chain_confidence": "confidence",
    }
    if not set(columns).issubset(analysis.columns):
        return pd.DataFrame()
    frame = analysis[list(columns)].rename(columns=columns).fillna("UNKNOWN")
    return (
        frame.groupby(["symbol", "direction", "confidence"], dropna=False)
        .size()
        .reset_index(name="checks")
        .sort_values(["symbol", "checks"], ascending=[True, False])
    )


def _top_counter_rows(frame, key, count_name="checks"):
    if frame.empty or key not in frame.columns:
        return []
    counts = Counter(frame[key].fillna("UNKNOWN").astype(str))
    return [{key: name, count_name: count} for name, count in counts.most_common()]


def _dominant_regime(signal_mix):
    messages = []
    if signal_mix.empty:
        return messages
    for symbol, part in signal_mix.groupby("symbol"):
        total = int(part["checks"].sum())
        if not total:
            continue
        row = part.sort_values("checks", ascending=False).iloc[0]
        share = float(row["checks"]) / total * 100
        if share >= 60:
            messages.append(
                f"{symbol} was predominantly {row['direction']} / {row['confidence']} "
                f"({int(row['checks'])} of {total} checks, {share:.0f}%)."
            )
        else:
            messages.append(
                f"{symbol} had a mixed signal regime; no direction/confidence pair exceeded 60% of checks."
            )
    return messages


def _trade_lessons(executed):
    messages = []
    if executed.empty:
        return ["No closed trades were available for entry/exit assessment."]

    realized = pd.to_numeric(executed.get("realized_pnl"), errors="coerce").fillna(0)
    peak = pd.to_numeric(executed.get("max_favorable_pnl"), errors="coerce").fillna(0)
    giveback = pd.to_numeric(
        executed.get("profit_given_back_from_peak"), errors="coerce"
    ).fillna(0)
    wins = int((realized > 0).sum())
    losses = int((realized < 0).sum())
    messages.append(
        f"Closed trades: {len(executed)} ({wins} profitable, {losses} losing); "
        f"realized gross P&L was Rs {realized.sum():,.2f}."
    )
    if peak.sum() > 0:
        capture = realized.clip(lower=0).sum() / peak.sum() * 100
        messages.append(
            f"Profitable P&L captured {capture:.0f}% of aggregate peak favorable P&L; "
            f"Rs {giveback.clip(lower=0).sum():,.2f} was given back from intratrade peaks."
        )
    recovery = executed.get(
        "recovered_to_entry_after_exit", pd.Series(False, index=executed.index)
    )
    recovery_count = int(recovery.fillna(False).astype(bool).sum())
    if recovery_count:
        messages.append(
            f"{recovery_count} exited trade(s) later recovered to entry in the selected window. "
            "This is diagnostic evidence, not proof that the original stop was wrong."
        )
    return messages


def _rejection_lessons(rejected):
    if rejected.empty:
        return ["No rejected directional signals were available for follow-through analysis."]
    outcomes = rejected.get(
        "forward_outcome", pd.Series("UNKNOWN", index=rejected.index)
    ).fillna("UNKNOWN")
    counts = outcomes.value_counts()
    missed = int(counts.get("MISSED_WINNER", 0))
    correct = int(counts.get("CORRECT_REJECT", 0))
    unclear = int(counts.get("NO_CLEAR_EDGE", 0))
    messages = [
        f"Rejected follow-through: {correct} correct rejects, {missed} missed winners, "
        f"and {unclear} no-clear-edge observations."
    ]
    if missed:
        messages.append(
            "Missed winners should be reviewed by blocker and time cluster; overlapping five-minute checks are not independent opportunities."
        )
    return messages


def effective_controls(env):
    keys = [
        "STOP_AFTER_FIRST_PROFIT_OR_LOSS",
        "STOP_AFTER_FIRST_PROFIT",
        "STOP_AFTER_FIRST_LOSS",
        "LOSS_REENTRY_MODE",
        "MIN_REENTRY_MINUTES",
    ]
    return {key: str(env.get(key, "NOT SET")) for key in keys}


def build_session_insights(date_text, analysis, executed, rejected, log_lines, env):
    log_signals, blockers = parse_log_evidence(log_lines)
    if not log_signals.empty:
        signal_mix = (
            log_signals.groupby(["symbol", "direction", "confidence"])
            .size()
            .reset_index(name="checks")
            .sort_values(["symbol", "checks"], ascending=[True, False])
        )
    else:
        signal_mix = analysis_signal_mix(analysis)

    blocker_rows = _top_counter_rows(blockers, "category")
    controls = effective_controls(env)
    narrative = []
    narrative.extend(_dominant_regime(signal_mix))
    if blocker_rows:
        top = blocker_rows[0]
        narrative.append(
            f"The most frequent recorded blocker was {top['category']} ({top['checks']} occurrence(s))."
        )
    narrative.extend(_trade_lessons(executed))
    narrative.extend(_rejection_lessons(rejected))

    combined_guard = controls["STOP_AFTER_FIRST_PROFIT_OR_LOSS"].lower() == "true"
    profit_guard = controls["STOP_AFTER_FIRST_PROFIT"].lower() == "true"
    loss_guard = controls["STOP_AFTER_FIRST_LOSS"].lower() == "true"
    if not combined_guard and not profit_guard and not loss_guard:
        narrative.append(
            "Effective controls permit new qualifying entries after both profitable and losing exits."
        )
    elif combined_guard:
        narrative.append("Effective controls stop new entries after the first closed profit or loss.")
    else:
        enabled = []
        if profit_guard:
            enabled.append("profit")
        if loss_guard:
            enabled.append("loss")
        narrative.append(
            "Effective controls stop new entries after the first " + " or ".join(enabled) + "."
        )

    priorities = [
        "Require evidence across multiple sessions before changing a production threshold or stop rule."
    ]
    if not executed.empty:
        realized = pd.to_numeric(executed.get("realized_pnl"), errors="coerce").fillna(0)
        giveback = pd.to_numeric(
            executed.get("profit_given_back_from_peak"), errors="coerce"
        ).fillna(0)
        if giveback.clip(lower=0).sum() > max(realized.clip(lower=0).sum(), 0):
            priorities.append(
                "Track profit capture over at least five sessions; peak giveback exceeded captured positive P&L today."
            )
    if blocker_rows:
        priorities.append(
            f"Audit {blocker_rows[0]['category']} first because it generated the most blocker log entries."
        )
    if not rejected.empty:
        outcomes = rejected.get(
            "forward_outcome", pd.Series("UNKNOWN", index=rejected.index)
        ).fillna("UNKNOWN")
        missed = int((outcomes == "MISSED_WINNER").sum())
        correct = int((outcomes == "CORRECT_REJECT").sum())
        if missed > correct and missed >= 3:
            priorities.append(
                "Review missed winners by blocker and time cluster, but de-duplicate overlapping checks before estimating opportunity."
            )

    return {
        "date": date_text,
        "narrative": narrative,
        "signal_mix": signal_mix.to_dict(orient="records"),
        "blockers": blocker_rows,
        "controls": controls,
        "review_priorities": priorities,
        "evidence": {
            "analysis_rows": int(len(analysis)),
            "signal_log_rows": int(len(log_signals)),
            "blocker_log_rows": int(len(blockers)),
            "executed_rows": int(len(executed)),
            "rejected_rows": int(len(rejected)),
        },
        "caveat": (
            "This summary describes saved evidence. Candle OHLC does not reveal tick order, "
            "and one session is not enough evidence for changing production rules."
        ),
    }
