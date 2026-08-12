import os
import json
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import call, patch

import pandas as pd


# The control tests do not call OpenAI. Stub the optional client so they also
# run on a lightweight development machine without production dependencies.
openai_stub = types.ModuleType("openai")
openai_stub.OpenAI = object
sys.modules.setdefault("openai", openai_stub)

requests_stub = types.ModuleType("requests")
requests_stub.request = lambda *args, **kwargs: None
requests_stub.get = lambda *args, **kwargs: None
requests_stub.post = lambda *args, **kwargs: None
sys.modules.setdefault("requests", requests_stub)

connection_stub = types.ModuleType("urllib3.util.connection")
connection_stub.allowed_gai_family = lambda: None
util_stub = types.ModuleType("urllib3.util")
util_stub.connection = connection_stub
urllib3_stub = types.ModuleType("urllib3")
urllib3_stub.util = util_stub
sys.modules.setdefault("urllib3", urllib3_stub)
sys.modules.setdefault("urllib3.util", util_stub)
sys.modules.setdefault("urllib3.util.connection", connection_stub)

httpx_stub = types.ModuleType("httpx")
httpx_stub.HTTPError = Exception
httpx_stub.Client = object
sys.modules.setdefault("httpx", httpx_stub)

jwt_stub = types.ModuleType("jwt")
jwt_stub.encode = lambda *args, **kwargs: "test-token"
sys.modules.setdefault("jwt", jwt_stub)

import trade_bot
import strategy_core
import manual_index_trade
import apns_push
import dashboard_data
import trade_journal
from counterfactual_replay import simulate_trade
from strategy_replay import Candidate, StrategyReplay, _choose_expiry, capital_sized_option_quantity
from strategy_core import choose_analysis_expiry, choose_expiry
from signal_score import nifty_neutral_chain_direction, weighted_alignment_score
from backtest_report import build_reports
from market_technicals import candle_confirmation, completed_candles


