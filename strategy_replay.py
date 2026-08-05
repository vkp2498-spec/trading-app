import json
import math
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from backtest_data import IST, UpstoxBacktestData
from llm_decision import build_rule_based_fallback, get_llm_decision
from market_technicals import analyze_latest, convert_index_levels_to_option_premium, resample_ohlc
from signal_score import weighted_alignment_score
from stock_futures_scanner import NIFTY50_SYMBOLS
from strategy_core import option_chain_signal
from trade_bot import evaluate_trade_feasibility, option_levels_from_fill, risk_percentages


INDEXES = {
    "NIFTY": {"key": "NSE_INDEX|Nifty 50", "step": 50},
    "BANKNIFTY": {"key": "NSE_INDEX|Nifty Bank", "step": 100},
}
MARKET_START = time(9, 20)
LAST_ENTRY = time(15, 25)
SQUARE_OFF = time(15, 29)


def _float(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def capital_sized_option_quantity(entry_price, lot_size, capital):
    lot_size = max(int(_float(lot_size, 1)), 1)
    capital = max(_float(capital, 1), 1)
    if capital == 1:
        return lot_size, 1
    entry_price = _float(entry_price)
    if entry_price <= 0:
        return 0, 0
    lots = int(capital // (entry_price * lot_size))
    return lot_size * max(lots, 0), max(lots, 0)


def _expiry(value):
    return pd.Timestamp(value).date()


def _contract_type(row):
    value = str(row.get("instrument_type") or row.get("option_type") or "").upper()
    if value in {"CE", "CALL"} or value.endswith("CE"):
        return "CE"
    if value in {"PE", "PUT"} or value.endswith("PE"):
        return "PE"
    symbol = str(row.get("trading_symbol") or "").upper()
    return "CE" if " CE " in f" {symbol} " else "PE" if " PE " in f" {symbol} " else value


def _strike(row):
    return _float(row.get("strike_price") or row.get("strike"))


def _completed(frame, timestamp, minutes):
    if frame.empty:
        return frame
    return frame[frame.index + pd.Timedelta(minutes=minutes) <= timestamp]


def _session_vwap_flow(frame, timestamp, transaction_type="BUY"):
    valid = _completed(frame, timestamp, 5)
    if valid.empty:
        return {"bias": "NEUTRAL", "confidence": "LOW", "reasons": ["No completed option candles"]}
    day = timestamp.date()
    intraday = valid[valid.index.date == day].copy()
    history = valid.tail(40).copy()
    if intraday.empty:
        return {"bias": "NEUTRAL", "confidence": "LOW", "reasons": ["No current-session option candles"]}
    volume_ma = _float(history["volume"].tail(20).mean())
    last = intraday.iloc[-1]
    typical = (intraday["high"] + intraday["low"] + intraday["close"]) / 3
    cumulative_volume = intraday["volume"].cumsum()
    vwap_series = (typical * intraday["volume"]).cumsum() / cumulative_volume.replace(0, pd.NA)
    vwap = _float(vwap_series.iloc[-1], 0)
    previous_vwap = _float(vwap_series.iloc[-2], vwap) if len(vwap_series) >= 2 else vwap
    close = _float(last["close"])
    volume = _float(last["volume"])
    ratio = volume / volume_ma if volume_ma else 0
    raw_score = 0
    reasons = []
    if ratio > 1:
        raw_score += 1
        reasons.append("Option volume is above its 20-candle average")
    else:
        reasons.append("Option volume is not above its 20-candle average")
    if vwap and close > vwap and vwap >= previous_vwap:
        raw_score += 1
        reasons.append("Option premium is above a flat/rising session VWAP")
    elif vwap and close < vwap and vwap <= previous_vwap:
        raw_score -= 1
        reasons.append("Option premium is below a flat/falling session VWAP")
    normalized = -raw_score if str(transaction_type).upper() == "SELL" else raw_score
    return {
        "bias": "BULLISH" if normalized >= 1 else "BEARISH" if normalized <= -1 else "NEUTRAL",
        "raw_premium_bias": "BULLISH" if raw_score >= 1 else "BEARISH" if raw_score <= -1 else "NEUTRAL",
        "confidence": "HIGH" if abs(normalized) >= 2 else "MEDIUM" if abs(normalized) == 1 else "LOW",
        "score": normalized,
        "close": round(close, 2),
        "volume": round(volume, 2),
        "volume_ma20": round(volume_ma, 2),
        "volume_ratio": round(ratio, 2),
        "volume_confirmed": bool(ratio > 1),
        "vwap": round(vwap, 2) if vwap else None,
        "vwap_slope": round(vwap - previous_vwap, 4) if vwap else None,
        "entry_transaction_type": str(transaction_type).upper(),
        "reasons": reasons,
    }


def _technicals(spot_5m, timestamp):
    five = _completed(spot_5m, timestamp, 5)
    fifteen = resample_ohlc(five, "15min", origin="start_day", offset="15min")
    two = resample_ohlc(five, "2h", origin="start_day", offset="1h15min")
    return {
        "five_min": analyze_latest(five, "5M"),
        "fifteen_min": analyze_latest(fifteen, "15M"),
        "two_hour": analyze_latest(two, "2H"),
    }


def _chain_trend(history, direction):
    if len(history) < 3:
        return {"bias": "NEUTRAL", "score": 0, "reasons": ["Less than three reconstructed snapshots"]}
    first, last = history[0], history[-1]
    change = last - first
    bias = "BULLISH" if change >= 0.05 else "BEARISH" if change <= -0.05 else "NEUTRAL"
    return {
        "bias": bias,
        "score": 2 if bias == direction else -2 if bias != "NEUTRAL" else 0,
        "pcr_start": round(first, 4),
        "pcr_latest": round(last, 4),
        "pcr_change": round(change, 4),
        "reasons": [f"Reconstructed PCR OI moved from {first:.2f} to {last:.2f}"],
    }


def _reconstructed_institutional(direction, technicals, pcr_history):
    five = technicals.get("five_min", {})
    two = technicals.get("two_hour", {})
    score = 0
    if five.get("bias") == direction:
        score += 20
    elif five.get("bias") not in {direction, "NEUTRAL"}:
        score -= 20
    if two.get("bias") == direction:
        score += 15
    elif two.get("bias") not in {direction, "NEUTRAL"}:
        score -= 15
    if len(pcr_history) >= 3:
        change = pcr_history[-1] - pcr_history[0]
        score += 15 if (direction == "BULLISH" and change > 0.03) or (direction == "BEARISH" and change < -0.03) else -10
    bias = "BULLISH" if score >= 25 else "BEARISH" if score <= -25 else "NEUTRAL"
    return {
        "bias": bias,
        "confidence": "MEDIUM" if abs(score) >= 30 else "LOW",
        "score": score,
        "source": "RECONSTRUCTED_HISTORY",
        "reasons": ["Historical proxy uses futures/index momentum and reconstructed option OI; daily FII data is unavailable"],
    }


def _choose_expiry(symbol, expiries, session_date):
    valid = sorted(_expiry(value) for value in expiries if _expiry(value) >= session_date)
    if not valid:
        return None
    if symbol == "NIFTY":
        if session_date.weekday() == 2:
            return valid[0]
        return valid[1] if len(valid) >= 2 else None
    return valid[0]


def _latest_row(frame, timestamp, minutes=5):
    valid = _completed(frame, timestamp, minutes)
    return valid.iloc[-1] if not valid.empty else None


@dataclass
class Candidate:
    category: str
    symbol: str
    direction: str
    transaction_type: str
    contract: dict
    signal_time: pd.Timestamp
    expected_entry: float
    target: float
    stop: float
    score: float
    grade: str
    reason: str


class StrategyReplay:
    def __init__(self, data, from_date, to_date, progress=None, use_llm=False, include_stock_futures=True, slippage_bps=8, cost_per_order=25, portfolio_mode="live"):
        self.data = data
        self.from_date = _expiry(from_date)
        self.to_date = _expiry(to_date)
        self.progress = progress or (lambda message, **extra: None)
        self.use_llm = bool(use_llm)
        self.include_stock_futures = bool(include_stock_futures)
        self.slippage_bps = float(slippage_bps)
        self.cost_per_order = float(cost_per_order)
        self.portfolio_mode = portfolio_mode if portfolio_mode in {"live", "independent"} else "live"
        self.allow_simultaneous_index_positions = (
            os.getenv("REPLAY_ALLOW_SIMULTANEOUS_INDEX_POSITIONS", "false").lower()
            == "true"
        )
        self.option_capital_per_entry = max(
            _float(os.getenv("REPLAY_OPTION_CAPITAL_PER_ENTRY"), 1),
            1,
        )
        self.max_trades_per_day_per_index = max(
            int(_float(os.getenv("REPLAY_MAX_TRADES_PER_DAY_PER_INDEX"), 0)),
            0,
        )
        self.stop_after_first_win = (
            os.getenv("REPLAY_STOP_AFTER_FIRST_WIN", "false").lower() == "true"
        )
        self.stop_after_first_loss = (
            os.getenv("REPLAY_STOP_AFTER_FIRST_LOSS", "false").lower() == "true"
        )
        self.enable_trailing_stop = (
            os.getenv("REPLAY_ENABLE_TRAILING_STOP", "true").lower() == "true"
        )
        self.trail_stage_one_trigger = _float(
            os.getenv("REPLAY_TRAIL_STAGE_ONE_TRIGGER"), 60.0
        )
        self.trail_stage_one_lock = _float(
            os.getenv("REPLAY_TRAIL_STAGE_ONE_LOCK"), 20.0
        )
        self.trail_stage_two_trigger = _float(
            os.getenv("REPLAY_TRAIL_STAGE_TWO_TRIGGER"), 70.0
        )
        self.trail_stage_two_lock = _float(
            os.getenv("REPLAY_TRAIL_STAGE_TWO_LOCK"), 35.0
        )
        self.trail_runner_trigger = _float(
            os.getenv("REPLAY_TRAIL_RUNNER_TRIGGER"), 80.0
        )
        self.trail_runner_lock = _float(
            os.getenv("REPLAY_TRAIL_RUNNER_LOCK"), 55.0
        )
        self.daily_profit_target = max(
            _float(os.getenv("REPLAY_DAILY_PROFIT_TARGET"), 0.0), 0.0
        )
        self.daily_max_loss = max(
            _float(os.getenv("REPLAY_DAILY_MAX_LOSS"), 0.0), 0.0
        )
        self.max_consecutive_losses = max(
            int(_float(os.getenv("REPLAY_MAX_CONSECUTIVE_LOSSES"), 0)), 0
        )
        if not (
            0 <= self.trail_stage_one_lock < self.trail_stage_one_trigger
            < self.trail_stage_two_trigger < self.trail_runner_trigger <= 100
            and self.trail_stage_one_lock <= self.trail_stage_two_lock
            < self.trail_stage_two_trigger
            and self.trail_stage_two_lock <= self.trail_runner_lock
            < self.trail_runner_trigger
        ):
            raise ValueError("Invalid replay staged-trailing thresholds")
        self.candles = {}
        self.contracts = {}
        self.expiries = {}
        self.pcr = defaultdict(lambda: deque(maxlen=5))
        self.decisions = []
        self.trades = []
        self.coverage = defaultdict(int)

    def _candles(self, contract, interval="5minute", start=None, end=None, expired=None):
        key = contract if isinstance(contract, str) else (
            contract.get("expired_instrument_key") or contract.get("instrument_key")
        )
        cache_key = (key, interval, str(start or self.from_date), str(end or self.to_date))
        if cache_key not in self.candles:
            self.candles[cache_key] = self.data.candles(
                contract, interval, start or self.from_date, end or self.to_date, expired=expired
            )
        return self.candles[cache_key]

    def _option_contracts(self, symbol, expiry):
        key = (symbol, str(expiry))
        if key not in self.contracts:
            self.contracts[key] = self.data.get_option_contracts(INDEXES[symbol]["key"], expiry)
        return self.contracts[key]

    def _find_contract(self, contracts, strike, option_type):
        eligible = [row for row in contracts if _contract_type(row) == option_type and _strike(row) > 0]
        return min(eligible, key=lambda row: abs(_strike(row) - strike)) if eligible else None

    def _chain_snapshot(self, symbol, contracts, spot, timestamp):
        step = INDEXES[symbol]["step"]
        atm_strike = round(spot / step) * step
        selected = [row for row in contracts if abs(_strike(row) - atm_strike) <= step * 4]
        by_strike = defaultdict(dict)
        for contract in selected:
            option_type = _contract_type(contract)
            if option_type not in {"CE", "PE"}:
                continue
            frame = self._candles(contract, "5minute")
            row = _latest_row(frame, timestamp)
            if row is None:
                continue
            previous = _completed(frame, timestamp, 5)
            previous_oi = _float(previous.iloc[-2]["oi"]) if len(previous) >= 2 else _float(row.get("oi"))
            prefix = option_type
            by_strike[_strike(contract)].update(
                {
                    f"{prefix}_ltp": _float(row.get("close")),
                    f"{prefix}_oi": _float(row.get("oi")),
                    f"{prefix}_previous_oi": previous_oi,
                    f"{prefix}_change_oi": _float(row.get("oi")) - previous_oi,
                    f"{prefix}_volume": _float(row.get("volume")),
                    f"{prefix}_contract": contract,
                }
            )
        if not by_strike:
            return None
        chain = pd.DataFrame([{"strike": strike, "spot": spot, **values} for strike, values in by_strike.items()])
        chain = chain.fillna(0).sort_values("strike")
        atm = chain.iloc[(chain["strike"] - spot).abs().argsort()[:1]].iloc[0]
        pcr = chain["PE_oi"].sum() / chain["CE_oi"].sum() if chain["CE_oi"].sum() else 0
        return atm, chain, pcr

    def _candidate(self, symbol, timestamp, spot_frame, contracts, transaction_type):
        spot_row = _latest_row(spot_frame, timestamp)
        if spot_row is None:
            return None
        snapshot = self._chain_snapshot(symbol, contracts, _float(spot_row["close"]), timestamp)
        if not snapshot:
            return None
        atm, chain, pcr = snapshot
        direction, confidence, signal_score, reasons = option_chain_signal(atm)
        if direction == "NEUTRAL" or confidence != "HIGH":
            return None
        trend_key = (symbol, str(contracts[0].get("expiry", "")))
        self.pcr[trend_key].append(pcr)
        trend = _chain_trend(list(self.pcr[trend_key]), direction)
        option_type = (
            "CE" if direction == "BULLISH" else "PE"
            if transaction_type == "BUY"
            else "PE" if direction == "BULLISH" else "CE"
        )
        # Parentheses keep the BUY/SELL option mapping explicit.
        if transaction_type == "BUY":
            option_type = "CE" if direction == "BULLISH" else "PE"
        else:
            option_type = "PE" if direction == "BULLISH" else "CE"
        contract = self._find_contract(contracts, _float(atm["strike"]), option_type)
        if not contract:
            return None
        option_frame = self._candles(contract, "5minute")
        option_row = _latest_row(option_frame, timestamp)
        if option_row is None or _float(option_row["close"]) <= 0:
            return None
        entry = _float(option_row["close"])
        technicals = _technicals(spot_frame, timestamp)
        flow = _session_vwap_flow(option_frame, timestamp, transaction_type)
        technicals["atm_option_flow"] = flow
        technicals["institutional_flow"] = _reconstructed_institutional(direction, technicals, list(self.pcr[trend_key]))
        for key in ("five_min", "fifteen_min", "two_hour"):
            technicals[key] = convert_index_levels_to_option_premium(
                technicals[key], option_type, entry, delta=0.5, transaction_type=transaction_type
            )
        summary = {
            "bias": direction,
            "confidence": confidence,
            "score": signal_score,
            "strike": _float(atm["strike"]),
            "entry_price": entry,
            "transaction_type": transaction_type,
            "reasons": reasons,
        }
        weighted = weighted_alignment_score(summary, technicals, trend)
        summary["weighted_alignment"] = weighted
        cautious = weighted["grade"] == "CAUTIOUS_TRADE"
        if os.getenv("CAUSAL_CONFIRMATION_MODE", "false").lower() == "true":
            confirmation_failures = []
            five = technicals.get("five_min", {})
            fifteen = technicals.get("fifteen_min", {})
            if fifteen.get("bias") != direction:
                confirmation_failures.append(f"15M bias={fifteen.get('bias')} does not align")
            if five.get("bias") != direction:
                confirmation_failures.append(f"5M bias={five.get('bias')} does not align")
            completed_spot = _completed(spot_frame, timestamp, 5)
            if len(completed_spot) < 2:
                confirmation_failures.append("not enough completed 5M candles")
            else:
                current_spot = completed_spot.iloc[-1]
                previous_spot = completed_spot.iloc[-2]
                current_close = _float(current_spot.get("close"))
                previous_high = _float(previous_spot.get("high"))
                previous_low = _float(previous_spot.get("low"))
                breakout = (
                    current_close > previous_high
                    if direction == "BULLISH"
                    else current_close < previous_low
                )
                if not breakout:
                    confirmation_failures.append("latest completed 5M candle did not break the previous candle")
            # For a BUY, the option premium itself must be strengthening. This
            # is deliberately independent of whether the contract is a CE or PE.
            if transaction_type == "BUY" and _float(flow.get("score")) < 1:
                confirmation_failures.append("ATM option premium flow is not strengthening")
            summary["causal_confirmation"] = {
                "enabled": True,
                "passed": not confirmation_failures,
                "failures": confirmation_failures,
            }
            if confirmation_failures:
                self._record_decision(
                    timestamp, symbol, transaction_type, weighted, False,
                    "; ".join(confirmation_failures), contract,
                )
                return None
        if (
            os.getenv("REPLAY_REQUIRE_NORMAL_TRADE", "false").lower() == "true"
            and weighted.get("grade") != "TRADE"
        ):
            self._record_decision(
                timestamp,
                symbol,
                transaction_type,
                weighted,
                False,
                f"strict replay requires TRADE grade; received {weighted.get('grade')}",
                contract,
            )
            return None
        # Research scenarios can vary symbol-specific quality thresholds without
        # changing the live bot configuration. The defaults preserve the
        # existing replay behavior.
        default_minimum = 70 if symbol == "NIFTY" else 65
        minimum = _float(os.getenv(f"{symbol}_REPLAY_MIN_SCORE"), default_minimum)
        if weighted["score"] < minimum:
            self._record_decision(timestamp, symbol, transaction_type, weighted, False, "weighted score below symbol minimum")
            return None
        if transaction_type == "SELL":
            if os.getenv("ALLOW_NAKED_OPTION_SELLING", "false").lower() != "true":
                return None
            if weighted["score"] < _float(os.getenv("SHORT_MIN_WEIGHTED_SCORE"), 80):
                return None
            fifteen = technicals["fifteen_min"]
            five = technicals["five_min"]
            raw_support = flow.get("raw_premium_bias") == "BEARISH"
            if fifteen.get("bias") != direction or five.get("bias") != direction or not raw_support:
                return None
        target_percent, stop_percent = risk_percentages(cautious=cautious)
        target, stop = option_levels_from_fill(entry, target_percent, stop_percent, transaction_type)
        feasibility = evaluate_trade_feasibility(
            direction, entry, target, stop, technicals, transaction_type=transaction_type
        )
        if not feasibility.get("allowed"):
            self._record_decision(timestamp, symbol, transaction_type, weighted, False, "; ".join(feasibility.get("reasons") or []))
            return None
        target = _float(feasibility.get("adjusted_target_price"), target)
        if (
            transaction_type == "BUY"
            and os.getenv("REPLAY_FIXED_INDEX_POINT_EXITS", "false").lower() == "true"
        ):
            delta = max(_float(os.getenv("REPLAY_OPTION_DELTA"), 0.5), 0.01)
            target_points = max(
                _float(os.getenv(f"REPLAY_{symbol}_TARGET_POINTS"), 30 if symbol == "NIFTY" else 60),
                0,
            )
            stop_points = max(
                _float(os.getenv(f"REPLAY_{symbol}_STOP_POINTS"), 15 if symbol == "NIFTY" else 30),
                0,
            )
            if target_points <= 0 or stop_points <= 0:
                self._record_decision(
                    timestamp, symbol, transaction_type, weighted, False,
                    "fixed replay target and stop points must be positive", contract,
                )
                return None
            target = entry + target_points * delta
            stop = max(entry - stop_points * delta, 0.05)
            feasibility["fixed_index_point_exits"] = {
                "target_points": target_points,
                "stop_points": stop_points,
                "delta": delta,
            }
        summary["target_price"] = target
        summary["stop_loss_price"] = stop
        summary["technical_feasibility"] = feasibility
        decision = build_rule_based_fallback(summary, technicals)
        if self.use_llm and decision.get("execute_trade"):
            decision = get_llm_decision(symbol, summary, technicals)
        allowed = bool(decision.get("execute_trade"))
        self._record_decision(timestamp, symbol, transaction_type, weighted, allowed, decision.get("reason"), contract)
        if not allowed:
            return None
        return Candidate(
            category=f"{symbol}_OPTION_{transaction_type}", symbol=symbol, direction=direction,
            transaction_type=transaction_type, contract=contract, signal_time=timestamp,
            expected_entry=entry, target=target, stop=stop, score=weighted["score"],
            grade=weighted["grade"], reason=decision.get("reason", "Approved"),
        )

    def _record_decision(self, timestamp, symbol, transaction_type, weighted, allowed, reason, contract=None):
        self.decisions.append(
            {
                "timestamp": timestamp.isoformat(), "trade_date": str(timestamp.date()),
                "symbol": symbol, "transaction_type": transaction_type,
                "category": f"{symbol}_OPTION_{transaction_type}",
                "weighted_score": weighted.get("score"), "weighted_grade": weighted.get("grade"),
                "allowed": bool(allowed), "reason": reason,
                "trading_symbol": (contract or {}).get("trading_symbol"),
            }
        )

    def _simulate(self, candidate, day):
        # The live strategy makes decisions every five minutes. Reusing the
        # already-loaded 5-minute option candles avoids downloading a month of
        # large 1-minute responses for every candidate contract and keeps the
        # replay aligned with the actual decision interval.
        frame = self._candles(candidate.contract, "5minute")
        day_frame = frame[frame.index.date == day]
        future = day_frame[day_frame.index > candidate.signal_time]
        cutoff = pd.Timestamp.combine(day, SQUARE_OFF).tz_localize(IST)
        future = future[future.index <= cutoff]
        if future.empty:
            return None
        is_short = candidate.transaction_type == "SELL"
        first = future.iloc[0]
        raw_entry = _float(first["open"])
        slip = self.slippage_bps / 10000
        entry = raw_entry * (1 - slip if is_short else 1 + slip)
        target_gap = abs(candidate.target - candidate.expected_entry)
        stop_gap = abs(candidate.stop - candidate.expected_entry)
        target = entry - target_gap if is_short else entry + target_gap
        stop = entry + stop_gap if is_short else entry - stop_gap
        current_stop = stop
        best = entry
        exit_price = _float(future.iloc[-1]["close"])
        exit_time = future.index[-1]
        reason = "SQUAREOFF"
        for timestamp, candle in future.iterrows():
            high, low = _float(candle["high"]), _float(candle["low"])
            stop_hit = high >= current_stop if is_short else low <= current_stop
            target_hit = low <= target if is_short else high >= target
            if stop_hit:
                exit_price, exit_time = current_stop, timestamp
                reason = "STOP_AND_TARGET_SAME_CANDLE" if target_hit else "STOP_LOSS"
                break
            if target_hit:
                exit_price, exit_time, reason = target, timestamp, "TARGET"
                break
            best = min(best, low) if is_short else max(best, high)
            if self.enable_trailing_stop:
                current_stop = self._trail(entry, target, current_stop, best, is_short)
        observed = future[future.index <= exit_time]
        max_high_after_entry = _float(observed["high"].max())
        min_low_after_entry = _float(observed["low"].min())
        if is_short:
            favorable_price = min_low_after_entry
            adverse_price = max_high_after_entry
            favorable_time = observed["low"].idxmin()
            adverse_time = observed["high"].idxmax()
            mfe_points = entry - favorable_price
            mae_points = entry - adverse_price
        else:
            favorable_price = max_high_after_entry
            adverse_price = min_low_after_entry
            favorable_time = observed["high"].idxmax()
            adverse_time = observed["low"].idxmin()
            mfe_points = favorable_price - entry
            mae_points = adverse_price - entry
        target_gap_actual = abs(target - entry)
        stop_gap_actual = abs(entry - stop)
        mfe_pct = (mfe_points / entry * 100) if entry else 0
        mae_pct = (mae_points / entry * 100) if entry else 0
        target_progress_pct = (mfe_points / target_gap_actual * 100) if target_gap_actual else 0
        exit_price *= 1 + slip if is_short else 1 - slip
        lot_size = max(int(_float(candidate.contract.get("lot_size"), 1)), 1)
        quantity = lot_size
        lot_multiplier = 1
        if candidate.category in {"NIFTY_OPTION_BUY", "BANKNIFTY_OPTION_BUY"}:
            quantity, lot_multiplier = capital_sized_option_quantity(
                entry,
                lot_size,
                self.option_capital_per_entry,
            )
            if lot_multiplier < 1:
                self.coverage["insufficient_capital_entries"] += 1
                return None
        gross = ((entry - exit_price) if is_short else (exit_price - entry)) * quantity
        return {
            "trade_date": str(day), "category": candidate.category, "symbol": candidate.symbol,
            "trading_symbol": candidate.contract.get("trading_symbol"), "direction": candidate.direction,
            "transaction_type": candidate.transaction_type, "quantity": quantity,
            "lot_size": lot_size, "lot_multiplier": lot_multiplier,
            "allocated_capital": round(self.option_capital_per_entry, 2),
            "signal_time": candidate.signal_time.isoformat(), "entry_time": future.index[0].isoformat(),
            "exit_time": exit_time.isoformat(), "entry_price": round(entry, 2),
            "target_price": round(target, 2), "stop_loss_price": round(stop, 2),
            "final_stop_price": round(current_stop, 2),
            "trailing_stop_enabled": self.enable_trailing_stop,
            "exit_price": round(exit_price, 2), "exit_reason": reason,
            "max_high_after_entry": round(max_high_after_entry, 2),
            "min_low_after_entry": round(min_low_after_entry, 2),
            "mfe_points": round(mfe_points, 2), "mae_points": round(mae_points, 2),
            "mfe_pct": round(mfe_pct, 2), "mae_pct": round(mae_pct, 2),
            "target_progress_pct": round(target_progress_pct, 2),
            "near_target_before_exit": bool(target_progress_pct >= 80 and reason not in {"TARGET", "STOP_AND_TARGET_SAME_CANDLE"}),
            "favorable_extreme_time": favorable_time.isoformat(),
            "adverse_extreme_time": adverse_time.isoformat(),
            "risk_points": round(stop_gap_actual, 2), "reward_points": round(target_gap_actual, 2),
            "weighted_score": candidate.score, "weighted_grade": candidate.grade,
            "gross_pnl": round(gross, 2), "estimated_costs": round(self.cost_per_order * 2, 2),
            "source": "POINT_IN_TIME_REPLAY",
        }

    def _trail(self, entry, target, current_stop, best, is_short):
        gap = entry - target if is_short else target - entry
        progress = (
            ((entry - best) if is_short else (best - entry)) / gap * 100
            if gap > 0
            else 0
        )
        lock_percent = None
        if progress >= self.trail_runner_trigger:
            lock_percent = self.trail_runner_lock
        elif progress >= self.trail_stage_two_trigger:
            lock_percent = self.trail_stage_two_lock
        elif progress >= self.trail_stage_one_trigger:
            lock_percent = self.trail_stage_one_lock
        if lock_percent is not None:
            locked_move = gap * lock_percent / 100
            candidate = entry - locked_move if is_short else entry + locked_move
            current_stop = min(current_stop, candidate) if is_short else max(current_stop, candidate)
        return current_stop

    def _stock_candidate(self, timestamp, stock_frames):
        ranked = []
        for item in stock_frames:
            frame = item["frame"]
            row = _latest_row(frame, timestamp)
            if row is None:
                continue
            day_rows = _completed(frame, timestamp, 5)
            day_rows = day_rows[day_rows.index.date == timestamp.date()]
            if len(day_rows) < 2:
                continue
            previous_close = _float(day_rows.iloc[-2]["close"])
            change = abs((_float(row["close"]) - previous_close) / previous_close) if previous_close else 0
            ranked.append((change, item))
        for _, item in sorted(ranked, reverse=True, key=lambda pair: pair[0])[:8]:
            frame = _completed(item["frame"], timestamp, 5)
            tech = _technicals(item["frame"], timestamp)
            five, fifteen, two = tech["five_min"], tech["fifteen_min"], tech["two_hour"]
            direction = five.get("bias")
            if direction not in {"BULLISH", "BEARISH"} or fifteen.get("bias") != direction:
                continue
            if two.get("bias") not in {direction, "NEUTRAL"} and two.get("confidence") in {"MEDIUM", "HIGH"}:
                continue
            score = 50
            score += 15 if two.get("bias") == direction else 7
            score += 10 if five.get("vwap_bias") == direction else 0
            score += 10 if _float(five.get("volume_ratio")) >= 1.2 else 0
            momentum = _float(five.get("momentum_score")) * (1 if direction == "BULLISH" else -1)
            score += 10 if momentum >= 3 else 0
            score += 5
            if score < _float(os.getenv("STOCK_FUTURES_MIN_SCORE"), 80):
                continue
            # Use the last completed candle at this timestamp. Using frame.iloc[-1]
            # would leak future prices into the stock-futures replay.
            entry = _float(_latest_row(frame, timestamp, 5)["close"])
            atr = max(_float(five.get("atr14")), entry * 0.0025)
            target = entry + atr * 1.5 if direction == "BULLISH" else entry - atr * 1.5
            stop = entry - atr if direction == "BULLISH" else entry + atr
            return Candidate(
                "STOCK_FUTURE", "STOCK_FUTURE", direction,
                "BUY" if direction == "BULLISH" else "SELL", item["contract"], timestamp,
                entry, target, stop, score, "TRADE", "NIFTY 50 futures fallback qualified",
            )
        return None

    def _prepare_stock_frames(self):
        if not self.include_stock_futures:
            return []
        frames = []
        rows = self.data.active_instruments()
        for row in rows:
            if str(row.get("segment")) != "NSE_FO":
                continue
            if str(row.get("instrument_type", "")).upper() not in {"FUT", "FUTSTK"}:
                continue
            if str(row.get("underlying_symbol", "")).upper() not in NIFTY50_SYMBOLS:
                continue
            if not row.get("expiry") or _expiry(row["expiry"]) < self.from_date:
                continue
            try:
                frame = self._candles(row, "5minute")
                if not frame.empty:
                    frames.append({"contract": row, "frame": frame})
            except Exception as error:
                self.progress(f"Skipping {row.get('trading_symbol')}: {error}")
        self.coverage["stock_future_contracts"] = len(frames)
        return frames

    def run(self):
        lookback = self.from_date - timedelta(days=50)
        spot_frames = {}
        for symbol, config in INDEXES.items():
            self.progress(f"Loading {symbol} index history", symbol=symbol)
            spot_frames[symbol] = self._candles(config["key"], "5minute", lookback, self.to_date, expired=False)
            self.expiries[symbol] = self.data.get_expiries(config["key"])
        stock_frames = self._prepare_stock_frames()
        sessions = sorted({stamp.date() for stamp in spot_frames["NIFTY"].index if self.from_date <= stamp.date() <= self.to_date})
        self.coverage["trading_days"] = len(sessions)
        for day_index, day in enumerate(sessions, 1):
            self.progress(f"Replaying {day} ({day_index}/{len(sessions)})", day=str(day), current=day_index, total=len(sessions))
            open_until = None
            open_until_by_symbol = {}
            open_until_by_category = {}
            daily_index_policy = {
                symbol: {"trades": 0, "halted": False}
                for symbol in ("NIFTY", "BANKNIFTY")
            }
            daily_realized_pnl = 0.0
            daily_consecutive_losses = 0
            daily_portfolio_halted = False
            times = pd.date_range(
                pd.Timestamp.combine(day, MARKET_START).tz_localize(IST),
                pd.Timestamp.combine(day, LAST_ENTRY).tz_localize(IST), freq="5min",
            )
            for timestamp in times:
                if daily_portfolio_halted:
                    break
                if (
                    self.portfolio_mode == "live"
                    and not self.allow_simultaneous_index_positions
                    and open_until is not None
                    and timestamp <= open_until
                ):
                    continue
                candidates = []
                for symbol in ("NIFTY", "BANKNIFTY"):
                    policy = daily_index_policy[symbol]
                    if policy["halted"] or (
                        self.max_trades_per_day_per_index > 0
                        and policy["trades"] >= self.max_trades_per_day_per_index
                    ):
                        continue
                    if self.allow_simultaneous_index_positions:
                        symbol_open_until = open_until_by_symbol.get(symbol)
                        if symbol_open_until is not None and timestamp <= symbol_open_until:
                            continue
                    expiry = _choose_expiry(symbol, self.expiries[symbol], day)
                    if not expiry:
                        continue
                    contracts = self._option_contracts(symbol, expiry)
                    for transaction_type in ("BUY", "SELL"):
                        try:
                            candidate = self._candidate(symbol, timestamp, spot_frames[symbol], contracts, transaction_type)
                            if candidate:
                                candidates.append(candidate)
                        except Exception as error:
                            self.progress(f"{symbol} {transaction_type} replay skipped at {timestamp.time()}: {error}")
                if self.portfolio_mode == "live":
                    if self.allow_simultaneous_index_positions:
                        selected_candidates = []
                        for symbol in ("NIFTY", "BANKNIFTY"):
                            symbol_open_until = open_until_by_symbol.get(symbol)
                            if symbol_open_until is not None and timestamp <= symbol_open_until:
                                continue
                            symbol_candidates = [
                                item for item in candidates if item.symbol == symbol
                            ]
                            candidate = max(
                                symbol_candidates,
                                key=lambda item: item.score,
                                default=None,
                            )
                            if candidate:
                                selected_candidates.append(candidate)
                    else:
                        candidate = max(candidates, key=lambda item: item.score, default=None)
                        if candidate is None and stock_frames:
                            candidate = self._stock_candidate(timestamp, stock_frames)
                        selected_candidates = [candidate] if candidate else []
                else:
                    selected_candidates = []
                    for category in {item.category for item in candidates}:
                        category_candidates = [item for item in candidates if item.category == category]
                        category_open_until = open_until_by_category.get(category)
                        if category_open_until is None or timestamp > category_open_until:
                            selected_candidates.append(max(category_candidates, key=lambda item: item.score))
                    # Stock futures are an additional diagnostic category. In
                    # independent mode it is evaluated alongside index tracks;
                    # live mode only reaches it as a fallback below.
                    if stock_frames:
                        stock_candidate = self._stock_candidate(timestamp, stock_frames)
                        category_open_until = open_until_by_category.get("STOCK_FUTURE")
                        if stock_candidate and (category_open_until is None or timestamp > category_open_until):
                            selected_candidates.append(stock_candidate)

                for candidate in selected_candidates:
                    trade = self._simulate(candidate, day)
                    if not trade:
                        continue
                    self.trades.append(trade)
                    realized_pnl = _float(trade.get("gross_pnl")) - _float(
                        trade.get("estimated_costs")
                    )
                    daily_realized_pnl += realized_pnl
                    daily_consecutive_losses = (
                        daily_consecutive_losses + 1 if realized_pnl < 0 else 0
                    )
                    if (
                        self.daily_profit_target > 0
                        and daily_realized_pnl >= self.daily_profit_target
                    ):
                        daily_portfolio_halted = True
                        self.coverage["daily_profit_halts"] += 1
                    elif (
                        self.daily_max_loss > 0
                        and daily_realized_pnl <= -self.daily_max_loss
                    ):
                        daily_portfolio_halted = True
                        self.coverage["daily_loss_halts"] += 1
                    elif (
                        self.max_consecutive_losses > 0
                        and daily_consecutive_losses >= self.max_consecutive_losses
                    ):
                        daily_portfolio_halted = True
                        self.coverage["consecutive_loss_halts"] += 1
                    if candidate.symbol in daily_index_policy:
                        policy = daily_index_policy[candidate.symbol]
                        policy["trades"] += 1
                        if self.stop_after_first_win and realized_pnl > 0:
                            policy["halted"] = True
                        elif self.stop_after_first_loss and realized_pnl < 0:
                            policy["halted"] = True
                    exit_time = pd.Timestamp(trade["exit_time"])
                    if self.portfolio_mode == "live":
                        if self.allow_simultaneous_index_positions:
                            open_until_by_symbol[candidate.symbol] = exit_time
                        else:
                            open_until = exit_time
                    else:
                        open_until_by_category[candidate.category] = exit_time
        return self.trades, self.decisions, dict(self.coverage)
