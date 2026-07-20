"""Post-market audit of BANKNIFTY trades vetoed by option-chain confidence.

The live bot is intentionally untouched. This module reconstructs technical
evidence from completed historical candles, selects a technical-only CE/PE
direction, and follows the next candles as a counterfactual research exercise.
"""

import gzip
import json
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from market_technicals import (
    INDEX_KEYS,
    UPSTOX_BASE,
    _parse_candles,
    add_indicators,
    analyze_latest,
    merge_candles,
    resample_ohlc,
    upstox_headers,
)
from trade_forensics import (
    excursion_metrics,
    first_level_touch,
    normalize_candles,
    normalize_timestamp,
    safe_float,
)


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
IST = ZoneInfo("Asia/Kolkata")

SIGNAL_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\|\s+"
    r"BANKNIFTY signal:\s+(?P<bias>[A-Z]+), confidence=(?P<confidence>[A-Z]+), "
    r"score=(?P<score>-?[\d.]+), strike=(?P<strike>[\d.]+), "
    r"expiry=(?P<expiry>\d{4}-\d{2}-\d{2})"
)
INSTITUTIONAL_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\|\s+"
    r"BANKNIFTY institutional footprint:\s+bias=(?P<bias>[A-Z]+) "
    r"confidence=(?P<confidence>[A-Z]+) score=(?P<score>-?[\d.]+)"
)