class TradeControlTests(unittest.TestCase):
    def test_vamsi_unified_fallback_is_strictly_above_fifty_five(self):
        with patch.dict(
            os.environ,
            {
                "VAMSI_UNIFIED_SCORE_FALLBACK": "55",
                "VAMSI_UNIFIED_SCORE_MAXIMUM": "",
                "INDEX_DIRECT_ENTRY_MIN_SCORE": "75",
            },
            clear=False,
        ):
            self.assertEqual(trade_bot.direct_entry_minimum_score("NIFTY"), 55)
            self.assertFalse(trade_bot.vamsi_weighted_score_qualifies(55, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_weighted_score_qualifies(55.1, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_weighted_score_qualifies(58, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_weighted_score_qualifies(75, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_weighted_score_qualifies(100, "NIFTY"))

    def test_vamsi_static_score_range_is_inclusive(self):
        with patch.dict(
            os.environ,
            {
                "VAMSI_ADAPTIVE_SCORE_ENABLED": "false",
                "VAMSI_UNIFIED_SCORE_FALLBACK": "50",
                "VAMSI_UNIFIED_SCORE_MAXIMUM": "59",
            },
            clear=False,
        ):
            rule = trade_bot.vamsi_score_rule("NIFTY")
            self.assertEqual(rule["mode"], "RANGE")
            self.assertEqual(rule["min_score"], 50)
            self.assertEqual(rule["max_score"], 59)
            self.assertFalse(trade_bot.vamsi_entry_score_qualifies(49.9, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_entry_score_qualifies(50, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_entry_score_qualifies(59, "NIFTY"))
            self.assertFalse(trade_bot.vamsi_entry_score_qualifies(59.1, "NIFTY"))

    def test_vamsi_disjoint_score_ranges_are_inclusive_and_reject_the_gap(self):
        with patch.dict(
            os.environ,
            {
                "VAMSI_ADAPTIVE_SCORE_ENABLED": "true",
                "VAMSI_UNIFIED_SCORE_RANGES": "50-59,80-89",
                "VAMSI_UNIFIED_SCORE_FALLBACK": "10",
                "VAMSI_UNIFIED_SCORE_MAXIMUM": "100",
            },
            clear=False,
        ), patch.object(trade_bot, "read_effective_score_rule") as adaptive:
            rule = trade_bot.vamsi_score_rule("NIFTY")

            self.assertEqual(rule["mode"], "RANGES")
            self.assertEqual(rule["source"], "STATIC_RANGES")
            self.assertEqual(trade_bot.direct_entry_minimum_score("NIFTY"), 50)
            self.assertFalse(trade_bot.vamsi_entry_score_qualifies(49.9, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_entry_score_qualifies(50, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_entry_score_qualifies(59, "NIFTY"))
            self.assertFalse(trade_bot.vamsi_entry_score_qualifies(59.1, "NIFTY"))
            self.assertFalse(trade_bot.vamsi_entry_score_qualifies(79.9, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_entry_score_qualifies(80, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_entry_score_qualifies(89, "NIFTY"))
            self.assertFalse(trade_bot.vamsi_entry_score_qualifies(89.1, "NIFTY"))
            self.assertEqual(
                trade_bot.format_vamsi_score_rule(rule),
                "configured ranges 50.0-59.0 or 80.0-89.0",
            )
            adaptive.assert_not_called()

    def test_vamsi_disjoint_score_ranges_reject_invalid_or_overlapping_bands(self):
        for configured in ("50-59,broken", "50-70,70-89", "80-50", "50-101"):
            with self.subTest(configured=configured), patch.dict(
                os.environ,
                {"VAMSI_UNIFIED_SCORE_RANGES": configured},
                clear=False,
            ):
                with self.assertRaises(RuntimeError):
                    trade_bot.static_vamsi_score_rule()

    def test_vamsi_score_threshold_cannot_exceed_one_hundred(self):
        with patch.dict(
            os.environ,
            {"VAMSI_UNIFIED_SCORE_FALLBACK": "101"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "at most 100"):
                trade_bot.direct_entry_minimum_score("BANKNIFTY")

    def test_vamsi_adaptive_range_is_inclusive_and_has_an_upper_cutoff(self):
        adaptive = {
            "status": "ADAPTIVE",
            "mode": "RANGE",
            "min_score": 40.0,
            "max_score": 64.0,
        }
        with (
            patch.dict(
                os.environ,
                {
                    "VAMSI_ADAPTIVE_SCORE_ENABLED": "true",
                    "VAMSI_UNIFIED_SCORE_FALLBACK": "55",
                },
                clear=False,
            ),
            patch.object(
                trade_bot,
                "read_effective_score_rule",
                return_value=adaptive,
            ),
        ):
            self.assertFalse(trade_bot.vamsi_weighted_score_qualifies(39.9, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_weighted_score_qualifies(40, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_weighted_score_qualifies(64, "NIFTY"))
            self.assertFalse(trade_bot.vamsi_weighted_score_qualifies(64.1, "NIFTY"))
            self.assertEqual(trade_bot.direct_entry_minimum_score("NIFTY"), 40)

    def test_vamsi_uses_static_threshold_when_daily_calibration_is_missing(self):
        with (
            patch.dict(
                os.environ,
                {
                    "VAMSI_ADAPTIVE_SCORE_ENABLED": "true",
                    "VAMSI_UNIFIED_SCORE_FALLBACK": "55",
                },
                clear=False,
            ),
            patch.object(
                trade_bot,
                "read_effective_score_rule",
                return_value=None,
            ),
        ):
            self.assertFalse(trade_bot.vamsi_weighted_score_qualifies(55, "NIFTY"))
            self.assertTrue(trade_bot.vamsi_weighted_score_qualifies(55.1, "NIFTY"))

    def test_vamsi_entry_window_includes_1525_only(self):
        with patch.dict(
            os.environ,
            {
                "VAMSI_FIRST_ENTRY_TIME": "09:15",
                "VAMSI_LAST_ENTRY_TIME": "15:25",
            },
            clear=False,
        ):
            with patch.object(
                trade_bot,
                "now_ist",
                return_value=datetime(2026, 8, 3, 9, 14, 59),
            ):
                self.assertFalse(trade_bot.market_window_ok())
            with patch.object(
                trade_bot,
                "now_ist",
                return_value=datetime(2026, 8, 3, 9, 15, 0),
            ):
                self.assertTrue(trade_bot.market_window_ok())
            with patch.object(
                trade_bot,
                "now_ist",
                return_value=datetime(2026, 8, 3, 15, 25, 0),
            ):
                self.assertTrue(trade_bot.market_window_ok())
            with patch.object(
                trade_bot,
                "now_ist",
                return_value=datetime(2026, 8, 3, 15, 25, 1),
            ):
                self.assertFalse(trade_bot.market_window_ok())

    def test_nifty_analysis_uses_nearest_expiry(self):
        expiries = ["2026-08-04", "2026-08-11", "2026-08-18"]
        self.assertEqual(choose_analysis_expiry("NIFTY", expiries), "2026-08-04")

    def test_nifty_uses_following_expiry_outside_wednesday(self):
        expiries = ["2026-08-04", "2026-08-11", "2026-08-18"]
        trading_date = datetime(2026, 8, 4).date()
        self.assertEqual(
            choose_expiry("NIFTY", expiries, trading_date),
            "2026-08-11",
        )

    def test_nifty_uses_front_expiry_only_on_wednesday(self):
        expiries = ["2026-08-11", "2026-08-18", "2026-08-25"]
        trading_date = datetime(2026, 8, 5).date()
        self.assertEqual(
            choose_expiry("NIFTY", expiries, trading_date),
            "2026-08-11",
        )

    def test_nifty_does_not_fall_back_when_next_expiry_is_missing(self):
        with self.assertRaisesRegex(RuntimeError, "No next-week expiry"):
            choose_expiry(
                "NIFTY",
                ["2026-08-04"],
                datetime(2026, 8, 4).date(),
            )

    def test_banknifty_keeps_nearest_expiry(self):
        expiries = ["2026-08-25", "2026-09-29"]
        self.assertEqual(choose_expiry("BANKNIFTY", expiries), "2026-08-25")

    def test_nifty_recommendation_separates_analysis_and_execution_expiries(self):
        def chain(expiry, *, bullish):
            row = {
                "expiry": expiry,
                "spot": 24310,
                "strike": 24300,
                "CE_ltp": 120 if bullish else 80,
                "PE_ltp": 100 if bullish else 140,
                "CE_oi": 100 if bullish else 400,
                "PE_oi": 400 if bullish else 100,
                "CE_previous_oi": 100,
                "PE_previous_oi": 100,
                "CE_change_oi": 0 if bullish else 300,
                "PE_change_oi": 300 if bullish else 0,
                "CE_volume": 1000,
                "PE_volume": 1200,
            }
            frame = pd.DataFrame([row])
            return frame.copy(), frame.copy(), frame.copy()

        nearest = chain("2026-08-04", bullish=True)
        next_week = chain("2026-08-11", bullish=False)

        def fetch(_symbol, nearby=5, *, expiry_role="execution", expiry=None):
            return nearest if expiry == "2026-08-04" else next_week

        with (
            patch.object(
                strategy_core,
                "get_expiries_from_upstox",
                return_value=["2026-08-04", "2026-08-11", "2026-08-18"],
            ),
            patch.object(
                strategy_core,
                "now_ist",
                return_value=datetime(2026, 8, 3, 10, 0),
            ),
            patch.object(strategy_core, "fetch_upstox_option_chain", side_effect=fetch),
        ):
            recommendation = strategy_core.get_index_recommendation("NIFTY")

        self.assertEqual(recommendation["direction"], "BULLISH")
        self.assertEqual(recommendation["analysis_expiry"], "2026-08-04")
        self.assertEqual(recommendation["analysis_atm"]["expiry"], "2026-08-04")
        self.assertEqual(recommendation["execution_expiry"], "2026-08-11")
        self.assertEqual(recommendation["atm"]["expiry"], "2026-08-11")

    def test_nifty_execution_contract_rows_are_atm_only(self):
        atm = {"strike": 24300, "expiry": "2026-08-11"}
        recommendation = {
            "symbol": "NIFTY",
            "atm": atm,
            "nearby_contracts": [
                {"strike": 24250, "expiry": "2026-08-11"},
                atm,
                {"strike": 24350, "expiry": "2026-08-11"},
            ],
        }
        self.assertEqual(
            trade_bot.index_contract_rows(recommendation, "BULLISH"),
            [atm],
        )

    def test_candidate_scores_near_expiry_flow_but_checks_execution_flow(self):
        rec = {
            "symbol": "NIFTY",
            "direction": "BULLISH",
            "confidence": "HIGH",
            "score": 4,
            "reasons": ["nearest chain bullish"],
            "analysis_atm": {
                "strike": 24300,
                "expiry": "2026-08-04",
                "CE_ltp": 100,
                "CE_volume": 1000,
            },
            "atm": {
                "strike": 24300,
                "expiry": "2026-08-11",
                "CE_ltp": 150,
                "CE_volume": 800,
            },
        }
        technicals = {
            "two_hour": {"bias": "BULLISH"},
            "fifteen_min": {"bias": "BULLISH"},
            "five_min": {"bias": "BULLISH"},
        }

        def instrument(_symbol, expiry, strike, option_type):
            return {
                "instrument_key": f"NSE_FO|{expiry}",
                "trading_symbol": f"NIFTY {strike} {option_type} {expiry}",
                "lot_size": 65,
            }

        near_flow = {"close": 101, "vwap": 99, "volume_ratio": 1.5, "bias": "BULLISH"}
        execution_flow = {"close": 151, "vwap": 149, "volume_ratio": 0.8, "bias": "NEUTRAL"}
        with (
            patch.object(trade_bot, "find_index_option_instrument", side_effect=instrument),
            patch.object(trade_bot, "read_market_cache", return_value={}),
            patch.object(
                trade_bot,
                "option_contract_quality",
                return_value={
                    "spread_percent": 1.0,
                    "delta": 0.5,
                    "depth_bias": "NEUTRAL",
                },
            ),
            patch.object(
                trade_bot,
                "get_option_volume_vwap_analysis",
                side_effect=[near_flow, execution_flow],
            ),
            patch.object(trade_bot, "write_stream_instruments"),
            patch.object(trade_bot, "classify_market_regime", return_value={"regime": "TREND"}),
            patch.object(trade_bot, "entry_structure_for_direction", return_value={"type": "TEST"}),
            patch.object(
                trade_bot,
                "convert_index_levels_to_option_premium",
                side_effect=lambda value, **_kwargs: value,
            ),
            patch.object(
                trade_bot,
                "weighted_alignment_score",
                return_value={"score": 0, "grade": "SKIP", "reasons": []},
            ),
        ):
            candidate, _ = trade_bot.build_trade_candidate(
                "NIFTY",
                rec,
                technicals,
                {},
                {},
                "BUY",
            )

        scored_flow = candidate["technicals"]["atm_option_flow"]
        checked_flow = candidate["technicals"]["execution_atm_option_flow"]
        self.assertEqual(scored_flow["vwap"], near_flow["vwap"])
        self.assertEqual(scored_flow["volume_ratio"], near_flow["volume_ratio"])
        self.assertEqual(checked_flow["vwap"], execution_flow["vwap"])
        self.assertEqual(checked_flow["volume_ratio"], execution_flow["volume_ratio"])
        self.assertEqual(candidate["option_summary"]["analysis_expiry"], "2026-08-04")
        self.assertEqual(candidate["option_summary"]["execution_expiry"], "2026-08-11")

    def test_ganesh_analyzes_nearest_and_executes_next_expiry_atm(self):
        recommendation = {
            "direction": "BULLISH",
            "confidence": "HIGH",
            "score": 4,
            "reasons": ["nearest chain bullish"],
            "analysis_expiry": "2026-08-04",
            "analysis_atm": {
                "strike": 24300,
                "expiry": "2026-08-04",
            },
            "execution_chain": [
                {
                    "strike": 24300,
                    "expiry": "2026-08-11",
                    "CE_ltp": 150,
                    "CE_bid_price": 149,
                    "CE_ask_price": 151,
                }
            ],
        }

        def instrument(_symbol, expiry, strike, option_type):
            return {
                "instrument_key": f"NSE_FO|{expiry}",
                "trading_symbol": f"NIFTY {strike} {option_type} {expiry}",
                "lot_size": 65,
            }

        with (
            patch.object(trade_bot, "get_index_recommendation", return_value=recommendation),
            patch.object(trade_bot, "find_index_option_instrument", side_effect=instrument),
            patch.object(trade_bot, "read_market_cache", return_value={}),
            patch.object(
                trade_bot,
                "option_contract_quality",
                return_value={
                    "ltp": 150,
                    "bid_price": 149,
                    "ask_price": 151,
                    "spread_percent": 1.33,
                },
            ),
            patch.object(
                trade_bot,
                "get_option_volume_vwap_analysis",
                return_value={"close": 100, "vwap": 98, "volume_ratio": 1.4},
            ),
            patch.object(trade_bot, "write_stream_instruments"),
        ):
            candidate = trade_bot.ganesh_gap_option_candidate(
                {"spot": 24310},
                {"option_type": "CE"},
            )

        self.assertEqual(candidate["expiry"], "2026-08-11")
        self.assertEqual(candidate["strike"], 24300)
        self.assertEqual(
            candidate["near_expiry_analysis"]["expiry"],
            "2026-08-04",
        )
        self.assertEqual(
            candidate["near_expiry_analysis"]["option_flow"]["vwap"],
            98,
        )

    def test_ganesh_skips_manually_held_atm_and_selects_nearest_itm(self):
        execution_expiry = "2026-08-11"
        recommendation = {
            "direction": "BULLISH",
            "confidence": "HIGH",
            "score": 4,
            "reasons": ["nearest chain bullish"],
            "analysis_expiry": "2026-08-04",
            "analysis_atm": {"strike": 24300, "expiry": "2026-08-04"},
            "execution_chain": [
                {
                    "strike": strike,
                    "expiry": execution_expiry,
                    "CE_ltp": 150,
                    "CE_bid_price": 149,
                    "CE_ask_price": 151,
                }
                for strike in (24250, 24300, 24350)
            ],
        }

        def instrument(_symbol, expiry, strike, option_type):
            return {
                "instrument_key": f"NSE_FO|{expiry}|{int(strike)}|{option_type}",
                "trading_symbol": f"NIFTY {int(strike)} {option_type} {expiry}",
                "lot_size": 65,
            }

        occupied_key = f"NSE_FO|{execution_expiry}|24300|CE"
        with (
            patch.dict(
                os.environ,
                {
                    "ENABLE_LIVE_TRADING": "true",
                    "GANESH_GAP_LIVE_TRADING": "true",
                },
                clear=False,
            ),
            patch.object(trade_bot, "get_index_recommendation", return_value=recommendation),
            patch.object(trade_bot, "find_index_option_instrument", side_effect=instrument),
            patch.object(
                trade_bot,
                "get_open_positions",
                return_value=[{"instrument_token": occupied_key, "quantity": 65}],
            ),
            patch.object(trade_bot, "read_market_cache", return_value={}),
            patch.object(
                trade_bot,
                "option_contract_quality",
                return_value={
                    "ltp": 150,
                    "bid_price": 149,
                    "ask_price": 151,
                    "spread_percent": 1.33,
                },
            ),
            patch.object(
                trade_bot,
                "get_option_volume_vwap_analysis",
                return_value={"close": 100, "vwap": 98, "volume_ratio": 1.4},
            ),
            patch.object(trade_bot, "write_stream_instruments"),
        ):
            candidate = trade_bot.ganesh_gap_option_candidate(
                {"symbol": "NIFTY", "spot": 24310},
                {"option_type": "CE"},
            )

        self.assertTrue(candidate["allowed"])
        self.assertEqual(candidate["strike"], 24250)
        self.assertTrue(candidate["contract_fallback_used"])
        self.assertIn("already held", candidate["contract_selection_attempts"][0])

    def test_ganesh_skips_manually_held_put_atm_and_selects_higher_itm(self):
        execution_expiry = "2026-08-11"
        recommendation = {
            "direction": "BEARISH",
            "confidence": "HIGH",
            "score": -4,
            "reasons": ["nearest chain bearish"],
            "analysis_expiry": "2026-08-04",
            "analysis_atm": {"strike": 24300, "expiry": "2026-08-04"},
            "execution_chain": [
                {
                    "strike": strike,
                    "expiry": execution_expiry,
                    "PE_ltp": 150,
                    "PE_bid_price": 149,
                    "PE_ask_price": 151,
                }
                for strike in (24250, 24300, 24350)
            ],
        }

        def instrument(_symbol, expiry, strike, option_type):
            return {
                "instrument_key": f"NSE_FO|{expiry}|{int(strike)}|{option_type}",
                "trading_symbol": f"NIFTY {int(strike)} {option_type} {expiry}",
                "lot_size": 65,
            }

        occupied_key = f"NSE_FO|{execution_expiry}|24300|PE"
        with (
            patch.dict(
                os.environ,
                {
                    "ENABLE_LIVE_TRADING": "true",
                    "GANESH_GAP_LIVE_TRADING": "true",
                },
                clear=False,
            ),
            patch.object(trade_bot, "get_index_recommendation", return_value=recommendation),
            patch.object(trade_bot, "find_index_option_instrument", side_effect=instrument),
            patch.object(
                trade_bot,
                "get_open_positions",
                return_value=[{"instrument_token": occupied_key, "quantity": 65}],
            ),
            patch.object(trade_bot, "read_market_cache", return_value={}),
            patch.object(
                trade_bot,
                "option_contract_quality",
                return_value={
                    "ltp": 150,
                    "bid_price": 149,
                    "ask_price": 151,
                    "spread_percent": 1.33,
                },
            ),
            patch.object(
                trade_bot,
                "get_option_volume_vwap_analysis",
                return_value={"close": 100, "vwap": 98, "volume_ratio": 1.4},
            ),
            patch.object(trade_bot, "write_stream_instruments"),
        ):
            candidate = trade_bot.ganesh_gap_option_candidate(
                {"symbol": "NIFTY", "spot": 24310},
                {"option_type": "PE"},
            )

        self.assertTrue(candidate["allowed"])
        self.assertEqual(candidate["strike"], 24350)
        self.assertTrue(candidate["contract_fallback_used"])

    def test_replay_uses_front_nifty_expiry_on_wednesday(self):
        session_date = datetime(2026, 7, 29).date()
        expiries = ["2026-08-04", "2026-08-11", "2026-08-18"]
        self.assertEqual(
            _choose_expiry("NIFTY", expiries, session_date),
            datetime(2026, 8, 4).date(),
        )

    def test_replay_uses_following_nifty_expiry_outside_wednesday(self):
        session_date = datetime(2026, 7, 30).date()
        expiries = ["2026-08-04", "2026-08-11", "2026-08-18"]
        self.assertEqual(
            _choose_expiry("NIFTY", expiries, session_date),
            datetime(2026, 8, 11).date(),
        )

    def test_market_orders_use_explicit_market_protection(self):
        instrument = {"instrument_key": "NSE_FO|1", "trading_symbol": "TEST"}
        with (
            patch.dict(os.environ, {"MARKET_PROTECTION_PERCENT": "3"}),
            patch.object(
                trade_bot,
                "upstox_request",
                return_value={"status": "success", "data": {"order_id": "1"}},
            ) as request,
        ):
            _, payload = trade_bot.place_market_order(instrument, "BUY", 65)
        self.assertEqual(payload["market_protection"], 3)
        self.assertEqual(request.call_count, 1)

    def test_long_index_option_requires_broker_protective_stop(self):
        with patch.dict(os.environ, {"BROKER_PROTECTIVE_STOP_ENABLED": "true"}):
            self.assertTrue(
                trade_bot.broker_protective_stop_required(
                    {"instrument_class": "INDEX_OPTION", "entry_transaction_type": "BUY"}
                )
            )

    def test_account_cap_limits_mobile_max_profile(self):
        with (
            patch.dict(os.environ, {"ACCOUNT_MAX_OPTION_CAPITAL": "100000"}),
            patch.object(trade_bot, "active_value", return_value=-1),
        ):
            self.assertEqual(trade_bot.option_capital_per_entry(), 100000)

    def test_manual_command_parses_symbol_side_and_symmetric_points(self):
        self.assertEqual(
            manual_index_trade.parse_request(["bank", "nifty", "call", "40"]),
            ("BANKNIFTY", "CALL", 40.0, 40.0),
        )
        self.assertEqual(
            manual_index_trade.parse_request(["nifty", "20", "15"]),
            ("NIFTY", None, 20.0, 15.0),
        )

    def test_manual_candidate_forces_max_capital_and_preserves_score(self):
        with patch.object(
            trade_bot,
            "index_point_exit_settings",
            return_value={"target_points": 20, "stop_points": 20, "delta": 0.5},
        ):
            chosen = manual_index_trade.build_manual_candidate(
                {
                    "symbol": "NIFTY",
                    "direction": "BEARISH",
                    "entry_price": 100,
                    "instrument": {"instrument_key": "NSE_FO|1", "lot_size": 65},
                    "option_summary": {"option_type": "PE"},
                    "technicals": {},
                    "weighted": {"score": 68, "grade": "CAUTIOUS_TRADE"},
                },
                20,
                20,
                "nifty 20",
            )

        self.assertEqual(chosen["direction"], "BEARISH")
        self.assertEqual(chosen["capital_override"], "MAX")
        self.assertTrue(chosen["manual_override"])
        self.assertEqual(chosen["target_price"], 110)
        self.assertEqual(chosen["stop_loss_price"], 90)

    def test_completed_candles_excludes_still_forming_interval(self):
        index = pd.DatetimeIndex(
            ["2026-07-20 10:00:00+05:30", "2026-07-20 10:15:00+05:30"]
        )
        frame = pd.DataFrame(
            [{"close": 100}, {"close": 101}],
            index=index,
        )
        result = completed_candles(
            frame,
            15,
            current_time=pd.Timestamp("2026-07-20 10:20:00+05:30"),
            grace_seconds=5,
        )
        self.assertEqual(list(result["close"]), [100])

    def test_watch_confirmation_requires_directional_breakout_body_and_close(self):
        base = {"open": 104, "high": 105, "low": 99, "close": 100}
        confirmation = {"open": 100, "high": 112, "low": 99, "close": 111}
        result = candle_confirmation(base, confirmation, "BULLISH")
        self.assertTrue(result["confirmed"])
        self.assertIn("BULLISH_ENGULFING", result["patterns"])

        weak = candle_confirmation(
            base,
            {"open": 104, "high": 108, "low": 100, "close": 105.2},
            "BULLISH",
        )
        self.assertFalse(weak["confirmed"])

    def test_live_watch_confirms_only_next_completed_fifteen_minute_candle(self):
        candidate = {
            "allowed": False,
            "watch_eligible": True,
            "symbol": "NIFTY",
            "direction": "BULLISH",
            "weighted": {"score": 58},
            "option_summary": {
                "chain_bias": "BULLISH",
                "chain_confidence": "HIGH",
            },
            "technicals": {
                "five_min": {"bias": "BULLISH"},
                "fifteen_min": {
                    "bias": "BULLISH",
                    "candle_time": "2026-07-20T10:15:00+05:30",
                    "open": 100,
                    "high": 112,
                    "low": 99,
                    "close": 111,
                },
                "atm_option_flow": {
                    "close": 110,
                    "vwap": 100,
                    "volume_ratio": 1.5,
                },
            },
        }
        watch = {
            "symbol": "NIFTY",
            "direction": "BULLISH",
            "base_score": 58,
            "started_at": "2026-07-20T10:15:05+05:30",
            "base_candle": {
                "candle_time": "2026-07-20T10:00:00+05:30",
                "open": 104,
                "high": 105,
                "low": 99,
                "close": 100,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(trade_bot, "WATCH_STATE_DIR", Path(temp_dir)),
                patch.object(
                    trade_bot,
                    "now_ist",
                    return_value=datetime.fromisoformat("2026-07-20T10:30:10+05:30"),
                ),
                patch.dict(
                    os.environ,
                    {
                        "INDEX_WATCH_MODE_ENABLED": "true",
                        "INDEX_WATCH_MODE_SHADOW_ONLY": "false",
                        "VAMSI_UNIFIED_SCORE_FALLBACK": "55",
                    },
                    clear=False,
                ),
            ):
                trade_bot.write_watch_state("NIFTY", watch)
                confirmed = trade_bot.process_watch("NIFTY", candidate)

        self.assertTrue(confirmed["allowed"])
        self.assertTrue(confirmed["watch_confirmed"])
        self.assertEqual(confirmed["entry_minimum_score"], 55)
        self.assertTrue(confirmed["score_cutoff_approved"])

    def test_timing_watch_confirms_on_next_completed_five_minute_candle(self):
        candidate = {
            "allowed": True,
            "symbol": "NIFTY",
            "direction": "BULLISH",
            "weighted": {"score": 58},
            "option_summary": {
                "chain_bias": "BULLISH",
                "chain_confidence": "HIGH",
            },
            "technicals": {
                "five_min": {
                    "bias": "BULLISH",
                    "candle_time": "2026-07-20T10:05:00+05:30",
                    "open": 100,
                    "high": 112,
                    "low": 99,
                    "close": 111,
                },
                "fifteen_min": {"bias": "BULLISH"},
                "atm_option_flow": {
                    "close": 110,
                    "vwap": 100,
                    "volume_ratio": 1.5,
                },
            },
        }
        watch = {
            "symbol": "NIFTY",
            "direction": "BULLISH",
            "base_score": 58,
            "started_at": "2026-07-20T10:00:05+05:30",
            "confirmation_timeframe": "5M",
            "base_candle": {
                "candle_time": "2026-07-20T10:00:00+05:30",
                "open": 104,
                "high": 105,
                "low": 99,
                "close": 100,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(trade_bot, "WATCH_STATE_DIR", Path(temp_dir)),
                patch.object(
                    trade_bot,
                    "now_ist",
                    return_value=datetime.fromisoformat("2026-07-20T10:10:10+05:30"),
                ),
                patch.dict(
                    os.environ,
                    {
                        "INDEX_WATCH_MODE_ENABLED": "true",
                        "INDEX_WATCH_MODE_SHADOW_ONLY": "false",
                    },
                    clear=False,
                ),
            ):
                trade_bot.write_watch_state("NIFTY", watch)
                confirmed = trade_bot.process_watch("NIFTY", candidate)

        self.assertTrue(confirmed["allowed"])
        self.assertTrue(confirmed["watch_confirmed"])

    def test_live_daily_first_outcome_guard_is_independent_by_index(self):
        today = datetime(2026, 7, 20, 10, 0)
        history = "\n".join([
            "trade_date,symbol,instrument_class,gross_pnl",
            "2026-07-20,NIFTY,INDEX_OPTION,1250",
            "2026-07-20,BANKNIFTY,INDEX_OPTION,-800",
            "2026-07-19,NIFTY,INDEX_OPTION,-500",
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "trade_history.csv"
            history_path.write_text(history)
            with (
                patch.object(trade_bot, "TRADE_HISTORY_FILE", history_path),
                patch.object(trade_bot, "now_ist", return_value=today),
                patch.dict(os.environ, {"STOP_AFTER_FIRST_PROFIT_OR_LOSS": "true"}),
            ):
                nifty_reason = trade_bot.daily_index_entry_block_reason("NIFTY")
                bank_reason = trade_bot.daily_index_entry_block_reason("BANKNIFTY")

        self.assertIn("PROFIT", nifty_reason)
        self.assertIn("1250.00", nifty_reason)
        self.assertIn("LOSS", bank_reason)
        self.assertIn("-800.00", bank_reason)

    def test_live_daily_first_outcome_guard_can_be_disabled(self):
        with patch.dict(os.environ, {"STOP_AFTER_FIRST_PROFIT_OR_LOSS": "false"}):
            self.assertEqual(trade_bot.daily_index_entry_block_reason("NIFTY"), "")

    def test_first_outcome_paper_mode_allows_later_simulated_entries(self):
        today = datetime(2026, 7, 20, 10, 0)
        history = "\n".join([
            "trade_date,symbol,instrument_class,strategy,gross_pnl",
            "2026-07-20,NIFTY,INDEX_OPTION,SELECTIVE,1250",
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "trade_history.csv"
            history_path.write_text(history)
            with (
                patch.object(trade_bot, "TRADE_HISTORY_FILE", history_path),
                patch.object(trade_bot, "now_ist", return_value=today),
                patch.dict(
                    os.environ,
                    {
                        "STOP_AFTER_FIRST_PROFIT_OR_LOSS": "true",
                        "AFTER_FIRST_OUTCOME_MODE": "paper",
                        "MAX_INDEX_TRADES_PER_DAY": "0",
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(trade_bot.daily_index_entry_block_reason("NIFTY"), "")
                self.assertTrue(trade_bot.paper_after_first_outcome("NIFTY"))

    def test_flat_first_real_trade_also_switches_later_entries_to_paper(self):
        today = datetime(2026, 7, 20, 10, 0)
        history = "\n".join([
            "trade_date,symbol,instrument_class,strategy,gross_pnl",
            "2026-07-20,NIFTY,INDEX_OPTION,SELECTIVE,0",
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "trade_history.csv"
            history_path.write_text(history)
            with (
                patch.object(trade_bot, "TRADE_HISTORY_FILE", history_path),
                patch.object(trade_bot, "now_ist", return_value=today),
                patch.dict(
                    os.environ,
                    {
                        "STOP_AFTER_FIRST_PROFIT_OR_LOSS": "true",
                        "AFTER_FIRST_OUTCOME_MODE": "paper",
                    },
                    clear=False,
                ),
            ):
                self.assertTrue(trade_bot.paper_after_first_outcome("NIFTY"))

    def test_adaptive_policy_controls_how_many_outcomes_remain_live(self):
        policy = {"global_status": "LIVE_ENABLED", "maximum_live_trades": 2}
        outcome = {"outcome": "PROFIT", "gross_pnl": 1000}
        with (
            patch.object(trade_bot, "adaptive_live_policy_today", return_value=policy),
            patch.object(trade_bot, "first_index_trade_outcome_today", return_value=outcome),
            patch.object(trade_bot, "today_index_trade_rows", return_value=[{"id": 1}]),
        ):
            self.assertFalse(trade_bot.paper_after_first_outcome("NIFTY"))
        with (
            patch.object(trade_bot, "adaptive_live_policy_today", return_value=policy),
            patch.object(trade_bot, "first_index_trade_outcome_today", return_value=outcome),
            patch.object(
                trade_bot,
                "today_index_trade_rows",
                return_value=[{"id": 1}, {"id": 2}],
            ),
        ):
            self.assertTrue(trade_bot.paper_after_first_outcome("NIFTY"))

    def test_adaptive_live_entry_decision_uses_effective_policy_cell(self):
        policy = {
            "global_status": "LIVE_ENABLED",
            "cells": {
                "11:00-12:59|65-69": {
                    "status": "LIVE_ENABLED",
                    "cell_id": "11:00-12:59|65-69",
                    "score_band": "65-69",
                    "proposed_target_points": 27,
                    "proposed_stop_points": 33,
                }
            },
        }
        with patch.object(trade_bot, "adaptive_live_policy_today", return_value=policy):
            decision = trade_bot.adaptive_live_entry_decision(
                66,
                datetime.fromisoformat("2026-07-20T11:15:00+05:30"),
            )

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["cell"]["proposed_target_points"], 27)

    def test_paper_trade_is_excluded_from_live_bot_pnl_controls(self):
        self.assertFalse(
            trade_bot.is_bot_trade_history_row(
                {
                    "strategy": "SELECTIVE_PAPER",
                    "trading_symbol": "NIFTY26AUG25000CE",
                    "exit_reason": "TARGET",
                }
            )
        )

    def test_selective_paper_position_uses_target_exit_without_broker_order(self):
        state = {
            "date": "2026-07-20",
            "symbol": "NIFTY",
            "underlying_symbol": "NIFTY",
            "instrument_class": "INDEX_OPTION",
            "strategy": "SELECTIVE_PAPER",
            "paper_trade": True,
            "entry_order_id": "PAPER-20260720100000",
            "entry_transaction_type": "BUY",
            "status": "POSITION_OPEN",
            "instrument_key": "NSE_FO|PAPER_NIFTY",
            "trading_symbol": "NIFTY26JUL25000CE",
            "quantity": 65,
            "lot_size": 65,
            "entry_price": 100.0,
            "target_price": 110.0,
            "planned_target_price": 110.0,
            "profit_booking_price": 110.0,
            "profit_booking_mode": "runner",
            "stop_loss_price": 90.0,
            "original_stop_loss_price": 90.0,
            "direction": "BULLISH",
            "created_at": "2026-07-20T10:00:00+05:30",
            "highest_ltp": 100.0,
            "lowest_ltp": 100.0,
        }
        paper_row = {
            "entry_price": 100.0,
            "exit_price": 110.0,
            "gross_pnl": 650.0,
        }
        with (
            patch.object(trade_bot, "read_state", return_value=state),
            patch.object(trade_bot, "write_state"),
            patch.object(
                trade_bot,
                "read_market_cache",
                side_effect=lambda key: {"ltp": 110.0, "received_at": 1},
            ),
            patch.object(trade_bot, "read_recent_ticks", return_value=[]),
            patch.object(trade_bot, "sentiment_exit_enabled_for_state", return_value=False),
            patch.object(trade_bot, "underlying_exit_reason", return_value=None),
            patch.object(trade_bot, "record_closed_trade", return_value=paper_row) as record,
            patch.object(trade_bot, "clear_state") as clear,
            patch.object(trade_bot, "place_market_order") as broker_order,
            patch.object(trade_bot, "find_matching_position_for_side") as broker_position,
        ):
            handled = trade_bot.handle_existing_state("NIFTY", state)

        self.assertTrue(handled)
        record.assert_called_once_with(state, 110.0, "TARGET")
        clear.assert_called_once_with("NIFTY")
        broker_order.assert_not_called()
        broker_position.assert_not_called()

    def test_daily_loss_guard_can_be_relaxed_without_disabling_profit_guard(self):
        today = datetime(2026, 7, 20, 10, 0)
        history = "\n".join([
            "trade_date,symbol,instrument_class,gross_pnl",
            "2026-07-20,NIFTY,INDEX_OPTION,-800",
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "trade_history.csv"
            history_path.write_text(history)
            with (
                patch.object(trade_bot, "TRADE_HISTORY_FILE", history_path),
                patch.object(trade_bot, "now_ist", return_value=today),
                patch.dict(
                    os.environ,
                    {
                        "STOP_AFTER_FIRST_PROFIT_OR_LOSS": "true",
                        "STOP_AFTER_FIRST_LOSS": "false",
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(trade_bot.daily_index_entry_block_reason("NIFTY"), "")

    def test_replay_sizes_each_option_entry_to_one_lakh(self):
        nifty_quantity, nifty_lots = capital_sized_option_quantity(100, 65, 100000)
        bank_quantity, bank_lots = capital_sized_option_quantity(500, 30, 100000)

        self.assertEqual((nifty_quantity, nifty_lots), (975, 15))
        self.assertEqual((bank_quantity, bank_lots), (180, 6))

    def test_replay_allows_nifty_and_banknifty_at_same_timestamp(self):
        day = pd.Timestamp("2026-07-15").date()
        first_time = pd.Timestamp("2026-07-15 09:20", tz="Asia/Kolkata")
        spot_frame = pd.DataFrame(
            [{"close": 100}, {"close": 101}],
            index=[first_time, first_time + pd.Timedelta(minutes=5)],
        )

        class ReplayData:
            def get_expiries(self, instrument_key):
                return [day, day + timedelta(days=7)]

        engine = StrategyReplay(
            ReplayData(),
            day,
            day,
            include_stock_futures=False,
            portfolio_mode="live",
        )
        engine.allow_simultaneous_index_positions = True

        def candidate_for(symbol, timestamp, *args):
            transaction_type = args[-1]
            if transaction_type != "BUY":
                return None
            return Candidate(
                category=f"{symbol}_OPTION_BUY",
                symbol=symbol,
                direction="BULLISH",
                transaction_type="BUY",
                contract={"lot_size": 1, "trading_symbol": symbol},
                signal_time=timestamp,
                expected_entry=100,
                target=110,
                stop=90,
                score=80,
                grade="TRADE",
                reason="test",
            )

        def simulate(candidate, replay_day):
            return {
                "symbol": candidate.symbol,
                "signal_time": candidate.signal_time.isoformat(),
                "exit_time": (candidate.signal_time + pd.Timedelta(minutes=5)).isoformat(),
            }

        with (
            patch.object(engine, "_candles", return_value=spot_frame),
            patch.object(engine, "_option_contracts", return_value=[]),
            patch.object(engine, "_candidate", side_effect=candidate_for),
            patch.object(engine, "_simulate", side_effect=simulate),
        ):
            trades, _, _ = engine.run()

        simultaneous = [
            trade for trade in trades if trade["signal_time"] == first_time.isoformat()
        ]
        self.assertEqual({trade["symbol"] for trade in simultaneous}, {"NIFTY", "BANKNIFTY"})

    def test_replay_daily_policy_is_applied_per_index(self):
        day = pd.Timestamp("2026-07-15").date()
        first_time = pd.Timestamp("2026-07-15 09:20", tz="Asia/Kolkata")
        times = [first_time + pd.Timedelta(minutes=5 * index) for index in range(8)]
        spot_frame = pd.DataFrame(
            [{"close": 100 + index} for index in range(len(times))],
            index=times,
        )

        class ReplayData:
            def get_expiries(self, instrument_key):
                return [day, day + timedelta(days=7)]

        def run_policy(
            max_trades=0,
            stop_after_win=False,
            stop_after_loss=False,
            gross_pnl=100,
        ):
            engine = StrategyReplay(
                ReplayData(), day, day, include_stock_futures=False, portfolio_mode="live"
            )
            engine.allow_simultaneous_index_positions = True
            engine.max_trades_per_day_per_index = max_trades
            engine.stop_after_first_win = stop_after_win
            engine.stop_after_first_loss = stop_after_loss

            def candidate_for(symbol, timestamp, *args):
                if args[-1] != "BUY":
                    return None
                return Candidate(
                    category=f"{symbol}_OPTION_BUY",
                    symbol=symbol,
                    direction="BULLISH",
                    transaction_type="BUY",
                    contract={"lot_size": 1, "trading_symbol": symbol},
                    signal_time=timestamp,
                    expected_entry=100,
                    target=110,
                    stop=90,
                    score=80,
                    grade="TRADE",
                    reason="test",
                )

            def simulate(candidate, replay_day):
                return {
                    "symbol": candidate.symbol,
                    "signal_time": candidate.signal_time.isoformat(),
                    "exit_time": (
                        candidate.signal_time + pd.Timedelta(minutes=5)
                    ).isoformat(),
                    "gross_pnl": gross_pnl,
                    "estimated_costs": 0,
                }

            with (
                patch.object(engine, "_candles", return_value=spot_frame),
                patch.object(engine, "_option_contracts", return_value=[]),
                patch.object(engine, "_candidate", side_effect=candidate_for),
                patch.object(engine, "_simulate", side_effect=simulate),
            ):
                trades, _, _ = engine.run()
            return trades

        capped = run_policy(max_trades=2)
        stopped_after_win = run_policy(stop_after_win=True)
        stopped_after_loss = run_policy(stop_after_loss=True, gross_pnl=-100)
        self.assertEqual(
            {symbol: sum(trade["symbol"] == symbol for trade in capped) for symbol in ("NIFTY", "BANKNIFTY")},
            {"NIFTY": 2, "BANKNIFTY": 2},
        )
        self.assertEqual(
            {
                symbol: sum(trade["symbol"] == symbol for trade in stopped_after_win)
                for symbol in ("NIFTY", "BANKNIFTY")
            },
            {"NIFTY": 1, "BANKNIFTY": 1},
        )
        self.assertEqual(
            {
                symbol: sum(trade["symbol"] == symbol for trade in stopped_after_loss)
                for symbol in ("NIFTY", "BANKNIFTY")
            },
            {"NIFTY": 1, "BANKNIFTY": 1},
        )

    def test_mobile_dashboard_calculates_short_option_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = {
                "instrument_key": "NSE_FO|SHORT_TEST",
                "trading_symbol": "NIFTY TEST PE",
                "direction": "BULLISH",
                "entry_transaction_type": "SELL",
                "position_side": "SHORT_OPTION",
                "quantity": 65,
                "entry_price": 100,
                "target_price": 80,
                "stop_loss_price": 110,
                "lowest_ltp": 88,
                "status": "POSITION_OPEN",
            }
            Path(temp_dir, "trade_state_NIFTY.json").write_text(
                json.dumps(state)
            )

            broker_positions = [{
                "instrument_token": "NSE_FO|SHORT_TEST",
                "quantity": -65,
                "sell_price": 100,
                "last_price": 90,
            }]

            with (
                patch.object(dashboard_data, "BASE_DIR", Path(temp_dir)),
                patch.object(
                    dashboard_data,
                    "fetch_upstox_positions",
                    return_value=(broker_positions, None),
                ),
            ):
                live = dashboard_data.build_live_positions()

        position = live["positions"][0]
        self.assertEqual(position["transactionType"], "SELL")
        self.assertEqual(position["positionSide"], "SHORT_OPTION")
        self.assertEqual(position["livePnL"], 650)
        self.assertEqual(position["targetProgress"], 50)
        self.assertEqual(position["riskToStop"], 1300)
        self.assertEqual(position["rewardLeft"], 650)

    def test_mobile_dashboard_builds_strategy_category_summaries(self):
        trades = [
            {
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "transactionType": "SELL",
                "positionSide": "SHORT_OPTION",
                "grossPnL": 1000,
            },
            {
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "transactionType": "BUY",
                "positionSide": "LONG_OPTION",
                "grossPnL": -400,
            },
            {
                "symbol": "STOCK_FUTURE",
                "underlyingSymbol": "BANKNIFTY",
                "instrumentClass": "STOCK_FUTURE",
                "transactionType": "SELL",
                "positionSide": "SHORT_FUTURE",
                "grossPnL": 750,
            },
        ]

        summaries = dashboard_data.category_performance(trades)
        by_key = {
            (item["symbol"], item["category"]): item
            for item in summaries
        }

        self.assertEqual(
            by_key[("NIFTY", "OPTION_SELL")]["tradeCount"],
            1,
        )
        self.assertEqual(
            by_key[("NIFTY", "OPTION_SELL")]["cumulativePnL"],
            1000,
        )
        self.assertEqual(
            by_key[("NIFTY", "OPTION_BUY")]["winRate"],
            0,
        )
        self.assertEqual(
            by_key[("ALL", "STOCK_FUTURES")]["cumulativePnL"],
            750,
        )

    def test_edge_analytics_groups_expectancy_by_score_and_entry_time(self):
        trades = [
            {
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "entryTime": "2026-07-20T09:35:00+05:30",
                "score": 64.4,
                "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
                "grossPnL": 1000,
            },
            {
                "symbol": "BANKNIFTY",
                "underlyingSymbol": "BANKNIFTY",
                "instrumentClass": "INDEX_OPTION",
                "entryTime": "2026-07-21T09:45:00+05:30",
                "score": 62.8,
                "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
                "grossPnL": 3000,
            },
            {
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "entryTime": "2026-07-21T10:30:00+05:30",
                "score": 59.4,
                "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
                "grossPnL": -1000,
            },
            {
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "entryTime": "2026-07-21T11:30:00+05:30",
                "score": None,
                "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
                "grossPnL": 500,
            },
            {
                "symbol": "BANKNIFTY",
                "underlyingSymbol": "BANKNIFTY",
                "instrumentClass": "INDEX_OPTION",
                "entryTime": "2026-07-21T12:00:00+05:30",
                "score": 0,
                "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
                "grossPnL": -200,
            },
            {
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "entryTime": "2026-07-21T13:30:00+05:30",
                "score": 72,
                "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
                "grossPnL": 400,
            },
            {
                "symbol": "BANKNIFTY",
                "underlyingSymbol": "BANKNIFTY",
                "instrumentClass": "INDEX_OPTION",
                "entryTime": "",
                "score": 84,
                "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
                "grossPnL": 800,
            },
        ]

        analytics = dashboard_data.edge_analytics(trades)
        opening_high = next(
            cell
            for cell in analytics["matrix"]
            if cell["timeBucket"] == "opening"
            and cell["scoreBand"] == "60-69"
        )
        unknown_time = next(
            cell
            for cell in analytics["matrix"]
            if cell["timeBucket"] == "unknown"
            and cell["scoreBand"] == "80-89"
        )
        unscored = next(
            cell
            for cell in analytics["matrix"]
            if cell["timeBucket"] == "late_morning"
            and cell["scoreBand"] == "Unscored"
        )

        self.assertEqual(analytics["totalTrades"], 7)
        self.assertEqual(analytics["symbolTrades"], {"NIFTY": 4, "BANKNIFTY": 3})
        self.assertEqual(
            analytics["scoreBands"],
            [
                "0-9", "10-19", "20-29", "30-39", "40-49",
                "50-59", "60-69", "70-79", "80-89", "90-100", "Unscored",
            ],
        )
        self.assertEqual(analytics["scoreVersion"], "VAMSI_UNIFIED_ENTRY_V1")
        self.assertEqual(opening_high["expectancy"], 2000)
        self.assertEqual(opening_high["trades"], 2)
        self.assertEqual(unknown_time["trades"], 1)
        self.assertEqual(unscored["trades"], 1)
        self.assertEqual(analytics["bestZone"]["timeBucket"], "opening")
        self.assertEqual(analytics["bestZone"]["scoreBand"], "60-69")

    def test_edge_score_bands_cover_the_zero_to_one_hundred_scale(self):
        cases = {
            0: "0-9",
            9.9: "0-9",
            10: "10-19",
            49.9: "40-49",
            50: "50-59",
            60: "60-69",
            65: "60-69",
            70: "70-79",
            80: "80-89",
            90: "90-100",
            100: "90-100",
            -1: "Unscored",
            101: "Unscored",
        }
        for score, expected in cases.items():
            with self.subTest(score=score):
                self.assertEqual(
                    dashboard_data.edge_score_band(
                        score,
                        "VAMSI_UNIFIED_ENTRY_V1",
                    ),
                    expected,
                )
        self.assertEqual(
            dashboard_data.edge_score_band(75, "LEGACY_WEIGHTED_SCORE"),
            "70-79",
        )
        self.assertEqual(
            dashboard_data.edge_score_band(55, ""),
            "50-59",
        )

    def test_option_type_recognizes_put_token_inside_trading_symbol(self):
        trade = {"tradingSymbol": "NIFTY 24200 PE 28 JUL 26"}
        self.assertEqual(dashboard_data.option_type(trade), "PUT")

    def test_unknown_option_type_is_not_mislabeled_as_call(self):
        self.assertEqual(dashboard_data.option_type({"tradingSymbol": "N/A"}), "UNKNOWN")

    def test_mobile_dashboard_prefers_today_bot_journal_without_broker_lag(self):
        trades = [
            {
                "tradeDate": "2026-07-20",
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "tradingSymbol": "NIFTY 24200 PE 28 JUL 26",
                "optionType": "PUT",
                "grossPnL": 1984.45,
                "exitTime": "2026-07-20T11:18:00+05:30",
            }
        ]
        with (
            patch.object(dashboard_data, "read_trade_history", return_value=trades),
            patch.object(
                dashboard_data,
                "fetch_upstox_today_pnl",
                return_value=(0.0, None, 0, {}, {}),
            ),
            patch.object(dashboard_data, "datetime") as mocked_datetime,
        ):
            mocked_datetime.now.return_value = datetime(2026, 7, 20, 12, 0)
            performance = dashboard_data.build_trade_performance()

        self.assertEqual(performance["today"]["closedTrades"], 1)
        self.assertEqual(performance["today"]["closedPnL"], 1984.45)
        self.assertEqual(performance["today"]["closedPnLSource"], "TRADE_LOG")
        put_rows = [
            row
            for row in performance["today"]["optionTypePerformance"]
            if row["optionType"] == "PUT" and row["symbol"] == "NIFTY"
        ]
        self.assertEqual(put_rows[0]["netPnL"], 1984.45)

    def test_mobile_dashboard_keeps_main_pnl_bot_only_with_synced_upstox_rows(self):
        trades = [
            {
                "tradeDate": "2026-07-22",
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "tradingSymbol": "NIFTY 24200 PE 28 JUL 26",
                "optionType": "PUT",
                "grossPnL": -4500.0,
                "exitTime": "2026-07-22T10:30:00+05:30",
                "exitReason": "TARGET",
            },
            {
                "tradeDate": "2026-07-22",
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "tradingSymbol": "UPSTOX SYNC NIFTY PUT",
                "optionType": "PUT",
                "grossPnL": 19500.0,
                "exitTime": "2026-07-22T13:00:00+05:30",
                "exitReason": "UPSTOX_SYNC_ADJUSTMENT",
            },
        ]
        with (
            patch.object(dashboard_data, "read_trade_history", return_value=trades),
            patch.object(
                dashboard_data,
                "fetch_upstox_today_pnl",
                return_value=(15000.0, None, 2, {"NIFTY": 15000.0}, {"NIFTY": 2}),
            ),
            patch.object(dashboard_data, "datetime") as mocked_datetime,
        ):
            mocked_datetime.now.return_value = datetime(2026, 7, 22, 13, 5)
            performance = dashboard_data.build_trade_performance()

        self.assertEqual(performance["today"]["closedPnL"], -4500.0)
        self.assertEqual(performance["today"]["botPnL"], -4500.0)
        self.assertIsNone(performance["today"]["manualOtherPnL"])
        self.assertIsNone(performance["today"]["totalUpstoxPnL"])
        self.assertIsNone(performance["today"]["closedPnLError"])
        self.assertEqual(performance["equityCurve"][-1]["dailyPnL"], -4500.0)
        put_rows = [
            row
            for row in performance["cumulative"]["optionTypePerformance"]
            if row["optionType"] == "PUT" and row["symbol"] == "NIFTY"
        ]
        self.assertEqual(put_rows[0]["netPnL"], -4500.0)

    def test_mobile_dashboard_excludes_stock_options_from_all_performance(self):
        trades = [
            {
                "tradeDate": "2026-07-22",
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "tradingSymbol": "NIFTY 24200 CE 28 JUL 26",
                "optionType": "CALL",
                "grossPnL": 6200.0,
                "quantity": 65,
                "entryPrice": 120.0,
                "exitPrice": 215.38,
                "transactionType": "BUY",
                "exitTime": "2026-07-22T10:30:00+05:30",
            },
            {
                "tradeDate": "2026-07-22",
                "symbol": "RELIANCE",
                "underlyingSymbol": "RELIANCE",
                "instrumentClass": "STOCK_OPTION",
                "tradingSymbol": "RELIANCE 3000 PE 28 JUL 26",
                "optionType": "PUT",
                "grossPnL": -2400.0,
                "exitTime": "2026-07-22T11:00:00+05:30",
            },
            {
                "tradeDate": "2026-07-21",
                "symbol": "RELIANCE",
                "underlyingSymbol": "RELIANCE",
                "instrumentClass": "STOCK_OPTION",
                "tradingSymbol": "RELIANCE 3000 CE 28 JUL 26",
                "optionType": "CALL",
                "grossPnL": 5100.0,
                "exitTime": "2026-07-21T11:00:00+05:30",
            },
            {
                "tradeDate": "2026-07-22",
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "STOCK_FUTURE",
                "tradingSymbol": "NIFTY FUT",
                "grossPnL": 9000.0,
                "exitTime": "2026-07-22T12:00:00+05:30",
            },
            {
                "tradeDate": "2026-07-22",
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "tradingSymbol": "UPSTOX SYNC NIFTY CALL",
                "optionType": "CALL",
                "grossPnL": 15000.0,
                "exitTime": "2026-07-22T13:00:00+05:30",
                "exitReason": "UPSTOX_SYNC_ADJUSTMENT",
            },
        ]
        with (
            patch.object(dashboard_data, "read_trade_history", return_value=trades),
            patch.object(dashboard_data, "datetime") as mocked_datetime,
        ):
            mocked_datetime.now.return_value = datetime(2026, 7, 22, 13, 5)
            performance = dashboard_data.build_trade_performance()

        self.assertEqual(performance["today"]["closedTrades"], 1)
        self.assertEqual(performance["today"]["closedPnL"], 6200.0)
        self.assertEqual(performance["cumulative"]["totalPnL"], 6200.0)
        self.assertEqual(performance["equityCurve"][-1]["dailyPnL"], 6200.0)
        self.assertIn("netPnL", performance["today"])
        self.assertNotIn("otherCharges", performance["today"])
        self.assertIn("symbolStats", performance["today"])
        self.assertIn("symbolStats", performance["cumulative"])
        self.assertEqual(performance["today"]["symbolStats"]["NIFTY"]["trades"], 1)
        self.assertEqual(performance["today"]["symbolStats"]["BANKNIFTY"]["trades"], 0)
        self.assertEqual(performance["today"]["netPnL"], performance["today"]["closedPnL"])

    def test_confirmed_entry_notification_contains_trade_plan(self):
        position_state = {
            "symbol": "NIFTY",
            "trading_symbol": "NIFTY 23JUL26 25000 CE",
            "direction": "BULLISH",
            "quantity": 65,
            "entry_price": 150.25,
            "target_price": 165.0,
            "stop_loss_price": 142.5,
            "created_at": "2026-07-15T10:15:00+05:30",
        }

        with (
            patch.object(apns_push, "apns_is_configured", return_value=True),
            patch.object(apns_push, "registered_device_count", return_value=1),
            patch.object(apns_push, "_send_payload") as send_payload,
            patch.dict(os.environ, {"TRADING_PROFILE": "Ganesh"}, clear=False),
        ):
            apns_push.send_trade_entered_notification(position_state)

        payload = send_payload.call_args.args[0]
        self.assertEqual(payload["eventType"], "tradeEntered")
        self.assertEqual(payload["profile"], "Ganesh")
        self.assertEqual(payload["quantity"], 65)
        self.assertIn("NIFTY 23JUL26 25000 CE", payload["aps"]["alert"]["body"])
        self.assertIn("Target ₹165.00", payload["aps"]["alert"]["body"])
        self.assertIn("Stop ₹142.50", payload["aps"]["alert"]["body"])

    def test_entry_notification_is_sent_only_once_per_broker_order(self):
        position_state = {
            "date": "2026-08-03",
            "entry_order_id": "ORDER-123",
            "symbol": "NIFTY",
            "instrument_key": "NSE_FO|123",
            "created_at": "2026-08-03T11:50:00+05:30",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_file = Path(temp_dir) / "receipts.json"
            receipt_lock = Path(temp_dir) / "receipts.lock"
            with (
                patch.object(
                    trade_bot,
                    "ENTRY_NOTIFICATION_RECEIPTS_FILE",
                    receipt_file,
                ),
                patch.object(
                    trade_bot,
                    "ENTRY_NOTIFICATION_RECEIPTS_LOCK_FILE",
                    receipt_lock,
                ),
                patch.object(
                    trade_bot,
                    "send_trade_entered_notification",
                    return_value={"sent": 2, "failed": 0},
                ) as send,
            ):
                first = trade_bot.send_apple_trade_entered_alert(position_state)
                second = trade_bot.send_apple_trade_entered_alert(position_state)

        self.assertEqual(first, {"sent": 2, "failed": 0})
        self.assertTrue(second["duplicate"])
        send.assert_called_once_with(position_state)

    def test_counterfactual_replay_uses_conservative_candle_ordering(self):
        signal_time = pd.Timestamp("2026-07-15 10:20:00", tz="Asia/Kolkata")
        candles = pd.DataFrame(
            [
                {"open": 100, "high": 111, "low": 91, "close": 105, "volume": 1},
            ],
            index=[pd.Timestamp("2026-07-15 10:25:00", tz="Asia/Kolkata")],
        )

        result = simulate_trade(candles, signal_time, 100, 110, 92, 65)

        self.assertEqual(result["exit_reason"], "STOP_AND_TARGET_SAME_CANDLE")
        self.assertEqual(result["gross_pnl"], -520)

    def test_counterfactual_short_trade_profits_when_premium_reaches_lower_target(self):
        signal_time = pd.Timestamp("2026-07-15 10:20:00", tz="Asia/Kolkata")
        candles = pd.DataFrame(
            [
                {"open": 100, "high": 101, "low": 89, "close": 91, "volume": 1},
            ],
            index=[pd.Timestamp("2026-07-15 10:25:00", tz="Asia/Kolkata")],
        )

        result = simulate_trade(
            candles,
            signal_time,
            100,
            90,
            108,
            30,
            transaction_type="SELL",
        )

        self.assertEqual(result["exit_reason"], "TARGET")
        self.assertEqual(result["gross_pnl"], 300)

    def test_signal_check_executes_best_qualified_index_candidate(self):
        candidates = {
            "NIFTY": {
                "symbol": "NIFTY",
                "transaction_type": "BUY",
                "weighted": {"score": 82},
            },
            "BANKNIFTY": {
                "symbol": "BANKNIFTY",
                "transaction_type": "BUY",
                "weighted": {"score": 88},
            },
        }

        with (
            patch.object(trade_bot, "market_window_ok", return_value=True),
            patch.object(trade_bot, "read_state", return_value={}),
            patch.object(
                trade_bot,
                "portfolio_day_circuit",
                return_value={"allowed": True, "score_penalty": 0},
            ),
            patch.object(trade_bot, "get_open_positions", return_value=[]),
            patch.object(
                trade_bot,
                "evaluate_symbol_buy_or_sell",
                side_effect=lambda symbol, **kwargs: candidates[symbol],
            ),
            patch.object(trade_bot, "execute_selected_candidate") as execute,
        ):
            trade_bot.run_signal_check()

        execute.assert_called_once_with(candidates["BANKNIFTY"])

    def test_trade_bank_nifty_false_scans_and_trades_only_nifty(self):
        nifty_candidate = {
            "symbol": "NIFTY",
            "transaction_type": "BUY",
            "weighted": {"score": 82},
        }

        with (
            patch.dict(os.environ, {"TRADE_BANK_NIFTY": "false"}, clear=False),
            patch.object(trade_bot, "market_window_ok", return_value=True),
            patch.object(trade_bot, "read_state", return_value={}),
            patch.object(
                trade_bot,
                "portfolio_day_circuit",
                return_value={"allowed": True, "score_penalty": 0},
            ),
            patch.object(trade_bot, "get_open_positions", return_value=[]),
            patch.object(
                trade_bot,
                "evaluate_symbol_buy_or_sell",
                return_value=nifty_candidate,
            ) as evaluate,
            patch.object(trade_bot, "execute_selected_candidate") as execute,
        ):
            trade_bot.run_signal_check()

        evaluate.assert_called_once()
        self.assertEqual(evaluate.call_args.args[0], "NIFTY")
        execute.assert_called_once_with(nifty_candidate)

    def test_paper_observation_lane_routes_live_ineligible_score_to_free_slot(self):
        candidate = {
            "symbol": "NIFTY",
            "transaction_type": "BUY",
            "weighted": {"score": 35},
            "allowed": False,
            "paper_observation_eligible": True,
            "reason": "below live score range",
        }
        with (
            patch.dict(
                os.environ,
                {
                    "PAPER_OBSERVATION_MODE_ENABLED": "true",
                    "TRADE_BANK_NIFTY": "false",
                },
                clear=False,
            ),
            patch.object(trade_bot, "market_window_ok", return_value=True),
            patch.object(trade_bot, "paper_observation_entry_window_ok", return_value=True),
            patch.object(trade_bot, "read_state", return_value={}),
            patch.object(
                trade_bot,
                "portfolio_day_circuit",
                return_value={"allowed": True, "score_penalty": 0},
            ),
            patch.object(trade_bot, "get_open_positions", return_value=[]),
            patch.object(trade_bot, "daily_index_entry_block_reason", return_value=""),
            patch.object(
                trade_bot,
                "evaluate_symbol_buy_or_sell",
                return_value=candidate,
            ),
            patch.object(
                trade_bot,
                "available_paper_observation_slot",
                return_value="PAPER_NIFTY_01",
            ),
            patch.object(trade_bot, "execute_selected_candidate") as execute,
        ):
            trade_bot.run_signal_check()

        selected = execute.call_args.args[0]
        self.assertTrue(selected["paper_trade"])
        self.assertTrue(selected["paper_observation"])
        self.assertEqual(selected["state_slot"], "PAPER_NIFTY_01")
        self.assertEqual(candidate.get("paper_trade"), None)

    def test_paper_observation_slots_are_capped_and_reused_independently(self):
        def state_for(slot):
            if slot == "PAPER_NIFTY_01":
                return {"instrument_key": "NSE_FO|1", "status": "POSITION_OPEN"}
            return {}

        with (
            patch.dict(
                os.environ,
                {"MAX_SIMULTANEOUS_PAPER_OBSERVATIONS": "10"},
                clear=False,
            ),
            patch.object(trade_bot, "read_state", side_effect=state_for),
        ):
            slot = trade_bot.available_paper_observation_slot("NIFTY")

        self.assertEqual(slot, "PAPER_NIFTY_02")
        self.assertIsNone(trade_bot.available_paper_observation_slot("BANKNIFTY"))

    def test_paper_observation_is_one_lot_and_bypasses_live_portfolio_gate(self):
        candidate = {
            "symbol": "NIFTY",
            "state_slot": "PAPER_NIFTY_03",
            "paper_trade": True,
            "paper_observation": True,
            "direction": "BULLISH",
            "confidence": "HIGH",
            "signal_score": 35,
            "transaction_type": "BUY",
            "instrument": {
                "instrument_key": "NSE_FO|PAPER",
                "trading_symbol": "NIFTY26AUG25000CE",
                "lot_size": 65,
            },
            "entry_price": 100.0,
            "target_price": 115.0,
            "stop_loss_price": 85.0,
            "target_points": 30.0,
            "stop_points": 30.0,
            "option_delta_used": 0.5,
            "target_percent": None,
            "stop_percent": None,
            "entry_score": {"score": 35, "score_version": "TEST"},
            "weighted": {"score": 35},
            "technicals": {},
            "option_summary": {"option_type": "CE"},
        }
        post_fill = {
            "target_price": 115.0,
            "stop_loss_price": 85.0,
            "technicals": {},
            "feasibility": {},
        }
        saved_state = {"paper_trade": True}
        with (
            patch.object(trade_bot, "paper_after_first_outcome", return_value=False),
            patch.object(trade_bot, "order_quantity_for") as live_sizing,
            patch.object(trade_bot, "planned_trade_context", return_value={}),
            patch.object(trade_bot, "write_stream_instruments"),
            patch.object(trade_bot, "pre_order_portfolio_decision") as live_gate,
            patch.object(
                trade_bot,
                "revalidate_option_after_fill",
                return_value=post_fill,
            ),
            patch.object(
                trade_bot,
                "make_post_fill_diagnostic_only",
                return_value=post_fill,
            ),
            patch.object(trade_bot, "save_open_position_state") as save,
            patch.object(trade_bot, "read_state", return_value=saved_state),
            patch.object(trade_bot, "write_state") as write,
        ):
            opened = trade_bot._execute_selected_candidate_locked(candidate)

        self.assertTrue(opened)
        live_sizing.assert_not_called()
        live_gate.assert_not_called()
        self.assertEqual(save.call_args.args[0], "PAPER_NIFTY_03")
        self.assertEqual(save.call_args.args[7], 65)
        self.assertEqual(saved_state["symbol"], "NIFTY")
        self.assertEqual(saved_state["state_slot"], "PAPER_NIFTY_03")
        write.assert_called_once_with("PAPER_NIFTY_03", saved_state)

    def test_trade_bank_nifty_false_applies_to_ganesh_scans(self):
        with (
            patch.dict(os.environ, {"TRADE_BANK_NIFTY": "false"}, clear=False),
            patch.object(
                trade_bot,
                "now_ist",
                return_value=datetime(2026, 8, 7, 10, 0),
            ),
            patch.object(trade_bot, "active_bot_states", return_value=[]),
            patch.object(trade_bot, "write_stream_instruments"),
            patch.object(trade_bot, "run_ganesh_gap_symbol_signal_check") as scan,
            patch.object(trade_bot, "read_state", return_value={}),
        ):
            trade_bot.run_ganesh_gap_signal_check()

        scan.assert_called_once()
        self.assertEqual(scan.call_args.args[0], "NIFTY")

    def test_index_point_exits_are_read_from_environment(self):
        with patch.dict(
            os.environ,
            {
                "NIFTY_TARGET_POINTS": "30",
                "NIFTY_STOP_POINTS": "30",
                "OPTION_DELTA_APPROXIMATION": "0.5",
            },
            clear=False,
        ):
            levels = trade_bot.option_levels_from_index_points("NIFTY", 150)

        self.assertEqual(levels["target_price"], 165)
        self.assertEqual(levels["stop_loss_price"], 135)
        self.assertEqual(levels["target_points"], 30)
        self.assertEqual(levels["stop_points"], 30)

    def test_adaptive_exit_history_cannot_override_static_execution_levels(self):
        rule = {
            "status": "ADAPTIVE",
            "mode": "MIN",
            "min_score": 55,
            "max_score": None,
            "exit_levels": {
                "source": "ADAPTIVE_HISTORY_AVERAGE",
                "target_points": 24.5,
                "stop_points": 18.0,
            },
        }
        with (
            patch.dict(
                os.environ,
                {
                    "VAMSI_ADAPTIVE_SCORE_ENABLED": "true",
                    "NIFTY_TARGET_POINTS": "30",
                    "NIFTY_STOP_POINTS": "30",
                    "EXTREME_SETUP_MIN_SCORE": "101",
                },
                clear=False,
            ),
            patch.object(trade_bot, "read_effective_score_rule", return_value=rule),
        ):
            settings = trade_bot.score_based_exit_settings("NIFTY", 65)

        self.assertEqual(settings["target_points"], 30)
        self.assertEqual(settings["stop_points"], 30)
        self.assertEqual(settings["profile"], "STANDARD")
        self.assertEqual(settings["source"], "STATIC_ENV")

    def test_active_selective_position_blocks_new_index_scan(self):
        def state_for(symbol):
            if symbol == "NIFTY":
                return {
                    "instrument_key": "NSE_FO|NIFTY_OPEN",
                    "status": "POSITION_OPEN",
                }
            return {}

        with (
            patch.object(trade_bot, "market_window_ok", return_value=True),
            patch.object(trade_bot, "read_state", side_effect=state_for),
            patch.object(trade_bot, "handle_existing_state"),
            patch.object(
                trade_bot,
                "portfolio_day_circuit",
                return_value={"allowed": True, "score_penalty": 0},
            ),
            patch.object(
                trade_bot,
                "evaluate_symbol_buy_or_sell",
            ) as evaluate,
            patch.object(trade_bot, "execute_selected_candidate") as execute,
        ):
            trade_bot.run_signal_check()

        evaluate.assert_not_called()
        execute.assert_not_called()

    def test_simultaneous_mode_scans_other_index_while_one_is_active(self):
        def state_for(symbol):
            if symbol == "NIFTY":
                return {
                    "instrument_key": "NSE_FO|NIFTY_OPEN",
                    "status": "POSITION_OPEN",
                }
            return {}

        bank_candidate = {
            "symbol": "BANKNIFTY",
            "transaction_type": "BUY",
            "weighted_score": 55,
        }
        with (
            patch.dict(
                os.environ,
                {"ALLOW_SIMULTANEOUS_INDEX_POSITIONS": "true"},
                clear=False,
            ),
            patch.object(trade_bot, "market_window_ok", return_value=True),
            patch.object(trade_bot, "trading_engine", return_value="VAMSI"),
            patch.object(trade_bot, "read_state", side_effect=state_for),
            patch.object(trade_bot, "handle_existing_state"),
            patch.object(
                trade_bot,
                "portfolio_day_circuit",
                return_value={"allowed": True, "score_penalty": 0},
            ),
            patch.object(trade_bot, "get_open_positions", return_value=[]),
            patch.object(trade_bot, "daily_index_entry_block_reason", return_value=""),
            patch.object(trade_bot, "active_index_instrument_keys", return_value=set()),
            patch.object(trade_bot, "read_watch_state", return_value={}),
            patch.object(trade_bot, "watch_mode_enabled", return_value=False),
            patch.object(
                trade_bot,
                "evaluate_symbol_buy_or_sell",
                return_value=bank_candidate,
            ) as evaluate,
            patch.object(trade_bot, "execute_selected_candidate", return_value=True) as execute,
        ):
            trade_bot.run_signal_check()

        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(evaluate.call_args.args[0], "BANKNIFTY")
        execute.assert_called_once_with(bank_candidate)

    def test_simultaneous_mode_can_execute_both_indices_from_same_scan(self):
        candidates = {
            "NIFTY": {
                "symbol": "NIFTY",
                "transaction_type": "BUY",
                "weighted_score": 50,
            },
            "BANKNIFTY": {
                "symbol": "BANKNIFTY",
                "transaction_type": "BUY",
                "weighted_score": 60,
            },
        }

        with (
            patch.dict(
                os.environ,
                {"ALLOW_SIMULTANEOUS_INDEX_POSITIONS": "true"},
                clear=False,
            ),
            patch.object(trade_bot, "market_window_ok", return_value=True),
            patch.object(trade_bot, "trading_engine", return_value="VAMSI"),
            patch.object(trade_bot, "read_state", return_value={}),
            patch.object(
                trade_bot,
                "portfolio_day_circuit",
                return_value={"allowed": True, "score_penalty": 0},
            ),
            patch.object(trade_bot, "get_open_positions", return_value=[]),
            patch.object(trade_bot, "daily_index_entry_block_reason", return_value=""),
            patch.object(trade_bot, "active_index_instrument_keys", return_value=set()),
            patch.object(trade_bot, "read_watch_state", return_value={}),
            patch.object(trade_bot, "watch_mode_enabled", return_value=False),
            patch.object(
                trade_bot,
                "evaluate_symbol_buy_or_sell",
                side_effect=lambda symbol, **kwargs: candidates[symbol],
            ),
            patch.object(trade_bot, "execute_selected_candidate", return_value=True) as execute,
        ):
            trade_bot.run_signal_check()

        self.assertEqual(execute.call_count, 2)
        self.assertEqual(
            {call.args[0]["symbol"] for call in execute.call_args_list},
            {"NIFTY", "BANKNIFTY"},
        )

    def test_stream_subscription_keeps_all_active_bot_contracts(self):
        states = {
            "NIFTY": {
                "instrument_key": "NSE_FO|NIFTY_OPEN",
                "status": "POSITION_OPEN",
            },
            "BANKNIFTY": {},
        }
        with patch.object(
            trade_bot,
            "BOT_STATE_SLOTS",
            tuple(states),
        ), patch.object(
            trade_bot,
            "read_state",
            side_effect=lambda symbol: states[symbol],
        ), patch.object(trade_bot, "_write_stream_instruments") as write:
            trade_bot.write_stream_instruments(
                ["NSE_INDEX|Nifty 50", "NSE_FO|BANKNIFTY_CANDIDATE"]
            )

        subscribed = write.call_args.args[0]
        self.assertIn("NSE_FO|NIFTY_OPEN", subscribed)
        self.assertIn("NSE_FO|BANKNIFTY_CANDIDATE", subscribed)

    def test_losing_setups_are_rejected_by_feasibility_gate(self):
        first = trade_bot.evaluate_trade_feasibility(
            "BULLISH",
            159,
            175,
            147,
            {
                "atm_option_flow": {"close": 161.25},
                "five_min": {"bias": "BULLISH", "option_target_price": 190},
                "fifteen_min": {"bias": "BULLISH", "option_target_price": 161},
            },
        )
        second = trade_bot.evaluate_trade_feasibility(
            "BULLISH",
            156,
            172,
            144,
            {
                "atm_option_flow": {"close": 152.5},
                "five_min": {"bias": "BULLISH", "option_target_price": 157},
                "fifteen_min": {"bias": "BULLISH", "option_target_price": 159},
            },
        )

        self.assertFalse(first["allowed"])
        self.assertEqual(first["technical_reward_risk"], 0.17)
        self.assertFalse(second["allowed"])
        self.assertIn("unfavorably extended by 2.30%", second["reasons"][0])

    def test_healthy_setup_passes_and_target_is_capped(self):
        result = trade_bot.evaluate_trade_feasibility(
            "BULLISH",
            100,
            112,
            92,
            {
                "atm_option_flow": {"close": 99.5},
                "five_min": {"bias": "BULLISH", "option_target_price": 111},
                "fifteen_min": {"bias": "BULLISH", "option_target_price": 110},
            },
        )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["adjusted_target_price"], 110)
        self.assertEqual(result["technical_reward_risk"], 1.25)

    def test_low_reward_risk_is_rejected_below_configured_gate(self):
        result = trade_bot.evaluate_trade_feasibility(
            "BULLISH",
            100,
            115,
            85,
            {
                "atm_option_flow": {"close": 100},
                "five_min": {"bias": "BULLISH", "option_target_price": 104},
                "fifteen_min": {"bias": "BULLISH", "option_target_price": 106},
            },
            symbol="NIFTY",
        )

        self.assertFalse(result["allowed"])
        self.assertEqual(result["technical_reward_risk"], 0.4)
        self.assertIn("below required 0.80", result["reasons"][0])

    def test_five_minute_target_does_not_limit_fifteen_minute_reward_risk(self):
        result = trade_bot.evaluate_trade_feasibility(
            "BULLISH",
            100,
            112,
            92,
            {
                "atm_option_flow": {"close": 100},
                "five_min": {"bias": "BULLISH", "option_target_price": 101},
                "fifteen_min": {"bias": "BULLISH", "option_target_price": 110},
            },
            symbol="NIFTY",
        )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["limiting_timeframe"], "15M")
        self.assertEqual(result["technical_reward_risk"], 1.25)
        self.assertEqual(
            result["technical_target_candidates"],
            [{"timeframe": "15M", "target_price": 110.0}],
        )

    def test_five_minute_target_cannot_replace_missing_aligned_fifteen_minute_target(self):
        result = trade_bot.evaluate_trade_feasibility(
            "BULLISH",
            100,
            112,
            92,
            {
                "atm_option_flow": {"close": 100},
                "five_min": {"bias": "BULLISH", "option_target_price": 110},
                "fifteen_min": {"bias": "NEUTRAL", "option_target_price": None},
            },
            symbol="NIFTY",
        )

        self.assertFalse(result["allowed"])
        self.assertEqual(
            result["reasons"],
            ["No aligned 15M option-premium target is available"],
        )

    def test_score_58_does_not_bypass_reward_risk_rejection(self):
        with patch.dict(
            os.environ,
            {"VAMSI_UNIFIED_SCORE_FALLBACK": "55"},
            clear=False,
        ):
            self.assertTrue(trade_bot.vamsi_weighted_score_qualifies(58, "NIFTY"))
            result = trade_bot.evaluate_trade_feasibility(
                "BULLISH",
                100,
                115,
                85,
                {
                    "atm_option_flow": {"close": 100},
                    "five_min": {"bias": "BULLISH", "option_target_price": 104},
                    "fifteen_min": {"bias": "BULLISH", "option_target_price": 106},
                },
                symbol="NIFTY",
            )

        self.assertFalse(result["allowed"])
        self.assertIn("below required 0.80", result["reasons"][0])

    def test_fifteen_minute_reward_risk_of_point_eight_is_accepted(self):
        result = trade_bot.evaluate_trade_feasibility(
            "BULLISH",
            100,
            112,
            90,
            {
                "atm_option_flow": {"close": 100},
                "five_min": {"bias": "BULLISH", "option_target_price": 101},
                "fifteen_min": {"bias": "BULLISH", "option_target_price": 108},
            },
            symbol="NIFTY",
        )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["limiting_timeframe"], "15M")
        self.assertEqual(result["technical_reward_risk"], 0.8)

    def test_fixed_index_gate_accepts_one_to_one_or_better(self):
        result = trade_bot.evaluate_trade_feasibility(
            "BULLISH",
            100,
            115,
            85,
            {
                "atm_option_flow": {"close": 100},
                "five_min": {"bias": "BULLISH", "option_target_price": 115},
                "fifteen_min": {"bias": "BULLISH", "option_target_price": 118},
            },
            symbol="BANKNIFTY",
        )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["technical_reward_risk"], 1.0)
        self.assertNotIn("exit_profile", result)

    def test_capital_allocation_rounds_down_to_whole_lots(self):
        with patch.dict(
            os.environ,
            {
                "OPTION_CAPITAL_PER_ENTRY": "350000",
                "MAX_LOTS_PER_ENTRY": "0",
            },
            clear=False,
        ), patch(
            "trade_bot.active_value",
            side_effect=lambda name, fallback: os.getenv(name, fallback),
        ):
            quantity = trade_bot.order_quantity_for(
                "NIFTY",
                {"lot_size": 65},
                entry_price=159,
            )

        self.assertEqual(quantity, 2145)

    def test_index_quantity_is_capped_by_risk_budget(self):
        with (
            patch.dict(
                os.environ,
                {
                    "OPTION_CAPITAL_PER_ENTRY": "350000",
                    "MAX_LOTS_PER_ENTRY": "0",
                    "INDEX_RISK_PER_TRADE": "5000",
                    "MAX_DAILY_INDEX_RISK": "10000",
                },
                clear=False,
            ),
            patch(
                "trade_bot.active_value",
                side_effect=lambda name, fallback: os.getenv(name, fallback),
            ),
            patch.object(trade_bot, "remaining_index_risk_budget", return_value=5000),
        ):
            quantity = trade_bot.order_quantity_for(
                "NIFTY",
                {"lot_size": 65},
                entry_price=200,
                stop_loss_price=170,
            )

        self.assertEqual(quantity, 130)

    def test_second_index_trade_requires_extra_score(self):
        chosen = {
            "symbol": "NIFTY",
            "direction": "BULLISH",
            "transaction_type": "BUY",
            "weighted": {"score": 82},
            "entry_minimum_score": 80,
        }
        with (
            patch.object(
                trade_bot,
                "portfolio_day_circuit",
                return_value={"allowed": True, "score_penalty": 0, "reason": "ok"},
            ),
            patch.object(trade_bot, "index_trade_count_today", return_value=1),
            patch.object(
                trade_bot,
                "last_index_trade_today",
                return_value={"symbol": "BANKNIFTY", "gross_pnl": "1500"},
            ),
            patch.object(trade_bot, "active_bot_states", return_value=[]),
            patch.object(
                trade_bot,
                "aggregate_risk_decision",
                return_value={"allowed": True, "reason": "ok"},
            ),
            patch.object(
                trade_bot,
                "correlation_decision",
                return_value={"allowed": True, "reason": "ok"},
            ),
            patch.dict(os.environ, {"SECOND_INDEX_TRADE_SCORE_BONUS": "5"}, clear=False),
        ):
            decision = trade_bot.pre_order_portfolio_decision(chosen, 65, 200, 170)

        self.assertFalse(decision["allowed"])
        self.assertIn("85.0", decision["reason"])

    def test_final_order_gate_enforces_combined_index_trade_cap(self):
        chosen = {
            "symbol": "BANKNIFTY",
            "direction": "BULLISH",
            "transaction_type": "BUY",
            "weighted_score": 55,
            "instrument": {"instrument_key": "NSE_FO|BANKNIFTY_NEW"},
        }
        with (
            patch.object(
                trade_bot,
                "monitor_health_gate",
                return_value={"allowed": True, "reason": "ok"},
            ),
            patch.object(
                trade_bot,
                "broker_pending_order_gate",
                return_value={"allowed": True, "reason": "ok"},
            ),
            patch.object(
                trade_bot,
                "portfolio_day_circuit",
                return_value={"allowed": True, "reason": "ok"},
            ),
            patch.object(trade_bot, "max_index_trades_per_day", return_value=2),
            patch.object(trade_bot, "index_trade_count_today", return_value=2),
        ):
            decision = trade_bot.pre_order_portfolio_decision(chosen, 30, 200, 180)

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["trades_used"], 2)
        self.assertIn("maximum 2 index trades", decision["reason"])

    def test_capital_value_one_means_one_lot(self):
        with patch.dict(
            os.environ,
            {"OPTION_CAPITAL_PER_ENTRY": "1", "MAX_LOTS_PER_ENTRY": "0"},
            clear=False,
        ), patch(
            "trade_bot.active_value",
            side_effect=lambda name, fallback: os.getenv(name, fallback),
        ):
            quantity = trade_bot.order_quantity_for(
                "BANKNIFTY",
                {"lot_size": 30},
                entry_price=900,
            )

        self.assertEqual(quantity, 30)

    def test_mobile_capital_is_applied_independently_to_both_indices(self):
        with (
            patch.dict(
                os.environ,
                {"OPTION_CAPITAL_PER_ENTRY": "1", "MAX_LOTS_PER_ENTRY": "0"},
                clear=False,
            ),
            patch("trade_bot.active_value", return_value=100000),
        ):
            nifty_quantity = trade_bot.order_quantity_for(
                "NIFTY", {"lot_size": 65}, entry_price=100
            )
            banknifty_quantity = trade_bot.order_quantity_for(
                "BANKNIFTY", {"lot_size": 30}, entry_price=500
            )

        self.assertEqual(nifty_quantity, 975)
        self.assertEqual(banknifty_quantity, 180)

    def test_short_levels_and_risk_are_side_aware(self):
        target, stop = trade_bot.option_levels_from_fill(
            100,
            10,
            7.5,
            transaction_type="SELL",
        )
        self.assertEqual((target, stop), (90, 108))

        with patch.dict(
            os.environ,
            {"OPTION_CAPITAL_PER_ENTRY": "350000", "MAX_LOTS_PER_ENTRY": "0"},
            clear=False,
        ):
            quantity = trade_bot.order_quantity_for(
                "BANKNIFTY",
                {"lot_size": 30},
                entry_price=100,
                stop_loss_price=108,
                transaction_type="SELL",
            )
        self.assertEqual(quantity, 0)

    def test_short_option_flow_is_normalized_for_position_scoring(self):
        normalized = trade_bot.normalize_option_flow_for_position(
            {
                "bias": "NEUTRAL",
                "close": 90,
                "vwap": 100,
                "vwap_slope": -0.2,
                "volume_confirmed": True,
            },
            "SELL",
        )
        self.assertEqual(normalized["bias"], "BULLISH")
        self.assertEqual(normalized["confidence"], "HIGH")
        self.assertEqual(normalized["raw_premium_bias"], "NEUTRAL")

    def test_sell_candidates_are_not_selected(self):
        buy = {"allowed": True, "transaction_type": "BUY", "weighted": {"score": 82}}
        sell = {"allowed": True, "transaction_type": "SELL", "weighted": {"score": 85}}
        self.assertIs(trade_bot.select_trade_candidate([buy, sell]), buy)
        sell["weighted"]["score"] = 120
        self.assertIs(trade_bot.select_trade_candidate([buy, sell]), buy)

    def test_profit_protection_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(trade_bot, "BASE_DIR", Path(temp_dir)),
                patch.dict(os.environ, {"PROFIT_PROTECTION_ENABLED": "false"}),
            ):
                state = {
                    "instrument_class": "INDEX_OPTION",
                    "entry_transaction_type": "BUY",
                    "entry_price": 100,
                    "target_price": 130,
                    "planned_target_price": 130,
                    "stop_loss_price": 90,
                    "highest_ltp": 100,
                    "quantity": 30,
                }
                updated = trade_bot.apply_trailing_stop("NIFTY", state, 121)
        self.assertEqual(updated["highest_ltp"], 121)
        self.assertEqual(updated["stop_loss_price"], 90)

    def test_trade_profile_can_disable_profit_protection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(trade_bot, "BASE_DIR", Path(temp_dir)),
                patch.dict(os.environ, {"PROFIT_PROTECTION_ENABLED": "true"}),
            ):
                state = {
                    "instrument_class": "INDEX_OPTION",
                    "entry_transaction_type": "BUY",
                    "entry_price": 100,
                    "target_price": 130,
                    "planned_target_price": 130,
                    "stop_loss_price": 90,
                    "highest_ltp": 100,
                    "profit_protection_stage": 0,
                    "profit_protection_enabled_for_trade": False,
                }
                updated = trade_bot.apply_trailing_stop("NIFTY", state, 121)
        self.assertEqual(updated["profit_protection_stage"], 0)
        self.assertEqual(updated["stop_loss_price"], 90)

    def test_stage_one_profit_protection_locks_twenty_percent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(trade_bot, "BASE_DIR", Path(temp_dir)),
                patch.dict(os.environ, {"PROFIT_PROTECTION_ENABLED": "true"}),
            ):
                state = {
                    "instrument_class": "INDEX_OPTION",
                    "entry_transaction_type": "BUY",
                    "entry_price": 100,
                    "target_price": 130,
                    "planned_target_price": 130,
                    "stop_loss_price": 90,
                    "highest_ltp": 100,
                    "profit_protection_stage": 0,
                }
                updated = trade_bot.apply_trailing_stop("NIFTY", state, 118)
        self.assertEqual(updated["profit_protection_stage"], 1)
        self.assertEqual(updated["stop_loss_price"], 106)
        self.assertEqual(updated["target_progress_percent"], 60)

    def test_profit_booking_price_is_eighty_percent_of_long_target(self):
        with patch.dict(os.environ, {"PROFIT_BOOKING_TARGET_PERCENT": "80"}):
            price = trade_bot.profit_booking_price(
                {
                    "entry_transaction_type": "BUY",
                    "entry_price": 100,
                    "target_price": 130,
                    "instrument_class": "INDEX_OPTION",
                }
            )
        self.assertEqual(price, 124)

    def test_profit_booking_price_supports_short_positions(self):
        with patch.dict(os.environ, {"PROFIT_BOOKING_TARGET_PERCENT": "80"}):
            price = trade_bot.profit_booking_price(
                {
                    "entry_transaction_type": "SELL",
                    "entry_price": 100,
                    "target_price": 70,
                    "instrument_class": "INDEX_OPTION",
                }
            )
        self.assertEqual(price, 76)

    def test_short_trade_journal_calculates_profit_when_premium_falls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history = Path(temp_dir) / "trade_history.csv"
            with (
                patch.object(trade_journal, "DATA_DIR", Path(temp_dir)),
                patch.object(trade_journal, "TRADE_HISTORY_FILE", history),
            ):
                row = trade_journal.record_closed_trade(
                    {
                        "symbol": "NIFTY",
                        "trading_symbol": "NIFTY TEST PE",
                        "direction": "BULLISH",
                        "entry_transaction_type": "SELL",
                        "quantity": 65,
                        "entry_price": 100,
                        "target_price": 90,
                        "stop_loss_price": 108,
                    },
                    exit_price=90,
                    exit_reason="TARGET",
                )
        self.assertEqual(row["gross_pnl"], 650)
        self.assertEqual(row["transaction_type"], "SELL")

    def test_trade_journal_records_index_sequence_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history = Path(temp_dir) / "trade_history.csv"
            with (
                patch.object(trade_journal, "DATA_DIR", Path(temp_dir)),
                patch.object(trade_journal, "TRADE_HISTORY_FILE", history),
            ):
                row = trade_journal.record_closed_trade(
                    {
                        "symbol": "NIFTY",
                        "trading_symbol": "NIFTY TEST PE",
                        "direction": "BEARISH",
                        "entry_transaction_type": "BUY",
                        "quantity": 65,
                        "entry_price": 100,
                        "target_price": 130,
                        "stop_loss_price": 70,
                        "trade_sequence": 2,
                        "prior_trade_symbol": "BANKNIFTY",
                        "prior_trade_outcome": "WIN",
                        "prior_trade_pnl": 1200,
                        "risk_per_trade_limit": 5000,
                        "remaining_index_risk_budget": 8000,
                        "planned_risk": 1950,
                    },
                    exit_price=110,
                    exit_reason="TARGET",
                )

        self.assertEqual(row["trade_sequence"], 2)
        self.assertEqual(row["prior_trade_symbol"], "BANKNIFTY")
        self.assertEqual(row["prior_trade_outcome"], "WIN")
        self.assertEqual(row["planned_risk"], 1950)

    def test_volume_ratio_is_graded(self):
        score = weighted_alignment_score(
            {"bias": "BULLISH", "confidence": "HIGH"},
            {
                "fifteen_min": {"bias": "BULLISH", "confidence": "HIGH"},
                "two_hour": {"bias": "BULLISH", "confidence": "HIGH"},
                "five_min": {"momentum_score": 4},
                "atm_option_flow": {
                    "bias": "BULLISH",
                    "volume_ratio": 1.09,
                    "volume_confirmed": True,
                },
                "institutional_flow": {"bias": "NEUTRAL", "confidence": "LOW"},
            },
            {"bias": "NEUTRAL"},
        )

        volume_reason = next(
            reason for reason in score["reasons"] if reason.startswith("Volume=")
        )
        self.assertIn("5.0/10", volume_reason)

    def test_losing_exit_requires_signal_reset_before_reentry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(trade_bot, "BASE_DIR", Path(temp_dir)):
                with patch.dict(
                    os.environ,
                    {"MIN_REENTRY_MINUTES": "0", "LOSS_REENTRY_MODE": "reset"},
                    clear=False,
                ):
                    state = {
                        "direction": "BULLISH",
                        "trading_symbol": "NIFTY TEST CE",
                    }
                    journal = {"gross_pnl": -1000}
                    trade_bot.register_losing_exit_guard(
                        "NIFTY",
                        state,
                        journal,
                        "STOP_LOSS",
                    )

                    self.assertIn(
                        "same-direction re-entry blocked",
                        trade_bot.reentry_block_reason("NIFTY", "BULLISH"),
                    )
                    trade_bot.observe_signal_reset("NIFTY", "NEUTRAL")
                    self.assertEqual(
                        trade_bot.reentry_block_reason("NIFTY", "BULLISH"),
                        "",
                    )

    def test_losing_exit_allows_next_signal_when_cooldown_is_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(trade_bot, "BASE_DIR", Path(temp_dir)):
                with patch.dict(
                    os.environ,
                    {"MIN_REENTRY_MINUTES": "0", "LOSS_REENTRY_MODE": "cooldown"},
                    clear=False,
                ):
                    trade_bot.register_losing_exit_guard(
                        "NIFTY",
                        {"direction": "BEARISH", "trading_symbol": "NIFTY TEST PE"},
                        {"gross_pnl": -500},
                        "STOP_LOSS",
                    )
                    self.assertEqual(
                        trade_bot.reentry_block_reason("NIFTY", "BEARISH"),
                        "",
                    )

    def test_fill_recalculation_preserves_nearer_technical_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(trade_bot, "BASE_DIR", Path(temp_dir)):
                with (
                    patch.dict(os.environ, {"NIFTY_LOTS": "1"}, clear=False),
                    patch.object(trade_bot, "send_apple_trade_entered_alert"),
                ):
                    trade_bot.save_open_position_state(
                        symbol="NIFTY",
                        order_id="test-order",
                        instrument={
                            "instrument_key": "NSE_FO|TEST",
                            "trading_symbol": "NIFTY TEST CE",
                            "lot_size": 65,
                        },
                        direction="BULLISH",
                        confidence="HIGH",
                        score=4,
                        entry_price=100,
                        quantity=65,
                        target_price=108,
                        stop_loss_price=92,
                        target_percent=10,
                        stop_percent=7.5,
                    )

                state = trade_bot.read_state("NIFTY")
                self.assertEqual(state["target_price"], 108)
                self.assertEqual(state["stop_loss_price"], 92)

    def test_replay_report_writes_category_daily_cumulative_results(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = build_reports(
                [
                    {
                        "trade_date": "2026-07-01",
                        "category": "NIFTY_OPTION_BUY",
                        "gross_pnl": 1000,
                        "estimated_costs": 50,
                    },
                    {
                        "trade_date": "2026-07-02",
                        "category": "NIFTY_OPTION_BUY",
                        "gross_pnl": -300,
                        "estimated_costs": 50,
                    },
                ],
                [],
                temp_dir,
            )
            daily = pd.read_csv(Path(temp_dir) / "category_daily_summary.csv")

        rows = daily[daily["category"] == "NIFTY_OPTION_BUY"].sort_values("trade_date")
        self.assertEqual(summary["categories"][0]["net_pnl"], 600)
        self.assertEqual(rows.iloc[-1]["cumulative_net_pnl"], 600)

    def test_replay_can_compare_trailing_and_fixed_stops(self):
        signal_time = pd.Timestamp("2026-07-15 10:00:00", tz="Asia/Kolkata")
        day = signal_time.date()
        candles = pd.DataFrame(
            [
                {"open": 100, "high": 106, "low": 99, "close": 105},
                {"open": 105, "high": 106, "low": 102, "close": 102},
                {"open": 102, "high": 103, "low": 89, "close": 90},
            ],
            index=[
                signal_time + pd.Timedelta(minutes=5),
                signal_time + pd.Timedelta(minutes=10),
                signal_time + pd.Timedelta(minutes=15),
            ],
        )
        candidate = Candidate(
            category="NIFTY_OPTION_BUY",
            symbol="NIFTY",
            direction="BULLISH",
            transaction_type="BUY",
            contract={"lot_size": 1, "trading_symbol": "TEST"},
            signal_time=signal_time,
            expected_entry=100,
            target=110,
            stop=90,
            score=80,
            grade="TRADE",
            reason="test",
        )

        outcomes = {}
        for enabled in (True, False):
            with patch.dict(
                os.environ,
                {"REPLAY_ENABLE_TRAILING_STOP": str(enabled).lower()},
            ):
                engine = StrategyReplay(
                    object(), day, day, include_stock_futures=False,
                    slippage_bps=0, cost_per_order=0,
                )
            with patch.object(engine, "_candles", return_value=candles):
                outcomes[enabled] = engine._simulate(candidate, day)

        self.assertEqual(outcomes[True]["exit_price"], 102)
        self.assertEqual(outcomes[False]["exit_price"], 90)
        self.assertTrue(outcomes[True]["trailing_stop_enabled"])
        self.assertFalse(outcomes[False]["trailing_stop_enabled"])

    def test_runner_mode_locks_profit_at_eighty_percent_and_keeps_full_target(self):
        state = {
            "instrument_key": "NSE_FO|RUNNER",
            "instrument_class": "INDEX_OPTION",
            "entry_transaction_type": "BUY",
            "entry_price": 100,
            "planned_target_price": 130,
            "target_price": 130,
            "stop_loss_price": 85,
            "quantity": 65,
            "profit_protection_stage": 2,
            "highest_ltp": 120,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            with (
                patch.object(trade_bot, "state_file", return_value=state_path),
                patch.dict(
                    os.environ,
                    {
                        "PROFIT_BOOKING_MODE": "runner",
                        "PROFIT_BOOKING_TARGET_PERCENT": "80",
                        "PROFIT_RUNNER_LOCK_PERCENT": "55",
                    },
                    clear=False,
                ),
            ):
                updated = trade_bot.apply_trailing_stop("NIFTY", state, 124)
                booking = trade_bot.profit_booking_price(updated)

        self.assertEqual(updated["profit_protection_stage"], 3)
        self.assertEqual(updated["stop_loss_price"], 116.5)
        self.assertEqual(booking, 130)

    def test_nifty_neutral_chain_override_requires_strong_aligned_momentum(self):
        direction, blockers = nifty_neutral_chain_direction(
            {
                "five_min": {
                    "bias": "BEARISH",
                    "confidence": "HIGH",
                    "momentum_score": -4,
                },
                "fifteen_min": {"bias": "BEARISH", "confidence": "MEDIUM"},
                "two_hour": {"bias": "NEUTRAL", "confidence": "LOW"},
                "nifty_breadth": {"bias": "BEARISH", "confidence": "MEDIUM"},
            }
        )
        self.assertEqual(direction, "BEARISH")
        self.assertEqual(blockers, [])

    def test_score_model_uses_requested_100_point_weights(self):
        result = weighted_alignment_score(
            {
                "bias": "BULLISH",
                "chain_bias": "BULLISH",
                "chain_confidence": "HIGH",
            },
            {
                "two_hour": {"bias": "BULLISH", "confidence": "HIGH"},
                "five_min": {
                    "bias": "BULLISH",
                    "confidence": "HIGH",
                    "vwap_bias": "BULLISH",
                },
                "fifteen_min": {
                    "bias": "BULLISH",
                    "confidence": "HIGH",
                    "open": 100,
                    "high": 111,
                    "low": 99,
                    "close": 110,
                },
                "atm_option_flow": {
                    "bias": "BULLISH",
                    "volume_ratio": 1.5,
                },
                "market_regime": {
                    "regime": "TREND",
                    "direction": "BULLISH",
                },
            },
            {"bias": "BULLISH"},
        )

        self.assertEqual(sum(result["weights"].values()), 100)
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["grade"], "TRADE")

    def test_standard_and_extreme_exit_points(self):
        with patch.dict(os.environ, {}, clear=True):
            standard_nifty = trade_bot.score_based_exit_settings("NIFTY", 89.9)
            extreme_nifty = trade_bot.score_based_exit_settings("NIFTY", 90)
            standard_bank = trade_bot.score_based_exit_settings("BANKNIFTY", 75)
            extreme_bank = trade_bot.score_based_exit_settings("BANKNIFTY", 95)

        self.assertEqual(
            (standard_nifty["target_points"], standard_nifty["stop_points"]),
            (20, 20),
        )
        self.assertEqual(
            (extreme_nifty["target_points"], extreme_nifty["stop_points"]),
            (30, 20),
        )
        self.assertEqual(
            (standard_bank["target_points"], standard_bank["stop_points"]),
            (40, 40),
        )
        self.assertEqual(
            (extreme_bank["target_points"], extreme_bank["stop_points"]),
            (60, 40),
        )

    def test_max_capital_uses_available_funds_and_rounds_down(self):
        trade_bot.BROKER_READ_CACHE.clear()
        with (
            patch("trade_bot.active_value", return_value=-1.0),
            patch.object(trade_bot, "available_equity_margin", return_value=100000),
            patch.dict(
                os.environ,
                {
                    "MAX_CAPITAL_USE_PERCENT": "95",
                    "MAX_CAPITAL_RESERVE_RUPEES": "1000",
                    "MAX_LOTS_PER_ENTRY": "0",
                },
                clear=False,
            ),
        ):
            quantity = trade_bot.order_quantity_for(
                "NIFTY", {"lot_size": 65}, entry_price=100
            )

        self.assertEqual(quantity, 910)

    def test_vamsi_score_approval_is_not_raised_by_trade_sequence(self):
        chosen = {
            "symbol": "NIFTY",
            "direction": "BULLISH",
            "transaction_type": "BUY",
            "weighted": {"score": 21},
            "entry_minimum_score": 20,
            "score_cutoff_approved": True,
        }
        with (
            patch.object(
                trade_bot,
                "portfolio_day_circuit",
                return_value={"allowed": True, "score_penalty": 10, "reason": "ok"},
            ),
            patch.object(trade_bot, "index_trade_count_today", return_value=1),
            patch.object(
                trade_bot,
                "last_index_trade_today",
                return_value={"symbol": "BANKNIFTY", "gross_pnl": "-1000"},
            ),
            patch.object(trade_bot, "remaining_index_risk_budget", return_value=0),
            patch.object(trade_bot, "active_bot_states", return_value=[]),
            patch.object(
                trade_bot,
                "aggregate_risk_decision",
                return_value={"allowed": True, "reason": "ok"},
            ),
            patch.object(
                trade_bot,
                "correlation_decision",
                return_value={"allowed": True, "reason": "ok"},
            ),
        ):
            decision = trade_bot.pre_order_portfolio_decision(chosen, 65, 200, 170)

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["required_score"], 20)

    def test_post_fill_reward_risk_rejection_is_diagnostic_only(self):
        post_fill = {
            "allowed": False,
            "feasibility": {
                "allowed": False,
                "reasons": [
                    "Technical reward/risk 0.07 is below required 1.00; 15M target=162.00"
                ],
            },
        }
        result = trade_bot.make_post_fill_diagnostic_only(post_fill)
        self.assertTrue(result["allowed"])
        self.assertTrue(result["feasibility"]["post_fill_diagnostic_only"])
        self.assertTrue(result["feasibility"]["post_fill_diagnostic_rejected"])
        self.assertFalse(result["feasibility"]["hard_execution_gate"])

    def test_post_fill_extension_rejection_is_diagnostic_only(self):
        post_fill = {
            "allowed": False,
            "feasibility": {
                "allowed": False,
                "reasons": [
                    "Expected entry is unfavorably extended by 2.00% versus the completed ATM option candle"
                ],
            },
        }
        result = trade_bot.make_post_fill_diagnostic_only(post_fill)
        self.assertTrue(result["allowed"])
        self.assertTrue(result["feasibility"]["post_fill_diagnostic_rejected"])
        self.assertFalse(result["feasibility"]["hard_execution_gate"])

    def test_live_entry_rejects_monitor_running_different_code(self):
        health = {
            "healthy": True,
            "updated_epoch": trade_bot.time_module.time(),
            "runtime_version": "older-runtime",
        }
        with (
            patch.dict(
                os.environ,
                {
                    "ENABLE_LIVE_TRADING": "true",
                    "REQUIRE_HEALTHY_POSITION_MONITOR": "true",
                },
                clear=False,
            ),
            patch.object(trade_bot, "read_json", return_value=health),
        ):
            result = trade_bot.monitor_health_gate()

        self.assertFalse(result["allowed"])
        self.assertIn("restart the monitor", result["reason"])

    def test_existing_state_refreshes_inside_finalization_lock(self):
        fresh = {"instrument_key": "NSE_FO|123", "status": "POSITION_OPEN"}
        with (
            patch.object(trade_bot, "read_state", return_value=fresh),
            patch.object(
                trade_bot,
                "position_finalization_lock",
                return_value=nullcontext(),
            ) as lock,
            patch.object(
                trade_bot,
                "_handle_existing_state_locked",
                return_value=True,
            ) as locked_handler,
        ):
            result = trade_bot.handle_existing_state(
                "NIFTY",
                {"instrument_key": "stale"},
                verbose=False,
            )

        self.assertTrue(result)
        lock.assert_called_once_with("NIFTY")
        locked_handler.assert_called_once_with("NIFTY", fresh, verbose=False)

    def test_selective_entry_uses_same_finalization_lock_as_monitor(self):
        chosen = {"symbol": "NIFTY"}
        with (
            patch.object(
                trade_bot,
                "position_finalization_lock",
                return_value=nullcontext(),
            ) as lock,
            patch.object(
                trade_bot,
                "_execute_selected_candidate_locked",
                return_value=True,
            ) as locked_execution,
        ):
            result = trade_bot.execute_selected_candidate(chosen)

        self.assertTrue(result)
        lock.assert_called_once_with("NIFTY")
        locked_execution.assert_called_once_with(chosen)

    def test_protection_failure_does_not_claim_second_pending_exit(self):
        state = {"status": "EXIT_PENDING", "entry_order_id": "entry-1"}
        with (
            patch.object(trade_bot, "read_state", return_value=state),
            patch.object(trade_bot, "find_matching_position_for_side") as position,
            patch.object(trade_bot, "write_state") as write,
        ):
            result = trade_bot.prepare_protection_failure_exit(
                "NIFTY",
                "NSE_FO|123",
                "BUY",
                520,
            )

        self.assertFalse(result["allowed"])
        self.assertEqual(result["quantity"], 0)
        position.assert_not_called()
        write.assert_not_called()

    def test_protection_failure_uses_only_remaining_broker_quantity(self):
        state = {"status": "POSITION_OPEN", "entry_order_id": "entry-1"}
        position = {"instrument_token": "NSE_FO|123", "quantity": 300}
        with (
            patch.object(trade_bot, "read_state", return_value=state),
            patch.object(
                trade_bot,
                "find_matching_position_for_side",
                return_value=position,
            ) as find_position,
            patch.object(trade_bot, "write_state") as write,
        ):
            result = trade_bot.prepare_protection_failure_exit(
                "NIFTY",
                "NSE_FO|123",
                "BUY",
                520,
            )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["quantity"], 300)
        self.assertEqual(state["status"], "EXIT_PENDING")
        self.assertTrue(state["exit_submission_in_progress"])
        find_position.assert_called_once_with("NSE_FO|123", "BUY", force=True)
        write.assert_called_once_with("NIFTY", state)

    def test_thesis_reversal_evidence_requires_both_vwap_views(self):
        state = {
            "direction": "BULLISH",
            "underlying_structural_stop": 99,
        }
        technicals = {
            "five_min": {
                "bias": "BEARISH",
                "confidence": "MEDIUM",
                "close": 98,
                "vwap": 100,
                "vwap_bias": "BEARISH",
            },
            "fifteen_min": {
                "bias": "BEARISH",
                "confidence": "HIGH",
                "close": 98,
            },
        }
        chain = {"direction": "BEARISH", "confidence": "HIGH"}
        evidence = trade_bot.thesis_reversal_evidence(
            state,
            technicals,
            chain,
            {"bias": "NEUTRAL", "confidence": "LOW"},
        )
        self.assertFalse(evidence["components"]["vwap_confirmation"])

        confirmed = trade_bot.thesis_reversal_evidence(
            state,
            technicals,
            chain,
            {"bias": "BEARISH", "confidence": "HIGH"},
        )
        self.assertEqual(confirmed["adverse_count"], 4)
        self.assertTrue(confirmed["hard_reversal"])

    def test_three_reversal_components_must_persist_for_two_five_minute_scans(self):
        state = {
            "instrument_class": "INDEX_OPTION",
            "instrument_key": "NSE_FO|123",
            "trading_symbol": "NIFTY26AUG25000CE",
            "direction": "BULLISH",
            "entry_price": 100,
            "planned_target_price": 115,
            "underlying_structural_stop": 90,
        }
        technicals = {
            "five_min": {
                "bias": "BEARISH",
                "confidence": "MEDIUM",
                "close": 98,
                "vwap": 100,
                "vwap_bias": "BEARISH",
                "candle_time": "2026-08-11T10:00:00+05:30",
            },
            "fifteen_min": {
                "bias": "NEUTRAL",
                "confidence": "LOW",
                "close": 100,
                "candle_time": "2026-08-11T09:45:00+05:30",
            },
        }
        env = {
            "VAMSI_THESIS_REVERSAL_EXIT_ENABLED": "true",
            "VAMSI_THESIS_REVERSAL_MIN_COMPONENTS": "3",
            "VAMSI_THESIS_REVERSAL_CONFIRMATION_SCANS": "2",
        }
        with (
            patch.dict(os.environ, env),
            patch.object(trade_bot, "minutes_since_created", return_value=10),
            patch.object(trade_bot, "get_technical_analysis", return_value=technicals),
            patch.object(
                trade_bot,
                "option_chain_reversal_snapshot",
                return_value={
                    "direction": "BEARISH",
                    "confidence": "HIGH",
                    "score": -4,
                    "reasons": [],
                },
            ),
            patch.object(
                trade_bot,
                "get_option_volume_vwap_analysis",
                return_value={"bias": "BEARISH", "confidence": "HIGH"},
            ),
            patch.object(trade_bot, "write_state"),
            patch.object(trade_bot, "log"),
        ):
            state, first_exit, _ = trade_bot.evaluate_five_minute_thesis_reversal(
                "NIFTY",
                state,
                95,
                datetime.fromisoformat("2026-08-11T10:05:06+05:30"),
            )
            state, second_exit, _ = trade_bot.evaluate_five_minute_thesis_reversal(
                "NIFTY",
                state,
                95,
                datetime.fromisoformat("2026-08-11T10:10:06+05:30"),
            )

        self.assertFalse(first_exit)
        self.assertTrue(second_exit)
        self.assertEqual(state["thesis_reversal_confirmation_count"], 2)

    def test_composite_reversal_replaces_option_chain_only_sentiment_exit(self):
        state = {"instrument_class": "INDEX_OPTION"}
        with patch.dict(
            os.environ,
            {"VAMSI_THESIS_REVERSAL_EXIT_ENABLED": "true"},
        ):
            self.assertFalse(trade_bot.sentiment_exit_enabled_for_state(state))


if __name__ == "__main__":
    unittest.main()
