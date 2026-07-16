import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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
import trade_journal
from counterfactual_replay import simulate_trade
from signal_score import weighted_alignment_score


class TradeControlTests(unittest.TestCase):
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

    def test_global_signal_check_executes_only_highest_scoring_index_candidate(self):
        candidates = {
            "NIFTY": {
                "symbol": "NIFTY",
                "transaction_type": "BUY",
                "weighted": {"score": 82},
            },
            "BANKNIFTY": {
                "symbol": "BANKNIFTY",
                "transaction_type": "SELL",
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
                side_effect=lambda symbol: candidates[symbol],
            ),
            patch.object(trade_bot, "execute_selected_candidate") as execute,
        ):
            trade_bot.run_signal_check()

        execute.assert_called_once_with(candidates["BANKNIFTY"])

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

    def test_risk_budget_caps_configured_lots(self):
        with patch.dict(
            os.environ,
            {
                "NIFTY_LOTS": "9",
                "MAX_LOTS_PER_ENTRY": "1",
                "MAX_RISK_PER_TRADE": "5000",
            },
            clear=False,
        ):
            quantity = trade_bot.order_quantity_for(
                "NIFTY",
                {"lot_size": 65},
                entry_price=159,
                stop_loss_price=147,
            )

        self.assertEqual(quantity, 65)

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
            {"BANKNIFTY_LOTS": "5", "MAX_LOTS_PER_ENTRY": "1", "MAX_RISK_PER_TRADE": "5000"},
            clear=False,
        ):
            quantity = trade_bot.order_quantity_for(
                "BANKNIFTY",
                {"lot_size": 30},
                entry_price=100,
                stop_loss_price=108,
                transaction_type="SELL",
            )
        self.assertEqual(quantity, 30)

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

    def test_buy_is_preferred_without_meaningful_short_score_advantage(self):
        buy = {"allowed": True, "transaction_type": "BUY", "weighted": {"score": 82}}
        sell = {"allowed": True, "transaction_type": "SELL", "weighted": {"score": 85}}
        with patch.dict(os.environ, {"SHORT_SCORE_ADVANTAGE": "5"}, clear=False):
            self.assertIs(trade_bot.select_trade_candidate([buy, sell]), buy)
            sell["weighted"]["score"] = 88
            self.assertIs(trade_bot.select_trade_candidate([buy, sell]), sell)

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


if __name__ == "__main__":
    unittest.main()
