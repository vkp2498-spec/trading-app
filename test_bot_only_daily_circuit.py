import csv
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import trade_bot


class BotOnlyDailyCircuitTests(unittest.TestCase):
    def test_bot_unrealized_pnl_excludes_open_manual_position(self):
        states = {
            "NIFTY": {
                "instrument_key": "BOT",
                "status": "POSITION_OPEN",
                "entry_price": 100,
                "entry_transaction_type": "BUY",
            },
            "BANKNIFTY": {
                "instrument_key": "MANUAL",
                "status": "POSITION_OPEN",
                "entry_price": 200,
                "entry_transaction_type": "BUY",
                "manual_override": True,
            },
        }
        positions = [
            {"instrument_token": "BOT", "quantity": 10, "last_price": 110},
            {"instrument_token": "MANUAL", "quantity": 5, "last_price": 250},
        ]
        with patch.object(trade_bot, "BOT_STATE_SLOTS", tuple(states)), patch.object(
            trade_bot, "read_state", side_effect=lambda symbol: states[symbol]
        ), patch.object(trade_bot, "get_open_positions", return_value=positions):
            self.assertEqual(trade_bot.bot_unrealized_pnl(), 100)

    def test_bot_realized_pnl_excludes_manual_and_broker_sync_rows(self):
        rows = [
            {
                "trade_date": "2026-08-06",
                "symbol": "NIFTY",
                "instrument_class": "INDEX_OPTION",
                "strategy": "SELECTIVE",
                "trading_symbol": "NIFTY BOT CE",
                "exit_reason": "TARGET",
                "gross_pnl": "500",
                "status": "CLOSED",
            },
            {
                "trade_date": "2026-08-06",
                "symbol": "NIFTY",
                "instrument_class": "INDEX_OPTION",
                "strategy": "MANUAL_INDEX",
                "trading_symbol": "NIFTY MANUAL PE",
                "exit_reason": "TARGET",
                "gross_pnl": "2000",
                "status": "CLOSED",
            },
            {
                "trade_date": "2026-08-06",
                "symbol": "BANKNIFTY",
                "instrument_class": "INDEX_OPTION",
                "strategy": "",
                "trading_symbol": "UPSTOX SYNC BANKNIFTY CALL",
                "exit_reason": "UPSTOX_SYNC_ADJUSTMENT",
                "gross_pnl": "-3000",
                "status": "CLOSED",
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            history = Path(temp_dir) / "trade_history.csv"
            with history.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with (
                patch.object(trade_bot, "TRADE_HISTORY_FILE", history),
                patch.object(trade_bot, "now_ist", return_value=datetime(2026, 8, 6, 12, 0)),
            ):
                self.assertEqual(trade_bot.today_realized_pnl(), 500)
                self.assertEqual(trade_bot.today_index_realized_pnl(), 500)
                self.assertEqual(len(trade_bot.today_closed_trade_rows()), 1)

    def test_account_wide_manual_pnl_is_diagnostic_only(self):
        for broker_pnl in (10000.0, -10000.0):
            with self.subTest(broker_pnl=broker_pnl), patch.dict(
                os.environ,
                {
                    "ENABLE_LIVE_TRADING": "true",
                    "BROKER_RECONCILIATION_REQUIRED": "true",
                    "DAILY_SOFT_LOSS": "0",
                    "PEAK_PROFIT_GIVEBACK_TRIGGER": "0",
                    "MAX_CONSECUTIVE_LOSSES": "0",
                },
                clear=False,
            ), (
                patch.object(trade_bot, "today_realized_pnl", return_value=100.0)
            ), (
                patch.object(trade_bot, "bot_unrealized_pnl", return_value=20.0)
            ), (
                patch.object(trade_bot, "broker_derivatives_day_pnl", return_value=broker_pnl)
            ), (
                patch.object(trade_bot, "daily_profit_target", return_value=1000.0)
            ), (
                patch.object(trade_bot, "daily_max_loss", return_value=1000.0)
            ), (
                patch.object(trade_bot, "consecutive_losses_today", return_value=0)
            ), (
                patch.object(trade_bot, "read_day_risk_state", return_value={"peak_pnl": 0.0})
            ), patch.object(trade_bot, "write_day_risk_state"):
                result = trade_bot.portfolio_day_circuit()

            self.assertTrue(result["allowed"])
            self.assertEqual(result["combined_pnl"], 120.0)
            self.assertEqual(result["broker_day_pnl"], broker_pnl)
            self.assertEqual(result["pnl_source"], "BOT_ONLY")

    def test_bot_pnl_still_triggers_daily_profit_target(self):
        with patch.dict(
            os.environ,
            {
                "ENABLE_LIVE_TRADING": "true",
                "BROKER_RECONCILIATION_REQUIRED": "true",
                "DAILY_SOFT_LOSS": "0",
                "PEAK_PROFIT_GIVEBACK_TRIGGER": "0",
                "MAX_CONSECUTIVE_LOSSES": "0",
            },
            clear=False,
        ), patch.object(trade_bot, "today_realized_pnl", return_value=900.0), patch.object(
            trade_bot, "bot_unrealized_pnl", return_value=200.0
        ), patch.object(
            trade_bot, "broker_derivatives_day_pnl", return_value=-5000.0
        ), patch.object(
            trade_bot, "daily_profit_target", return_value=1000.0
        ), patch.object(
            trade_bot, "daily_max_loss", return_value=1000.0
        ), patch.object(
            trade_bot, "consecutive_losses_today", return_value=0
        ), patch.object(
            trade_bot, "read_day_risk_state", return_value={"peak_pnl": 0.0}
        ), patch.object(trade_bot, "write_day_risk_state"):
            result = trade_bot.portfolio_day_circuit()

        self.assertFalse(result["allowed"])
        self.assertIn("daily profit target reached", result["reason"])
        self.assertEqual(result["combined_pnl"], 1100.0)


if __name__ == "__main__":
    unittest.main()
