import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# The control tests do not call OpenAI. Stub the optional client so they also
# run on a lightweight development machine without production dependencies.
openai_stub = types.ModuleType("openai")
openai_stub.OpenAI = object
sys.modules.setdefault("openai", openai_stub)

import trade_bot
from signal_score import weighted_alignment_score


class TradeControlTests(unittest.TestCase):
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
        self.assertIn("2.30% above", second["reasons"][0])

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
            {"NIFTY_LOTS": "9", "MAX_RISK_PER_TRADE": "5000"},
            clear=False,
        ):
            quantity = trade_bot.order_quantity_for(
                "NIFTY",
                {"lot_size": 65},
                entry_price=159,
                stop_loss_price=147,
            )

        self.assertEqual(quantity, 390)

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
