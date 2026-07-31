import unittest
from contextlib import nullcontext
from unittest.mock import patch

import trade_bot


class T20ModeTests(unittest.TestCase):
    def candidate(self, score=51.0):
        return {
            "symbol": "NIFTY",
            "direction": "BULLISH",
            "transaction_type": "BUY",
            "entry_price": 100.0,
            "instrument": {
                "instrument_key": "NSE_FO|T20",
                "trading_symbol": "NIFTY TEST CE",
                "lot_size": 65,
            },
            "option_summary": {"bias": "BULLISH", "entry_price": 100.0},
            "technicals": {
                "option_market_quality": {"entry_allowed": True},
            },
            "weighted": {"score": score},
        }

    def test_t20_score_must_be_strictly_above_cutoff(self):
        with patch.dict("os.environ", {"T20_MIN_SCORE": "50"}, clear=False):
            rejected, reason = trade_bot.prepare_t20_candidate(
                "NIFTY", self.candidate(50.0)
            )
            accepted, _ = trade_bot.prepare_t20_candidate(
                "NIFTY", self.candidate(50.1)
            )
        self.assertIsNone(rejected)
        self.assertIn("not above", reason)
        self.assertEqual(accepted["strategy"], "T20")

    def test_t20_keeps_option_market_quality_as_hard_gate(self):
        candidate = self.candidate(80.0)
        candidate["technicals"]["option_market_quality"]["entry_allowed"] = False
        accepted, reason = trade_bot.prepare_t20_candidate("NIFTY", candidate)
        self.assertIsNone(accepted)
        self.assertIn("market quality", reason)

    def test_t20_levels_use_fixed_premium_points(self):
        with patch.dict(
            "os.environ",
            {
                "T20_NIFTY_TARGET_PREMIUM_POINTS": "5",
                "T20_NIFTY_STOP_PREMIUM_POINTS": "5",
                "T20_BANKNIFTY_TARGET_PREMIUM_POINTS": "10",
                "T20_BANKNIFTY_STOP_PREMIUM_POINTS": "10",
            },
            clear=False,
        ):
            nifty = trade_bot.t20_premium_levels("NIFTY", 100.0, 65)
            banknifty = trade_bot.t20_premium_levels("BANKNIFTY", 100.0, 30)
        self.assertEqual(nifty["target_price"], 105.0)
        self.assertEqual(nifty["stop_loss_price"], 95.0)
        self.assertEqual(banknifty["target_price"], 110.0)
        self.assertEqual(banknifty["stop_loss_price"], 90.0)

    def test_t20_sentiment_exit_is_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(
                trade_bot.sentiment_exit_enabled_for_state({"strategy": "T20"})
            )
            self.assertTrue(
                trade_bot.sentiment_exit_enabled_for_state({"strategy": "SELECTIVE"})
            )

    def test_t20_sentiment_exit_can_be_explicitly_enabled(self):
        with patch.dict(
            "os.environ", {"T20_SENTIMENT_EXIT_ENABLED": "true"}, clear=False
        ):
            self.assertTrue(
                trade_bot.sentiment_exit_enabled_for_state({"strategy": "T20"})
            )

    def test_t20_trailing_stop_is_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(trade_bot.t20_trailing_stop_enabled())

    def test_t20_trailing_stop_can_be_explicitly_enabled(self):
        with patch.dict(
            "os.environ", {"T20_TRAILING_STOP_ENABLED": "true"}, clear=False
        ):
            self.assertTrue(trade_bot.t20_trailing_stop_enabled())

    def test_disabled_t20_trailing_does_not_move_stop(self):
        state = {
            "strategy": "T20",
            "instrument_class": "INDEX_OPTION",
            "entry_transaction_type": "BUY",
            "entry_price": 100.0,
            "planned_target_price": 105.0,
            "target_price": 105.0,
            "stop_loss_price": 95.0,
            "profit_protection_stage": 0,
            "profit_protection_enabled_for_trade": True,
        }
        with patch.dict(
            "os.environ",
            {
                "T20_TRAILING_STOP_ENABLED": "false",
                "PROFIT_PROTECTION_ENABLED": "true",
            },
            clear=False,
        ):
            updated = trade_bot.apply_trailing_stop("T20_NIFTY", state, 104.0)
        self.assertEqual(updated["profit_protection_stage"], 0)
        self.assertEqual(updated["stop_loss_price"], 95.0)

    def test_t20_books_at_eighty_percent_even_in_global_runner_mode(self):
        with patch.dict(
            "os.environ",
            {
                "T20_PROFIT_BOOKING_TARGET_PERCENT": "80",
                "PROFIT_BOOKING_MODE": "runner",
            },
            clear=False,
        ):
            nifty_price = trade_bot.profit_booking_price(
                {
                    "strategy": "T20",
                    "instrument_class": "INDEX_OPTION",
                    "entry_transaction_type": "BUY",
                    "entry_price": 100.0,
                    "planned_target_price": 105.0,
                    "target_price": 105.0,
                }
            )
            bank_price = trade_bot.profit_booking_price(
                {
                    "strategy": "T20",
                    "instrument_class": "INDEX_OPTION",
                    "entry_transaction_type": "BUY",
                    "entry_price": 100.0,
                    "planned_target_price": 110.0,
                    "target_price": 110.0,
                }
            )
        self.assertEqual(nifty_price, 104.0)
        self.assertEqual(bank_price, 108.0)

    def test_t20_quantity_uses_dedicated_cap_and_remaining_risk(self):
        instrument = {"lot_size": 65}
        with (
            patch.object(trade_bot, "effective_t20_capital", return_value=50000),
            patch.object(trade_bot, "order_quantity_for", return_value=2600) as sizing,
            patch.object(trade_bot, "today_t20_realized_pnl", return_value=0.0),
            patch.object(trade_bot, "active_t20_states", return_value=[]),
            patch.object(trade_bot, "total_open_risk", return_value=0.0),
            patch.dict("os.environ", {"T20_DAILY_MAX_LOSS": "10000"}, clear=False),
        ):
            quantity = trade_bot.t20_risk_adjusted_quantity(
                "NIFTY", instrument, 100.0, 90.0
            )
        sizing.assert_called_once_with(
            "NIFTY",
            instrument,
            100.0,
            90.0,
            transaction_type="BUY",
            capital_override=50000,
        )
        self.assertEqual(quantity, 975)
        self.assertLessEqual((100.0 - 90.0) * quantity, 10000.0)

    def test_live_t20_cap_shrinks_to_available_broker_funds(self):
        instrument = {"lot_size": 65}
        with (
            patch.dict(
                "os.environ",
                {
                    "ENABLE_LIVE_TRADING": "true",
                    "T20_OPTION_CAPITAL_PER_ENTRY": "50000",
                    "T20_DAILY_MAX_LOSS": "10000",
                },
                clear=False,
            ),
            patch.object(trade_bot, "maximum_available_option_capital", return_value=30000),
            patch.object(trade_bot, "index_risk_per_trade_limit", return_value=100000),
            patch.object(trade_bot, "remaining_index_risk_budget", return_value=100000),
            patch.object(trade_bot, "today_t20_realized_pnl", return_value=0.0),
            patch.object(trade_bot, "active_t20_states", return_value=[]),
            patch.object(trade_bot, "total_open_risk", return_value=0.0),
        ):
            quantity = trade_bot.t20_risk_adjusted_quantity(
                "NIFTY", instrument, 100.0, 95.0
            )
        self.assertEqual(quantity, 260)

    def test_one_lot_t20_is_rejected_when_broker_funds_are_insufficient(self):
        instrument = {"lot_size": 65}
        with (
            patch.dict(
                "os.environ",
                {
                    "ENABLE_LIVE_TRADING": "true",
                    "T20_OPTION_CAPITAL_PER_ENTRY": "1",
                },
                clear=False,
            ),
            patch.object(trade_bot, "maximum_available_option_capital", return_value=5000),
        ):
            capital = trade_bot.effective_t20_capital(instrument, 100.0)
        self.assertEqual(capital, 0.0)

    def test_exact_daily_loss_is_permitted_but_sixth_trade_is_not(self):
        candidate = self.candidate(60.0)
        common = [
            patch.object(trade_bot, "monitor_health_gate", return_value={"allowed": True}),
            patch.object(trade_bot, "broker_pending_order_gate", return_value={"allowed": True}),
            patch.object(trade_bot, "portfolio_day_circuit", return_value={"allowed": True}),
            patch.object(trade_bot, "index_underlying_has_active_state", return_value=False),
            patch.object(trade_bot, "today_t20_realized_pnl", return_value=-9000.0),
            patch.object(trade_bot, "active_t20_states", return_value=[]),
            patch.object(trade_bot, "active_bot_states", return_value=[]),
            patch.object(
                trade_bot,
                "aggregate_risk_decision",
                return_value={"allowed": True, "reason": "accepted"},
            ),
        ]
        with patch.dict(
            "os.environ",
            {
                "T20_MAX_TRADES_PER_DAY": "5",
                "T20_DAILY_MAX_LOSS": "10000",
                "MAX_OPEN_PORTFOLIO_RISK": "20000",
            },
            clear=False,
        ):
            for item in common:
                item.start()
            try:
                with patch.object(trade_bot, "t20_trade_count_today", return_value=4):
                    allowed = trade_bot.pre_order_t20_decision(
                        candidate, 100, 100.0, 90.0
                    )
                with patch.object(trade_bot, "t20_trade_count_today", return_value=5):
                    blocked = trade_bot.pre_order_t20_decision(
                        candidate, 100, 100.0, 90.0
                    )
            finally:
                for item in reversed(common):
                    item.stop()
        self.assertTrue(allowed["allowed"])
        self.assertFalse(blocked["allowed"])
        self.assertIn("trade cap", blocked["reason"])

    def test_t20_fill_is_finalized_and_protected_only_once(self):
        pending = {
            "entry_order_id": "ENTRY-1",
            "status": "BUY_PLACED_NOT_COMPLETE",
        }
        opened = {
            "entry_order_id": "ENTRY-1",
            "status": "POSITION_OPEN",
        }
        protected = {
            **opened,
            "protective_stop_order_id": "STOP-1",
        }
        current = pending

        def read_state(_symbol):
            return current

        def finalize(*_args, **_kwargs):
            nonlocal current
            current = opened
            return current

        def protect(_symbol, _state):
            nonlocal current
            current = protected
            return current

        instrument = {
            "instrument_key": "NSE_FO|T20",
            "trading_symbol": "NIFTY TEST CE",
        }
        with (
            patch.object(trade_bot, "position_finalization_lock", return_value=nullcontext()),
            patch.object(trade_bot, "read_state", side_effect=read_state),
            patch.object(trade_bot, "finalize_t20_open_position", side_effect=finalize) as finalize_mock,
            patch.object(trade_bot, "ensure_protective_stop", side_effect=protect) as protect_mock,
        ):
            first = trade_bot.finalize_and_protect_t20_position(
                "T20_NIFTY", pending, 100.0, 65, instrument, "ENTRY-1"
            )
            second = trade_bot.finalize_and_protect_t20_position(
                "T20_NIFTY", pending, 100.0, 65, instrument, "ENTRY-1"
            )

        self.assertEqual(first["protective_stop_order_id"], "STOP-1")
        self.assertEqual(second["protective_stop_order_id"], "STOP-1")
        finalize_mock.assert_called_once()
        protect_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
