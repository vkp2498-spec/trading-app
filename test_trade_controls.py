import os
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
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
import apns_push
import dashboard_data
import trade_journal
from counterfactual_replay import simulate_trade
from strategy_replay import Candidate, StrategyReplay, capital_sized_option_quantity
from signal_score import weighted_alignment_score
from backtest_report import build_reports


class TradeControlTests(unittest.TestCase):
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
                return [day]

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
                return [day]

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

    def test_signal_check_executes_each_qualified_index_candidate(self):
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
            patch.object(trade_bot, "get_open_positions", return_value=[]),
            patch.object(
                trade_bot,
                "evaluate_symbol_buy_or_sell",
                side_effect=lambda symbol, **kwargs: candidates[symbol],
            ),
            patch.object(trade_bot, "execute_selected_candidate") as execute,
        ):
            trade_bot.run_signal_check()

        self.assertEqual(execute.call_count, 2)
        execute.assert_has_calls([
            call(candidates["BANKNIFTY"]),
            call(candidates["NIFTY"]),
        ])

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

    def test_active_nifty_does_not_block_banknifty_entry(self):
        bank_candidate = {
            "symbol": "BANKNIFTY",
            "transaction_type": "BUY",
            "weighted": {"score": 82},
        }

        def state_for(symbol):
            if symbol == "NIFTY":
                return {"instrument_key": "NSE_FO|NIFTY_OPEN"}
            return {}

        with (
            patch.object(trade_bot, "market_window_ok", return_value=True),
            patch.object(trade_bot, "read_state", side_effect=state_for),
            patch.object(trade_bot, "handle_existing_state"),
            patch.object(trade_bot, "get_open_positions", return_value=[]),
            patch.object(
                trade_bot,
                "evaluate_symbol_buy_or_sell",
                return_value=bank_candidate,
            ) as evaluate,
            patch.object(trade_bot, "execute_selected_candidate") as execute,
        ):
            trade_bot.run_signal_check()

        evaluate.assert_called_once_with("BANKNIFTY", allow_option_sell=False)
        execute.assert_called_once_with(bank_candidate)

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

    def test_short_trailing_stop_moves_down_as_premium_falls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(trade_bot, "BASE_DIR", Path(temp_dir)):
                state = {
                    "entry_transaction_type": "SELL",
                    "entry_price": 100,
                    "target_price": 90,
                    "stop_loss_price": 108,
                    "lowest_ltp": 100,
                    "quantity": 30,
                }
                updated = trade_bot.apply_trailing_stop("BANKNIFTY", state, 94)
        self.assertEqual(updated["lowest_ltp"], 94)
        self.assertEqual(updated["stop_loss_price"], 97)

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

        flow_reason = next(reason for reason in score["reasons"] if reason.startswith("ATM"))
        self.assertIn("7.5/15", flow_reason)

    def test_losing_exit_requires_signal_reset_before_reentry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(trade_bot, "BASE_DIR", Path(temp_dir)):
                with patch.dict(os.environ, {"MIN_REENTRY_MINUTES": "0"}, clear=False):
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

    def test_fill_recalculation_preserves_nearer_technical_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(trade_bot, "BASE_DIR", Path(temp_dir)):
                with patch.dict(os.environ, {"NIFTY_LOTS": "1"}, clear=False):
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


if __name__ == "__main__":
    unittest.main()
