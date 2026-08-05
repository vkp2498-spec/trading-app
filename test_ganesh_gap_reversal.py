import unittest
import os
from contextlib import nullcontext
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import trade_bot
from strategy_core import choose_expiry
from ganesh_gap_reversal import (
    BEARISH,
    BULLISH,
    GAP_DOWN,
    GAP_UP,
    NO_GAP,
    active_two_hour_start,
    advance_entry_confirmation,
    advance_exit_confirmation,
    atm_strike,
    bollinger_bands,
    candle_colour,
    classic_pivots,
    continuation_for_gap,
    nearest_continuation_target,
    nearest_target,
    opening_gap,
    score_gap_continuation,
    target_reached,
    within_entry_window,
)


IST = ZoneInfo("Asia/Kolkata")


class GaneshGapReversalTests(unittest.TestCase):
    def test_entry_window_includes_1525_only(self):
        self.assertTrue(
            within_entry_window(datetime(2026, 8, 3, 15, 25, 0, tzinfo=IST))
        )
        self.assertFalse(
            within_entry_window(datetime(2026, 8, 3, 15, 25, 1, tzinfo=IST))
        )

    def test_opening_gap_threshold_is_fixed_from_open_and_previous_close(self):
        self.assertEqual(opening_gap(24000, 23940, 0.20)["direction"], GAP_DOWN)
        self.assertEqual(opening_gap(24000, 24060, 0.20)["direction"], GAP_UP)
        self.assertEqual(opening_gap(24000, 24020, 0.20)["direction"], NO_GAP)

    def test_classic_pivots(self):
        levels = classic_pivots(110, 90, 100)
        self.assertEqual(levels["P"], 100.0)
        self.assertEqual(levels["R1"], 110.0)
        self.assertEqual(levels["S1"], 90.0)
        self.assertEqual(levels["R2"], 120.0)
        self.assertEqual(levels["S2"], 80.0)

    def test_bollinger_bands_use_only_latest_period(self):
        bands = bollinger_bands(range(1, 22), period=20, standard_deviations=2)
        self.assertEqual(bands["middle"], 11.5)
        self.assertGreater(bands["upper"], bands["middle"])
        self.assertLess(bands["lower"], bands["middle"])
        self.assertIsNone(bollinger_bands([1], period=2))

    def test_market_aligned_two_hour_buckets(self):
        cases = {
            (9, 30): (9, 15),
            (11, 14): (9, 15),
            (11, 15): (11, 15),
            (13, 14): (11, 15),
            (13, 15): (13, 15),
            (15, 20): (13, 15),
        }
        for (hour, minute), expected in cases.items():
            value = active_two_hour_start(datetime(2026, 7, 31, hour, minute, tzinfo=IST))
            self.assertEqual((value.hour, value.minute), expected)

    def test_candle_colour_honours_neutral_buffer(self):
        self.assertEqual(candle_colour(100, 101, 0.5), "GREEN")
        self.assertEqual(candle_colour(100, 99, 0.5), "RED")
        self.assertEqual(candle_colour(100, 100.25, 0.5), "NEUTRAL")

    def test_gap_down_requires_observed_red_then_two_green_scans(self):
        start = datetime(2026, 7, 31, 9, 15, tzinfo=IST)
        state, entered = advance_entry_confirmation({}, start, "GREEN", GAP_DOWN, 2)
        self.assertFalse(entered)
        state, entered = advance_entry_confirmation(state, start, "RED", GAP_DOWN, 2)
        self.assertFalse(entered)
        state, entered = advance_entry_confirmation(state, start, "GREEN", GAP_DOWN, 2)
        self.assertFalse(entered)
        state, entered = advance_entry_confirmation(state, start, "GREEN", GAP_DOWN, 2)
        self.assertTrue(entered)
        self.assertEqual(state["previous_confirmed_colour"], "GREEN")

    def test_gap_up_requires_observed_green_then_red(self):
        start = datetime(2026, 7, 31, 9, 15, tzinfo=IST)
        state, _ = advance_entry_confirmation({}, start, "GREEN", GAP_UP, 2)
        state, first = advance_entry_confirmation(state, start, "RED", GAP_UP, 2)
        state, second = advance_entry_confirmation(state, start, "RED", GAP_UP, 2)
        self.assertFalse(first)
        self.assertTrue(second)

    def test_new_two_hour_candle_resets_transition_state(self):
        first = datetime(2026, 7, 31, 9, 15, tzinfo=IST)
        second = datetime(2026, 7, 31, 11, 15, tzinfo=IST)
        state, _ = advance_entry_confirmation({}, first, "RED", GAP_DOWN, 2)
        state, entered = advance_entry_confirmation(state, second, "GREEN", GAP_DOWN, 2)
        self.assertFalse(entered)
        self.assertFalse(state["initial_colour_observed"])

    def test_exit_requires_confirmed_opposite_colour(self):
        state, first = advance_exit_confirmation({}, "RED", BULLISH, required_scans=2)
        state, second = advance_exit_confirmation(state, "RED", BULLISH, required_scans=2)
        self.assertFalse(first)
        self.assertTrue(second)
        _, put_exit = advance_exit_confirmation({}, "GREEN", BEARISH, required_scans=1)
        self.assertTrue(put_exit)

    def test_nearest_target_uses_direction_and_minimum_distance(self):
        pivots = {"P": 101, "R1": 103, "R2": 106, "R3": 110, "S1": 97, "S2": 94, "S3": 90}
        self.assertEqual(nearest_target(BULLISH, 100, 102, pivots)["level"], 101.0)
        self.assertEqual(nearest_target(BEARISH, 100, 98, pivots)["level"], 98.0)
        self.assertEqual(
            nearest_target(BULLISH, 100, 102, pivots, minimum_distance=5)["level"],
            106.0,
        )

    def test_atm_strike_and_locked_target_reached(self):
        self.assertEqual(atm_strike(24226, 50), 24250)
        self.assertTrue(target_reached(BULLISH, 24250, 24240))
        self.assertTrue(target_reached(BEARISH, 24190, 24200))

    def test_continuation_direction_follows_the_opening_gap(self):
        self.assertEqual(continuation_for_gap(GAP_UP)["direction"], BULLISH)
        self.assertEqual(continuation_for_gap(GAP_UP)["option_type"], "CE")
        self.assertEqual(continuation_for_gap(GAP_DOWN)["direction"], BEARISH)
        self.assertEqual(continuation_for_gap(GAP_DOWN)["option_type"], "PE")

    def test_continuation_target_uses_outer_band_or_directional_pivot(self):
        bands = {"middle": 101, "upper": 106, "lower": 94}
        pivots = {"P": 100, "R1": 104, "R2": 108, "R3": 112, "S1": 96, "S2": 92, "S3": 88}
        bullish = nearest_continuation_target(BULLISH, 102, bands, pivots)
        bearish = nearest_continuation_target(BEARISH, 98, bands, pivots)
        self.assertEqual(bullish["type"], "R1")
        self.assertEqual(bullish["level"], 104.0)
        self.assertEqual(bearish["type"], "S1")
        self.assertEqual(bearish["level"], 96.0)

    def continuation_snapshot(self):
        return {
            "gap": {"direction": GAP_UP},
            "previous_close": 100.0,
            "today_open": 102.0,
            "spot": 104.5,
            "opening_range": {
                "complete": True,
                "open": 102.0,
                "high": 104.0,
                "low": 101.8,
                "close": 103.5,
            },
            "latest_completed_5m": {
                "complete": True,
                "open": 103.5,
                "high": 105.0,
                "low": 103.8,
                "close": 104.5,
            },
        }

    def test_gap_continuation_accepts_completed_candles_and_aligned_evidence(self):
        result = score_gap_continuation(
            self.continuation_snapshot(),
            {"bias": BULLISH, "confidence": "HIGH"},
            {"direction": BULLISH, "confidence": "HIGH"},
            {
                "close": 110.0,
                "vwap": 100.0,
                "vwap_slope": 0.5,
                "volume_ratio": 1.5,
                "volume_confirmed": True,
            },
            {"bias": "NEUTRAL", "confidence": "LOW"},
        )
        self.assertTrue(result["allowed"])
        self.assertGreaterEqual(result["score"], 75.0)
        self.assertTrue(result["retest_confirmed"])

    def test_gap_continuation_rejects_high_confidence_opposite_chain(self):
        result = score_gap_continuation(
            self.continuation_snapshot(),
            {"bias": BULLISH, "confidence": "HIGH"},
            {"direction": BEARISH, "confidence": "HIGH"},
            {
                "close": 110.0,
                "vwap": 100.0,
                "vwap_slope": 0.5,
                "volume_ratio": 1.5,
                "volume_confirmed": True,
            },
            {"bias": "NEUTRAL", "confidence": "LOW"},
        )
        self.assertFalse(result["allowed"])
        self.assertIn("HIGH-confidence option chain opposes continuation", result["blockers"])

    def test_candle_freshness_is_measured_from_interval_end(self):
        current = datetime(2026, 8, 3, 10, 2, 30, tzinfo=IST)
        candle_start = datetime(2026, 8, 3, 10, 1, tzinfo=IST)
        self.assertEqual(
            trade_bot._ganesh_candle_age_seconds(candle_start, current),
            30.0,
        )

    def test_ganesh_dispatch_checks_nifty_and_banknifty(self):
        current = datetime(2026, 8, 3, 10, 0, tzinfo=IST)
        with (
            patch.object(trade_bot, "now_ist", return_value=current),
            patch.object(trade_bot, "write_stream_instruments") as subscriptions,
            patch.object(trade_bot, "run_ganesh_gap_symbol_signal_check") as check,
            patch.object(trade_bot, "read_state", return_value={}),
        ):
            trade_bot.run_ganesh_gap_signal_check()

        self.assertEqual(
            [call.args[0] for call in check.call_args_list],
            ["NIFTY", "BANKNIFTY"],
        )
        subscriptions.assert_called_once()
        subscribed = set(subscriptions.call_args.args[0])
        self.assertIn(trade_bot.UNDERLYING_INDEX_KEYS["NIFTY"], subscribed)
        self.assertIn(trade_bot.UNDERLYING_INDEX_KEYS["BANKNIFTY"], subscribed)

    def test_ganesh_daily_trade_cap_is_combined_across_both_indices(self):
        with patch.object(
            trade_bot,
            "trade_count_for",
            side_effect=lambda slot: 1 if slot == "GANESH_GAP_NIFTY" else 0,
        ):
            self.assertEqual(trade_bot.ganesh_gap_trade_count_today(), 1)

    def test_trade_cap_does_not_create_partial_symbol_state(self):
        current = datetime(2026, 8, 3, 12, 35, tzinfo=IST)
        with (
            patch.object(trade_bot, "read_state", return_value={}),
            patch.object(trade_bot, "state_is_active", return_value=False),
            patch.object(trade_bot, "active_bot_states", return_value=[]),
            patch.object(trade_bot, "ganesh_gap_max_trades_per_day", return_value=1),
            patch.object(trade_bot, "ganesh_gap_trade_count_today", return_value=1),
            patch.object(trade_bot, "write_state") as write,
        ):
            trade_bot.run_ganesh_gap_symbol_signal_check("BANKNIFTY", now=current)

        write.assert_not_called()

    def test_completed_gap_acceptance_selects_continuation_lane(self):
        current = datetime(2026, 8, 3, 9, 35, tzinfo=IST)
        state_slot = trade_bot.GANESH_GAP_STATE_BY_SYMBOL["NIFTY"]
        snapshot = {
            "symbol": "NIFTY",
            "timestamp": current.isoformat(),
            "spot": 104.5,
            "previous_high": 103.0,
            "previous_low": 98.0,
            "previous_close": 100.0,
            "today_open": 102.0,
            "gap": {"direction": GAP_UP, "points": 2.0, "percent": 2.0},
            "pivots": {
                "P": 100.3,
                "R1": 102.7,
                "R2": 105.3,
                "R3": 107.7,
                "S1": 97.7,
                "S2": 95.3,
                "S3": 92.7,
            },
            "bollinger": {"middle": 101.0, "upper": 106.0, "lower": 96.0},
            "candle_start": current.replace(hour=9, minute=15).isoformat(),
            "candle_open": 102.0,
            "candle_colour": "GREEN",
            "volume_confirmed": False,
            "volume_ratio": 0.0,
            "opening_range": {
                "complete": True,
                "open": 102.0,
                "high": 104.0,
                "low": 101.8,
                "close": 103.5,
            },
            "latest_completed_5m": {
                "complete": True,
                "start": current.replace(minute=30).isoformat(),
                "open": 103.5,
                "high": 105.0,
                "low": 103.8,
                "close": 104.5,
            },
        }
        option = {
            "allowed": True,
            "option_type": "CE",
            "strike": 100,
            "expiry": "2026-08-11",
        }
        evidence = {
            "allowed": True,
            "score": 92.5,
            "reasons": ["aligned"],
            "blockers": [],
            "retest_confirmed": True,
        }
        with (
            patch.dict(os.environ, {"GANESH_MIN_TARGET_POINTS": "1"}, clear=False),
            patch.object(trade_bot, "read_state", return_value={}),
            patch.object(trade_bot, "state_is_active", return_value=False),
            patch.object(trade_bot, "active_bot_states", return_value=[]),
            patch.object(trade_bot, "ganesh_gap_max_trades_per_day", return_value=2),
            patch.object(trade_bot, "ganesh_gap_trade_count_today", return_value=0),
            patch.object(trade_bot, "portfolio_day_circuit", return_value={"allowed": True}),
            patch.object(trade_bot, "ganesh_gap_market_snapshot", return_value=snapshot),
            patch.object(trade_bot, "ganesh_gap_option_candidate", return_value=option),
            patch.object(trade_bot, "ganesh_gap_continuation_evidence", return_value=evidence),
            patch.object(trade_bot, "record_ganesh_gap_scan"),
            patch.object(trade_bot, "write_state"),
            patch.object(trade_bot, "execute_ganesh_gap_entry", return_value=(True, "opened")) as execute,
        ):
            trade_bot.run_ganesh_gap_symbol_signal_check("NIFTY", now=current)

        selected_state = execute.call_args.args[0]
        self.assertEqual(selected_state["strategy_lane"], "CONTINUATION")
        self.assertEqual(selected_state["continuation_score"], 92.5)
        self.assertEqual(execute.call_args.kwargs["symbol"], "NIFTY")

    def test_continuation_exits_after_later_completed_5m_closes_back_inside_range(self):
        current = datetime(2026, 8, 3, 10, 0, tzinfo=IST)
        frame = pd.DataFrame(
            [{"open": 104.2, "high": 104.4, "low": 103.2, "close": 103.5}],
            index=pd.DatetimeIndex([datetime(2026, 8, 3, 9, 55, tzinfo=IST)]),
        )
        state = {
            "strategy": "GANESH_GAP_CONTINUATION",
            "strategy_lane": "CONTINUATION",
            "direction": BULLISH,
            "underlying_instrument_key": "NSE_INDEX|Nifty 50",
            "continuation_opening_range_high": 104.0,
            "continuation_entry_5m_start": datetime(
                2026, 8, 3, 9, 30, tzinfo=IST
            ).isoformat(),
        }
        with patch.object(
            trade_bot, "fetch_v3_intraday_minutes", return_value=frame
        ):
            reason = trade_bot.ganesh_continuation_structure_exit(state, current)
        self.assertEqual(reason, "CONTINUATION_5M_INVALIDATION")

    def test_same_day_partial_state_is_reinitialized_from_market_snapshot(self):
        current = datetime(2026, 8, 3, 12, 40, tzinfo=IST)
        state_slot = trade_bot.GANESH_GAP_STATE_BY_SYMBOL["BANKNIFTY"]
        partial = {
            "date": "2026-08-03",
            "phase": "DISABLED_FOR_DAY",
            "symbol": "BANKNIFTY",
        }
        snapshot = {
            "symbol": "BANKNIFTY",
            "gap": {"direction": NO_GAP, "points": 0.0, "percent": 0.0},
            "previous_high": 58000.0,
            "previous_low": 57000.0,
            "previous_close": 57500.0,
            "today_open": 57500.0,
            "pivots": {"P": 57500.0},
        }
        with (
            patch.object(trade_bot, "read_state", return_value=partial),
            patch.object(trade_bot, "state_is_active", return_value=False),
            patch.object(trade_bot, "active_bot_states", return_value=[]),
            patch.object(trade_bot, "ganesh_gap_max_trades_per_day", return_value=2),
            patch.object(trade_bot, "ganesh_gap_trade_count_today", return_value=1),
            patch.object(trade_bot, "portfolio_day_circuit", return_value={"allowed": True}),
            patch.object(trade_bot, "ganesh_gap_market_snapshot", return_value=snapshot),
            patch.object(trade_bot, "record_ganesh_gap_scan"),
            patch.object(trade_bot, "write_state") as write,
        ):
            trade_bot.run_ganesh_gap_symbol_signal_check("BANKNIFTY", now=current)

        repaired = write.call_args.args[1]
        self.assertEqual(repaired["state_slot"], state_slot)
        self.assertEqual(repaired["gap_direction"], NO_GAP)
        self.assertEqual(repaired["previous_close"], 57500.0)

    def test_completed_ganesh_entry_overrides_transitional_metadata(self):
        state_slot = trade_bot.GANESH_GAP_STATE_BY_SYMBOL["BANKNIFTY"]
        initial = {
            "status": "BUY_PLACED_NOT_COMPLETE",
            "phase": "ENTRY_PENDING",
            "symbol": "BANKNIFTY",
            "underlying_symbol": "BANKNIFTY",
            "direction": BEARISH,
            "underlying_target_distance": 100.0,
        }
        pending = {
            **initial,
            "entry_order_id": "ENTRY-1",
            "instrument_key": "NSE_FO|BANK_OPTION",
        }
        instrument = {
            "instrument_key": "NSE_FO|BANK_OPTION",
            "trading_symbol": "BANKNIFTY ATM PE",
            "lot_size": 30,
        }
        with (
            patch.object(trade_bot, "save_open_position_state") as save,
            patch.object(trade_bot, "read_state", return_value=pending),
            patch.object(trade_bot, "write_state") as write,
        ):
            state = trade_bot.finalize_ganesh_gap_position(
                initial,
                fill=300.0,
                quantity=30,
                instrument=instrument,
                order_id="ENTRY-1",
                state_slot=state_slot,
            )

        save.assert_called_once()
        self.assertEqual(state["status"], "POSITION_OPEN")
        self.assertEqual(state["phase"], "POSITION_OPEN")
        written = write.call_args.args[1]
        self.assertEqual(written["status"], "POSITION_OPEN")
        self.assertEqual(written["phase"], "POSITION_OPEN")

    def test_pending_ganesh_entry_with_existing_stop_is_repaired_without_rearming(self):
        state_slot = trade_bot.GANESH_GAP_STATE_BY_SYMBOL["BANKNIFTY"]
        stale = {
            "status": "BUY_PLACED_NOT_COMPLETE",
            "phase": "ENTRY_PENDING",
            "symbol": "BANKNIFTY",
            "entry_order_id": "ENTRY-1",
            "protective_stop_order_id": "STOP-1",
        }
        with (
            patch.object(
                trade_bot, "position_finalization_lock", return_value=nullcontext()
            ),
            patch.object(trade_bot, "read_state", return_value=stale),
            patch.object(trade_bot, "write_state") as write,
            patch.object(trade_bot, "ensure_protective_stop") as ensure_stop,
            patch.object(trade_bot, "finalize_ganesh_gap_position") as finalize,
        ):
            state = trade_bot.finalize_and_protect_ganesh_gap_position(
                stale,
                fill=300.0,
                quantity=30,
                instrument={"instrument_key": "NSE_FO|BANK_OPTION"},
                order_id="ENTRY-1",
                state_slot=state_slot,
            )

        self.assertEqual(state["status"], "POSITION_OPEN")
        self.assertEqual(state["phase"], "POSITION_OPEN")
        self.assertEqual(state["protective_stop_order_id"], "STOP-1")
        write.assert_called_once_with(state_slot, state)
        ensure_stop.assert_not_called()
        finalize.assert_not_called()

    def test_banknifty_contract_selection_uses_banknifty_chain(self):
        chain = pd.DataFrame(
            [
                {
                    "strike": 55500.0,
                    "expiry": "2026-08-25",
                    "CE_ltp": 320.0,
                    "CE_bid_price": 319.0,
                    "CE_ask_price": 321.0,
                }
            ]
        )
        instrument = {
            "instrument_key": "NSE_FO|BANK_OPTION",
            "trading_symbol": "BANKNIFTY 55500 CE",
            "lot_size": 30,
        }
        quality = {
            "ltp": 320.0,
            "bid_price": 319.0,
            "ask_price": 321.0,
            "spread_percent": 0.625,
        }
        recommendation = {
            "symbol": "BANKNIFTY",
            "direction": "BULLISH",
            "confidence": "HIGH",
            "score": 4,
            "reasons": [],
            "analysis_expiry": "2026-08-18",
            "analysis_atm": {"strike": 55500.0, "expiry": "2026-08-18"},
            "execution_chain": chain.to_dict("records"),
        }
        with (
            patch.object(
                trade_bot,
                "get_index_recommendation",
                return_value=recommendation,
            ) as recommend,
            patch.object(
                trade_bot,
                "find_index_option_instrument",
                return_value=instrument,
            ) as find,
            patch.object(trade_bot, "read_market_cache", return_value={}),
            patch.object(trade_bot, "option_contract_quality", return_value=quality),
            patch.object(
                trade_bot,
                "get_option_volume_vwap_analysis",
                return_value={"bias": "BULLISH"},
            ),
            patch.object(trade_bot, "write_stream_instruments"),
        ):
            candidate = trade_bot.ganesh_gap_option_candidate(
                {"symbol": "BANKNIFTY", "spot": 55525.0},
                {"option_type": "CE"},
            )

        self.assertTrue(candidate["allowed"])
        recommend.assert_called_once_with("BANKNIFTY")
        self.assertEqual(find.call_args.args[0], "BANKNIFTY")
        self.assertEqual(candidate["strike"], 55500)

    def test_nifty_contract_selection_uses_expiry_after_nearest(self):
        expiries = ["2026-08-04", "2026-08-11", "2026-08-18"]
        self.assertEqual(
            choose_expiry("NIFTY", expiries, datetime(2026, 8, 4).date()),
            "2026-08-11",
        )

    def test_ganesh_engine_dispatches_only_ganesh_entry_logic(self):
        with (
            patch.dict(os.environ, {"TRADING_ENGINE": "GANESH"}, clear=False),
            patch.object(trade_bot, "market_window_ok", return_value=True),
            patch.object(trade_bot, "read_state", return_value={}),
            patch.object(trade_bot, "run_ganesh_gap_signal_check") as ganesh,
            patch.object(trade_bot, "evaluate_symbol_buy_or_sell") as vamsi,
        ):
            trade_bot.run_signal_check()
        ganesh.assert_called_once_with()
        vamsi.assert_not_called()

    def test_unknown_engine_is_rejected(self):
        with patch.dict(os.environ, {"TRADING_ENGINE": "UNKNOWN"}, clear=False):
            with self.assertRaises(RuntimeError):
                trade_bot.trading_engine()

    def test_paper_position_exits_when_locked_underlying_target_is_reached(self):
        now = trade_bot.now_ist()
        candle_start = active_two_hour_start(now)
        state = {
            "strategy": "GANESH_GAP_REVERSAL",
            "paper_trade": True,
            "status": "POSITION_OPEN",
            "instrument_key": "NSE_FO|OPTION",
            "trading_symbol": "NIFTY ATM CE",
            "symbol": "NIFTY",
            "underlying_symbol": "NIFTY",
            "direction": BULLISH,
            "quantity": 65,
            "entry_price": 100.0,
            "nifty_target_type": "P",
            "nifty_target_level": 24250.0,
            "monitor_candle_start": candle_start.isoformat(),
            "monitor_candle_open": 24210.0,
            "created_at": now.isoformat(),
        }

        def quote_for(key):
            if key == "NSE_FO|OPTION":
                return {"ltp": 110.0, "received_at": now.timestamp()}
            return {"ltp": 24255.0, "received_at": now.timestamp()}

        journal = {"entry_price": 100.0, "exit_price": 110.0, "gross_pnl": 650.0}
        with (
            patch.object(trade_bot, "daily_max_loss_reached", return_value=False),
            patch.object(trade_bot, "read_market_cache", side_effect=quote_for),
            patch.object(trade_bot, "write_state"),
            patch.object(trade_bot, "record_closed_trade", return_value=journal) as record,
            patch.object(trade_bot, "send_apple_closed_trade_alert"),
            patch.object(trade_bot, "clear_state") as clear,
        ):
            handled = trade_bot.handle_ganesh_gap_position(state, verbose=False)

        self.assertTrue(handled)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[2], "UNDERLYING_TARGET")
        clear.assert_called_once_with(trade_bot.GANESH_GAP_STATE)

    def test_banknifty_paper_position_uses_bank_feed_and_state_slot(self):
        now = trade_bot.now_ist()
        candle_start = active_two_hour_start(now)
        bank_slot = trade_bot.GANESH_GAP_STATE_BY_SYMBOL["BANKNIFTY"]
        state = {
            "strategy": "GANESH_GAP_REVERSAL",
            "state_slot": bank_slot,
            "paper_trade": True,
            "status": "POSITION_OPEN",
            "instrument_key": "NSE_FO|BANK_OPTION",
            "trading_symbol": "BANKNIFTY ATM CE",
            "symbol": "BANKNIFTY",
            "underlying_symbol": "BANKNIFTY",
            "underlying_instrument_key": trade_bot.UNDERLYING_INDEX_KEYS["BANKNIFTY"],
            "direction": BULLISH,
            "quantity": 30,
            "entry_price": 300.0,
            "underlying_target_type": "R1",
            "underlying_target_level": 55600.0,
            "monitor_candle_start": candle_start.isoformat(),
            "monitor_candle_open": 55500.0,
            "created_at": now.isoformat(),
        }

        def quote_for(key):
            if key == "NSE_FO|BANK_OPTION":
                return {"ltp": 330.0, "received_at": now.timestamp()}
            if key == trade_bot.UNDERLYING_INDEX_KEYS["BANKNIFTY"]:
                return {"ltp": 55610.0, "received_at": now.timestamp()}
            self.fail(f"unexpected market cache key {key}")

        journal = {"entry_price": 300.0, "exit_price": 330.0, "gross_pnl": 900.0}
        with (
            patch.object(trade_bot, "daily_max_loss_reached", return_value=False),
            patch.object(trade_bot, "read_market_cache", side_effect=quote_for),
            patch.object(trade_bot, "write_state"),
            patch.object(trade_bot, "record_closed_trade", return_value=journal),
            patch.object(trade_bot, "send_apple_closed_trade_alert"),
            patch.object(trade_bot, "clear_state") as clear,
        ):
            handled = trade_bot.handle_ganesh_gap_position(state, verbose=False)

        self.assertTrue(handled)
        clear.assert_called_once_with(bank_slot)


if __name__ == "__main__":
    unittest.main()
