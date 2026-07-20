import os
import unittest
from unittest.mock import patch

import pandas as pd

from stock_option_score import (
    stock_option_directional_score,
    stock_option_tradeability,
)
from stock_option_scanner import _select_tradeable_contract


class StockOptionScoreTests(unittest.TestCase):
    def strong_technicals(self):
        return {
            "five_min": {
                "bias": "BULLISH",
                "confidence": "HIGH",
                "momentum_score": 4,
                "vwap_bias": "BULLISH",
                "volume_ratio": 1.6,
            },
            "fifteen_min": {"bias": "BULLISH", "confidence": "HIGH"},
            "two_hour": {"bias": "BULLISH", "confidence": "MEDIUM"},
        }

    def test_neutral_chain_does_not_veto_strong_technical_setup(self):
        result = stock_option_directional_score(
            "BULLISH",
            self.strong_technicals(),
            {"bias": "NEUTRAL", "confidence": "LOW"},
            {
                "bias": "BULLISH",
                "confidence": "HIGH",
                "volume_confirmed": True,
            },
            {"bias": "BULLISH", "confidence": "HIGH"},
            {"directional_support": 1.0, "reliability": 1.0},
        )
        self.assertEqual(result["grade"], "TRADE")
        self.assertEqual(result["components"]["option_chain"], 0)
        self.assertGreaterEqual(result["score"], 70)

    def test_opposite_chain_is_a_penalty_not_an_automatic_veto(self):
        result = stock_option_directional_score(
            "BULLISH",
            self.strong_technicals(),
            {"bias": "BEARISH", "confidence": "HIGH"},
            {"bias": "BULLISH", "confidence": "HIGH", "volume_confirmed": True},
            {"bias": "BULLISH", "confidence": "HIGH"},
            {"directional_support": 1.0, "reliability": 1.0},
        )
        self.assertEqual(result["components"]["option_chain"], -10)

    def test_missing_evidence_is_not_renormalized(self):
        result = stock_option_directional_score(
            "BULLISH",
            {"five_min": {}, "fifteen_min": {}, "two_hour": {}},
            {},
            {},
        )
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["grade"], "SKIP")

    @patch.dict(os.environ, {}, clear=False)
    def test_tradeability_accepts_liquid_contract(self):
        result = stock_option_tradeability(
            "INFY",
            {
                "bid_price": 100,
                "ask_price": 101,
                "bid_qty": 600,
                "ask_qty": 600,
                "delta": 0.50,
            },
            chain_volume=5000,
            lot_size=300,
        )
        self.assertTrue(result["allowed"])
        self.assertGreaterEqual(result["score"], 75)

    def test_tradeability_rejects_wide_or_shallow_contract(self):
        result = stock_option_tradeability(
            "INFY",
            {
                "bid_price": 90,
                "ask_price": 110,
                "bid_qty": 100,
                "ask_qty": 100,
                "delta": 0.50,
            },
            chain_volume=100,
            lot_size=300,
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(any("spread" in reason for reason in result["blockers"]))
        self.assertTrue(any("depth" in reason for reason in result["blockers"]))

    @patch.dict(os.environ, {"STOCK_OPTION_FNO_BAN_SYMBOLS": "INFY"}, clear=False)
    def test_tradeability_respects_configured_ban_list(self):
        result = stock_option_tradeability(
            "INFY",
            {
                "bid_price": 100,
                "ask_price": 101,
                "bid_qty": 600,
                "ask_qty": 600,
                "delta": 0.50,
            },
            chain_volume=5000,
            lot_size=300,
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(any("block list" in reason for reason in result["blockers"]))

    def test_contract_selection_uses_liquid_one_step_itm_when_atm_is_wide(self):
        chain = pd.DataFrame(
            [
                {
                    "strike": 90,
                    "CE_instrument_key": "ITM",
                    "CE_ltp": 119.5,
                    "CE_bid_price": 119,
                    "CE_ask_price": 120,
                    "CE_bid_qty": 50,
                    "CE_ask_qty": 50,
                    "CE_volume": 500,
                    "CE_delta": 0.6,
                },
                {
                    "strike": 100,
                    "CE_instrument_key": "ATM",
                    "CE_ltp": 100,
                    "CE_bid_price": 90,
                    "CE_ask_price": 110,
                    "CE_bid_qty": 50,
                    "CE_ask_qty": 50,
                    "CE_volume": 500,
                    "CE_delta": 0.5,
                },
            ]
        )
        selected, reason = _select_tradeable_contract(
            "INFY",
            "BULLISH",
            chain.iloc[1],
            chain,
            {
                "ATM": {"instrument_key": "ATM", "lot_size": 10},
                "ITM": {"instrument_key": "ITM", "lot_size": 10},
            },
            lambda _key: {},
        )
        self.assertEqual(reason, "qualified")
        self.assertEqual(selected["contract_kind"], "ITM1")
        self.assertEqual(selected["option_key"], "ITM")


if __name__ == "__main__":
    unittest.main()
