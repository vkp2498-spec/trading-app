import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from zoneinfo import ZoneInfo

import vamsi_kb_intraday as kb


IST = ZoneInfo("Asia/Kolkata")


def qualified_candidate(direction="BULLISH", current=None):
    current = current or datetime(2026, 8, 14, 11, 6, tzinfo=IST)
    breadth_score = 45 if direction == "BULLISH" else -45
    option_type = "CE" if direction == "BULLISH" else "PE"
    return {
        "symbol": "NIFTY",
        "direction": direction,
        "entry_price": 150.0,
        "instrument": {
            "instrument_key": "NSE_FO|1",
            "trading_symbol": f"NIFTY ATM {option_type}",
            "lot_size": 65,
        },
        "option_summary": {
            "option_type": option_type,
            "chain_bias": direction,
            "chain_confidence": "HIGH",
            "option_market_quality": {
                "entry_allowed": True,
                "ltp": 149.5,
                "spread_percent": 0.8,
                "delta": 0.52 if direction == "BULLISH" else -0.52,
                "depth_bias": "NEUTRAL",
            },
        },
        "technicals": {
            "market_regime": {
                "regime": "TREND",
                "direction": direction,
            },
            "entry_structure": {
                "qualified": True,
                "type": "PULLBACK_HOLD",
                "signed_momentum": 4,
            },
            "bollinger_reversal": {"confirmed": False},
            "five_min": {
                "bias": direction,
                "confidence": "HIGH",
                "candle_time": (current - timedelta(minutes=6)).isoformat(),
            },
            "fifteen_min": {"bias": direction, "confidence": "HIGH"},
            "nifty_breadth": {
                "bias": direction,
                "confidence": "HIGH",
                "score": breadth_score,
                "coverage": 50,
            },
            "execution_atm_option_flow": {
                "bias": "BULLISH",
                "confidence": "HIGH",
                "close": 150,
                "vwap": 147,
                "volume_ratio": 1.5,
            },
            "trade_feasibility": {"entry_extension_percent": 0.2},
        },
    }


class VamsiKnowledgeEngineTests(unittest.TestCase):
    def test_all_independent_families_are_required(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        result = kb.evaluate_knowledge_setup(qualified_candidate(current=current), current)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["setup"], "PULLBACK_HOLD")

        candidate = qualified_candidate(current=current)
        candidate["technicals"]["nifty_breadth"]["bias"] = "BEARISH"
        rejected = kb.evaluate_knowledge_setup(candidate, current)
        self.assertFalse(rejected["allowed"])
        self.assertIn("breadth", " ".join(rejected["blockers"]).lower())

    def test_put_uses_oriented_negative_breadth_and_bought_put_flow(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        result = kb.evaluate_knowledge_setup(
            qualified_candidate("BEARISH", current), current
        )
        self.assertTrue(result["allowed"])

    def test_neutral_chain_is_rejected_by_default(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        candidate = qualified_candidate(current=current)
        candidate["option_summary"]["chain_bias"] = "NEUTRAL"
        with patch.dict(kb.os.environ, {"VAMSI_KB_ALLOW_NEUTRAL_CHAIN": "false"}):
            result = kb.evaluate_knowledge_setup(candidate, current)
        self.assertFalse(result["allowed"])
        self.assertFalse(result["evidence"]["option_chain"]["passed"])

    def test_contract_quality_requires_live_delta_and_tight_spread(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        candidate = qualified_candidate(current=current)
        candidate["option_summary"]["option_market_quality"]["spread_percent"] = 2.1
        result = kb.evaluate_knowledge_setup(candidate, current)
        self.assertFalse(result["allowed"])
        self.assertFalse(result["evidence"]["contract_quality"]["passed"])

    def test_prepared_trade_uses_actual_delta_and_fixed_nifty_points(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        candidate = qualified_candidate(current=current)
        decision = kb.evaluate_knowledge_setup(candidate, current)
        prepared = kb.prepare_candidate(candidate, decision)
        self.assertEqual(prepared["target_points"], 30)
        self.assertEqual(prepared["stop_points"], 30)
        self.assertEqual(prepared["target_price"], 165.6)
        self.assertEqual(prepared["stop_loss_price"], 134.4)
        self.assertEqual(prepared["entry_score"]["score_version"], kb.SCORE_VERSION)


if __name__ == "__main__":
    unittest.main()