def _iter_log_lines(date_text):
    """Yield matching lines from current and rotated bot logs without duplicates."""
    seen = set()
    for path in sorted(LOG_DIR.glob("trade_bot.log*")):
        try:
            opener = gzip.open if path.suffix == ".gz" else open
            mode = "rt"
            with opener(path, mode, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    text = line.strip()
                    if text.startswith(date_text) and text not in seen:
                        seen.add(text)
                        yield text
        except (OSError, EOFError):
            continue


def read_option_chain_vetoes(date_text):
    """Read non-HIGH BANKNIFTY option-chain checks and nearby footprint logs."""
    signals = []
    footprints = []
    for line in _iter_log_lines(date_text):
        signal = SIGNAL_PATTERN.search(line)
        if signal and signal.group("confidence") != "HIGH":
            row = signal.groupdict()
            row["timestamp"] = normalize_timestamp(row["timestamp"])
            row["option_chain_score"] = safe_float(row.pop("score"), 0.0)
            row["strike"] = safe_float(row["strike"])
            row["option_chain_bias"] = row.pop("bias")
            row["option_chain_confidence"] = row.pop("confidence")
            signals.append(row)
            continue

        footprint = INSTITUTIONAL_PATTERN.search(line)
        if footprint:
            row = footprint.groupdict()
            row["timestamp"] = normalize_timestamp(row["timestamp"])
            row["score"] = safe_float(row["score"], 0.0)
            footprints.append(row)

    for signal in signals:
        eligible = [
            item
            for item in footprints
            if item["timestamp"] is not None
            and signal["timestamp"] is not None
            and item["timestamp"] >= signal["timestamp"]
            and item["timestamp"] - signal["timestamp"] <= pd.Timedelta(seconds=45)
        ]
        footprint = min(eligible, key=lambda item: item["timestamp"], default={})
        signal.update(
            {
                "institutional_bias": footprint.get("bias", "NEUTRAL"),
                "institutional_confidence": footprint.get("confidence", "LOW"),
                "institutional_score": footprint.get("score", 0.0),
            }
        )
    return pd.DataFrame(signals)


def _fetch_range(instrument_key, unit, interval, from_date, to_date, cache):
    cache_key = (instrument_key, unit, interval, str(from_date), str(to_date))
    if cache_key in cache:
        return cache[cache_key]

    frames = []
    today = datetime.now(IST).date()
    historical_to = min(to_date, today - timedelta(days=1)) if to_date >= today else to_date
    if from_date <= historical_to:
        url = (
            f"{UPSTOX_BASE}/v3/historical-candle/{instrument_key}/{unit}/{interval}/"
            f"{historical_to}/{from_date}"
        )
        response = requests.get(url, headers=upstox_headers(), timeout=30)
        if response.status_code >= 300:
            raise RuntimeError(
                f"Upstox historical candle API failed {response.status_code}: "
                f"{response.text[:300]}"
            )
        frames.append(_parse_candles(response.json()))

    if to_date >= today:
        url = f"{UPSTOX_BASE}/v3/historical-candle/intraday/{instrument_key}/{unit}/{interval}"
        response = requests.get(url, headers=upstox_headers(), timeout=30)
        if response.status_code >= 300:
            raise RuntimeError(
                f"Upstox intraday candle API failed {response.status_code}: "
                f"{response.text[:300]}"
            )
        frames.append(_parse_candles(response.json()))

    cache[cache_key] = normalize_candles(merge_candles(*frames))
    return cache[cache_key]


def load_index_history(date_text, cache):
    audit_date = datetime.fromisoformat(date_text).date()
    key = INDEX_KEYS["BANKNIFTY"]
    return {
        "five": _fetch_range(
            key, "minutes", 5, audit_date - timedelta(days=10), audit_date, cache
        ),
        "fifteen": _fetch_range(
            key, "minutes", 15, audit_date - timedelta(days=35), audit_date, cache
        ),
    }


def _completed(frame, timestamp, minutes):
    if frame is None or frame.empty or timestamp is None:
        return pd.DataFrame()
    return frame[(frame.index + pd.Timedelta(minutes=minutes)) <= timestamp].copy()


def technical_context_asof(history, timestamp):
    five = _completed(history["five"], timestamp, 5)
    fifteen = _completed(history["fifteen"], timestamp, 15)
    two_hour = resample_ohlc(
        fifteen,
        "2h",
        origin="start_day",
        offset="1h15min",
    )
    return {
        "five_min": analyze_latest(five, "5M"),
        "fifteen_min": analyze_latest(fifteen, "15M"),
        "two_hour": analyze_latest(two_hour, "2H"),
    }


def _confidence_multiplier(value):
    return {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}.get(str(value), 0.4)


def _bias_component(analysis, direction, weight):
    bias = (analysis or {}).get("bias")
    if bias == direction:
        alignment = 1.0
    elif bias == "NEUTRAL" or not bias:
        alignment = 0.25
    else:
        alignment = 0.0
    return weight * alignment * _confidence_multiplier((analysis or {}).get("confidence"))


def technical_only_score(direction, technicals, institutional=None, option_flow=None):
    """Score non-option-chain evidence on a stable 0-100 scale."""
    five = technicals.get("five_min", {}) or {}
    fifteen = technicals.get("fifteen_min", {}) or {}
    two = technicals.get("two_hour", {}) or {}
    institutional = institutional or {}
    option_flow = option_flow or {}

    fifteen_score = _bias_component(fifteen, direction, 30)
    five_bias_score = _bias_component(five, direction, 20)
    two_score = _bias_component(two, direction, 15)

    momentum = safe_float(five.get("momentum_score"), 0.0) or 0.0
    if direction == "BEARISH":
        momentum = -momentum
    momentum_score = 15 if momentum >= 4 else 11 if momentum >= 2 else 7 if momentum >= 1 else 3 if momentum >= 0 else 0

    institutional_bias = institutional.get("bias")
    if institutional_bias == direction:
        institutional_score = 10 * _confidence_multiplier(institutional.get("confidence"))
    elif institutional_bias == "NEUTRAL" or not institutional_bias:
        institutional_score = 2.5
    else:
        institutional_score = 0.0

    flow_bias = option_flow.get("bias")
    volume_confirmed = bool(option_flow.get("volume_confirmed"))
    if flow_bias == "BULLISH":
        flow_score = 10 if volume_confirmed else 7
    elif flow_bias == "NEUTRAL":
        flow_score = 5 if volume_confirmed else 2
    else:
        flow_score = 0

    components = {
        "15M": round(fifteen_score, 1),
        "5M_bias": round(five_bias_score, 1),
        "5M_momentum": round(momentum_score, 1),
        "2H": round(two_score, 1),
        "institutional": round(institutional_score, 1),
        "option_VWAP_volume": round(flow_score, 1),
    }
    return round(min(sum(components.values()), 100.0), 1), components


def choose_technical_direction(technicals, institutional):
    bullish, _ = technical_only_score("BULLISH", technicals, institutional)
    bearish, _ = technical_only_score("BEARISH", technicals, institutional)
    if abs(bullish - bearish) < 8 or max(bullish, bearish) < 45:
        return "NEUTRAL", bullish, bearish
    return ("BULLISH" if bullish > bearish else "BEARISH"), bullish, bearish


def option_flow_asof(candles, timestamp):
    completed = _completed(candles, timestamp, 5)
    if len(completed) < 20:
        return {
            "bias": "NEUTRAL",
            "confidence": "LOW",
            "volume_confirmed": False,
            "reasons": ["Not enough completed option candles"],
        }
    indicators = add_indicators(completed)
    valid = indicators.dropna(subset=["close"])
    last = valid.iloc[-1]
    close = safe_float(last.get("close"), 0.0) or 0.0
    volume = safe_float(last.get("volume"), 0.0) or 0.0
    volume_ma20 = safe_float(last.get("volume_ma20"), 0.0) or 0.0
    volume_ratio = volume / volume_ma20 if volume_ma20 else 0.0
    vwap = safe_float(last.get("vwap"))
    vwap_values = valid.get("vwap", pd.Series(dtype="float64")).dropna()
    previous_vwap = safe_float(vwap_values.iloc[-2]) if len(vwap_values) >= 2 else vwap
    slope = vwap - previous_vwap if vwap is not None and previous_vwap is not None else None
    volume_confirmed = bool(volume_ma20 > 0 and volume > volume_ma20)

    if vwap is not None and close > vwap and (slope is None or slope >= 0):
        bias = "BULLISH"
    elif vwap is not None and close < vwap and (slope is None or slope <= 0):
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    confidence = "HIGH" if bias != "NEUTRAL" and volume_confirmed else "MEDIUM" if bias != "NEUTRAL" else "LOW"
    return {
        "bias": bias,
        "confidence": confidence,
        "close": round(close, 2),
        "volume_ratio": round(volume_ratio, 2),
        "volume_confirmed": volume_confirmed,
        "vwap": round(vwap, 2) if vwap is not None else None,
        "vwap_slope": round(slope, 4) if slope is not None else None,
    }


def _hypothetical_pnl(outcome, entry, target, stop, final_close, quantity):
    if outcome == "TARGET_FIRST":
        exit_price = target
    elif outcome in {"STOP_FIRST", "AMBIGUOUS_SAME_CANDLE"}:
        exit_price = stop
    else:
        exit_price = final_close
    return round((float(exit_price) - float(entry)) * int(quantity), 2), round(float(exit_price), 2)


def _deduplicate_episodes(rows, minimum_score, forward_candles):
    if rows.empty:
        return rows.copy()
    eligible = rows[
        (pd.to_numeric(rows["technical_score"], errors="coerce") >= minimum_score)
        & rows["analysis_error"].fillna("").eq("")
    ].copy()
    if eligible.empty:
        return eligible
    eligible["_time"] = pd.to_datetime(eligible["signal_time"], errors="coerce", utc=True)
    eligible = eligible.sort_values("_time")
    selected = []
    active_until = None
    for _, row in eligible.iterrows():
        timestamp = row["_time"]
        if active_until is not None and timestamp <= active_until:
            continue
        selected.append(row)
        outcome_time = pd.to_datetime(row.get("outcome_time"), errors="coerce", utc=True)
        active_until = (
            outcome_time
            if pd.notna(outcome_time)
            else timestamp + pd.Timedelta(minutes=5 * forward_candles)
        )
    return pd.DataFrame(selected).drop(columns=["_time"], errors="ignore")


def run_banknifty_veto_audit(date_text, forward_candles=6, minimum_score=70):
    vetoes = read_option_chain_vetoes(date_text)
    if vetoes.empty:
        return pd.DataFrame(), pd.DataFrame(), {
            "date": date_text,
            "veto_checks": 0,
            "message": "No non-HIGH BANKNIFTY option-chain checks were found in the bot logs.",
        }

    cache = {}
    history = load_index_history(date_text, cache)
    audit_date = datetime.fromisoformat(date_text).date()
    rows = []

    from trade_bot import find_index_option_instrument, index_point_exit_settings

    exit_settings = index_point_exit_settings("BANKNIFTY")
    premium_target_move = exit_settings["target_points"] * exit_settings["delta"]
    premium_stop_move = exit_settings["stop_points"] * exit_settings["delta"]

    for _, veto in vetoes.iterrows():
        signal_time = veto.get("timestamp")
        institutional = {
            "bias": veto.get("institutional_bias"),
            "confidence": veto.get("institutional_confidence"),
            "score": veto.get("institutional_score"),
        }
        base = {
            "signal_time": signal_time.isoformat() if signal_time is not None else None,
            "option_chain_bias": veto.get("option_chain_bias"),
            "option_chain_confidence": veto.get("option_chain_confidence"),
            "option_chain_score": veto.get("option_chain_score"),
            "strike": veto.get("strike"),
            "expiry": veto.get("expiry"),
            "institutional_bias": institutional["bias"],
            "institutional_confidence": institutional["confidence"],
            "institutional_score": institutional["score"],
        }
        try:
            technicals = technical_context_asof(history, signal_time)
            direction, bullish_pre, bearish_pre = choose_technical_direction(
                technicals, institutional
            )
            base.update(
                {
                    "technical_direction": direction,
                    "bullish_pre_flow_score": bullish_pre,
                    "bearish_pre_flow_score": bearish_pre,
                    "five_min_bias": technicals["five_min"].get("bias"),
                    "five_min_confidence": technicals["five_min"].get("confidence"),
                    "five_min_momentum": technicals["five_min"].get("momentum_score"),
                    "fifteen_min_bias": technicals["fifteen_min"].get("bias"),
                    "fifteen_min_confidence": technicals["fifteen_min"].get("confidence"),
                    "two_hour_bias": technicals["two_hour"].get("bias"),
                    "two_hour_confidence": technicals["two_hour"].get("confidence"),
                }
            )
            if direction == "NEUTRAL":
                rows.append(
                    {
                        **base,
                        "technical_score": max(bullish_pre, bearish_pre),
                        "forward_outcome": "NO_TECHNICAL_DIRECTION",
                        "hypothetical_pnl": 0.0,
                        "analysis_error": "",
                    }
                )
                continue

            option_type = "CE" if direction == "BULLISH" else "PE"
            instrument = find_index_option_instrument(
                "BANKNIFTY", veto.get("expiry"), veto.get("strike"), option_type
            )
            option_candles = _fetch_range(
                instrument["instrument_key"],
                "minutes",
                5,
                audit_date - timedelta(days=10),
                audit_date,
                cache,
            )
            flow = option_flow_asof(option_candles, signal_time)
            final_score, components = technical_only_score(
                direction, technicals, institutional, flow
            )

            entry_start = signal_time.ceil("5min")
            forward = option_candles[option_candles.index >= entry_start].head(
                max(int(forward_candles), 1)
            )
            if forward.empty:
                raise RuntimeError("No option candles after the rejected check")
            entry_price = float(forward.iloc[0]["open"])
            target_price = round(entry_price + premium_target_move, 2)
            stop_price = round(max(entry_price - premium_stop_move, 0.05), 2)
            quantity = int(safe_float(instrument.get("lot_size"), 0) or 0)
            metrics = excursion_metrics(forward, entry_price, quantity, "BUY")
            outcome, outcome_time = first_level_touch(
                forward, target_price, stop_price, "BUY"
            )
            hypothetical_pnl, hypothetical_exit = _hypothetical_pnl(
                outcome,
                entry_price,
                target_price,
                stop_price,
                float(forward.iloc[-1]["close"]),
                quantity,
            )

            index_forward = history["five"][
                history["five"].index >= entry_start
            ].head(max(int(forward_candles), 1))
            index_entry = float(index_forward.iloc[0]["open"]) if not index_forward.empty else None
            index_exit = float(index_forward.iloc[-1]["close"]) if not index_forward.empty else None
            signed_move = None
            if index_entry is not None and index_exit is not None:
                raw_move = index_exit - index_entry
                signed_move = raw_move if direction == "BULLISH" else -raw_move

            rows.append(
                {
                    **base,
                    "technical_direction": direction,
                    "technical_score": final_score,
                    "score_components": json.dumps(components, sort_keys=True),
                    "trading_symbol": instrument.get("trading_symbol"),
                    "option_type": option_type,
                    "option_flow_bias": flow.get("bias"),
                    "option_flow_volume_ratio": flow.get("volume_ratio"),
                    "option_flow_volume_confirmed": flow.get("volume_confirmed"),
                    "option_flow_vwap": flow.get("vwap"),
                    "entry_price": round(entry_price, 2),
                    "target_price": target_price,
                    "stop_loss_price": stop_price,
                    "one_lot_quantity": quantity,
                    "forward_candles": int(len(forward)),
                    **metrics,
                    "forward_outcome": outcome,
                    "outcome_time": outcome_time,
                    "hypothetical_exit_price": hypothetical_exit,
                    "hypothetical_pnl": hypothetical_pnl,
                    "underlying_signed_move_points": round(signed_move, 2) if signed_move is not None else None,
                    "underlying_direction_correct": bool(signed_move > 0) if signed_move is not None else None,
                    "analysis_error": "",
                }
            )
        except Exception as error:
            rows.append({**base, "analysis_error": str(error)})

    observations = pd.DataFrame(rows)
    episodes = _deduplicate_episodes(observations, float(minimum_score), forward_candles)
    valid = observations[observations.get("analysis_error", "").fillna("").eq("")]
    strong = valid[pd.to_numeric(valid.get("technical_score"), errors="coerce") >= minimum_score]
    episode_pnl = pd.to_numeric(episodes.get("hypothetical_pnl"), errors="coerce").fillna(0) if not episodes.empty else pd.Series(dtype=float)
    episode_outcomes = episodes.get("forward_outcome", pd.Series(dtype="object"))
    resolved = episode_outcomes.isin(
        {"TARGET_FIRST", "STOP_FIRST", "AMBIGUOUS_SAME_CANDLE"}
    )
    wins = int((episode_outcomes == "TARGET_FIRST").sum())
    losses = int(episode_outcomes.isin({"STOP_FIRST", "AMBIGUOUS_SAME_CANDLE"}).sum())
    direction_results = (
        episodes.get(
            "underlying_direction_correct",
            pd.Series(index=episodes.index, dtype="object"),
        ).dropna()
        if not episodes.empty
        else pd.Series(dtype="object")
    )
    direction_correct = int(direction_results.astype(bool).sum())

    if len(episodes) < 5:
        verdict = "INSUFFICIENT_INDEPENDENT_SAMPLE"
    elif episode_pnl.sum() > 0 and wins > losses:
        verdict = "OPTION_CHAIN_VETO_MAY_BE_TOO_STRICT_FOR_STRONG_TECHNICAL_SETUPS"
    elif losses >= wins:
        verdict = "OPTION_CHAIN_VETO_WAS_PROTECTIVE_OR_TECHNICAL_EDGE_WAS_WEAK"
    else:
        verdict = "MIXED_EVIDENCE"

    summary = {
        "date": date_text,
        "veto_checks": int(len(vetoes)),
        "technically_directional_checks": int(
            valid.get("technical_direction", pd.Series(dtype="object"))
            .isin({"BULLISH", "BEARISH"})
            .sum()
        ),
        "strong_raw_checks": int(len(strong)),
        "independent_episodes": int(len(episodes)),
        "target_first": wins,
        "stop_first_or_ambiguous": losses,
        "no_edge_episodes": int((~resolved).sum()) if not episodes.empty else 0,
        "episode_win_percent": round(wins / (wins + losses) * 100, 2) if wins + losses else 0.0,
        "underlying_direction_correct": direction_correct,
        "underlying_direction_checks": int(len(direction_results)),
        "underlying_direction_accuracy": (
            round(direction_correct / len(direction_results) * 100, 2)
            if len(direction_results)
            else 0.0
        ),
        "one_lot_hypothetical_pnl": round(float(episode_pnl.sum()), 2),
        "minimum_technical_score": float(minimum_score),
        "forward_five_minute_candles": int(forward_candles),
        "target_index_points": exit_settings["target_points"],
        "stop_index_points": exit_settings["stop_points"],
        "delta_approximation": exit_settings["delta"],
        "verdict": verdict,
        "caveat": (
            "This is counterfactual research. Entries use the next five-minute candle open; "
            "same-candle target/stop collisions are counted as stops; repeated checks are "
            "de-duplicated into non-overlapping episodes. Brokerage, slippage, and taxes are excluded."
        ),
    }
    return observations, episodes, summary
